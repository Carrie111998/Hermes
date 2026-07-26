from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from hermes_cli import doctor as doctor_module
from hermes_cli import kanban_db
from hermes_cli.cost import ledger, task_cap_schema
from hermes_cli.lanes import enable, schema
from hermes_cli.programme import init as programme_init
from hermes_cli.subcommands import lanes as lanes_subcommands


NOW = datetime(2026, 7, 26, 0, 0, tzinfo=timezone.utc)


def _raw(*, enabled=False, publish_enabled=False, module=None):
    return {
        "schema_version": 1,
        "lanes": [
            {
                "lane_id": "tihna",
                "enabled": enabled,
                "module": module or "hermes_cli.lanes.impls.tihna",
                "approval_channel": "dashboard",
                "approval_timeout_hours": 168,
                "per_lane_daily_cost_cap_aud": 2.0,
                "per_lane_daily_task_cap": 15,
                "per_lane_hourly_ingest_cap": 5,
                "publish_enabled": publish_enabled,
                "description": "test",
            },
            {
                "lane_id": "dayroute",
                "enabled": False,
                "module": "json",
                "approval_channel": "telegram",
                "approval_timeout_hours": 24,
                "per_lane_daily_cost_cap_aud": 3.0,
                "per_lane_daily_task_cap": 50,
                "per_lane_hourly_ingest_cap": 20,
                "publish_enabled": False,
                "description": "test",
            },
            {
                "lane_id": "green_captains",
                "enabled": False,
                "module": "json",
                "approval_channel": "telegram",
                "approval_timeout_hours": 12,
                "per_lane_daily_cost_cap_aud": 2.0,
                "per_lane_daily_task_cap": 25,
                "per_lane_hourly_ingest_cap": 10,
                "publish_enabled": False,
                "description": "test",
            },
        ],
    }


@pytest.fixture
def lane_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = tmp_path / "kanban.db"
    manifest = tmp_path / "lane_manifest.yaml"
    manifest.write_text(yaml.safe_dump(_raw(), sort_keys=False))
    kanban_db.init_db(db)
    programme_init.migrate(db)
    schema.ensure_migrated(db)
    ledger.ensure_migrated(db)
    task_cap_schema.ensure_migrated(db)
    enable.ensure_audit_migrated(db)
    return {"db": db, "manifest": manifest}


def _doctor_ok(*_args, **_kwargs):
    return {
        "success": True,
        "module_status": "RESOLVABLE",
        "errors": [],
    }


def _doctor_absent(*_args, **_kwargs):
    return {
        "success": False,
        "module_status": "ABSENT",
        "errors": ["module absent"],
    }


def _flags(env, *, enabled=None, publish_enabled=None):
    raw = yaml.safe_load(env["manifest"].read_text())
    lane = raw["lanes"][0]
    if enabled is not None:
        lane["enabled"] = enabled
    if publish_enabled is not None:
        lane["publish_enabled"] = publish_enabled
    env["manifest"].write_text(yaml.safe_dump(raw, sort_keys=False))


def _enable(env, **kwargs):
    return enable.enable_lane(
        "tihna",
        True,
        manifest_path=env["manifest"],
        db_path=env["db"],
        doctor_runner=_doctor_ok,
        now=NOW,
        **kwargs,
    )


def _disable(env, **kwargs):
    return enable.disable_lane(
        "tihna",
        True,
        manifest_path=env["manifest"],
        db_path=env["db"],
        now=NOW,
        **kwargs,
    )


def _audit_rows(env):
    connection = schema.connect(env["db"])
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM lane_manifest_audit ORDER BY id"
            )
        ]
    finally:
        connection.close()


def _insert_task(env, *, status="ingested"):
    connection = schema.connect(env["db"])
    try:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        cursor = connection.execute(
            """
            INSERT INTO lane_task (
                lane_id, external_id, task_id, ingested_at,
                status, payload_json
            ) VALUES ('tihna', ?, 'task-1', ?, ?, '{}')
            """,
            (f"external-{status}", "2026-07-25T23:00:00Z", status),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def _insert_approval(env, *, status="pending", grant_ts=None):
    task_id = _insert_task(env)
    connection = schema.connect(env["db"])
    try:
        connection.execute(
            """
            INSERT INTO lane_approval_queue (
                lane_id, lane_task_id, approval_token, channel,
                draft_json, created_at, expires_at, status, grant_ts
            ) VALUES (
                'tihna', ?, ?, 'dashboard', '{}',
                '2026-07-25T22:00:00Z', '2026-07-27T00:00:00Z', ?, ?
            )
            """,
            (task_id, f"token-{status}", status, grant_ts),
        )
        connection.commit()
    finally:
        connection.close()
    return task_id


def test_enable_refuses_without_force_flag(lane_env):
    before = lane_env["manifest"].read_bytes()
    result = enable.enable_lane(
        "tihna",
        False,
        manifest_path=lane_env["manifest"],
        db_path=lane_env["db"],
    )
    assert result.exit_code == 2
    assert lane_env["manifest"].read_bytes() == before


def test_enable_refuses_when_programme_paused(lane_env):
    connection = schema.connect(lane_env["db"])
    connection.execute(
        "UPDATE programme_state SET state='PAUSED' WHERE id=1"
    )
    connection.commit()
    connection.close()
    result = _enable(lane_env)
    assert result.exit_code == 1
    assert "programme resume" in result.message


def test_enable_refuses_when_doctor_reports_module_absent(lane_env):
    result = enable.enable_lane(
        "tihna",
        True,
        manifest_path=lane_env["manifest"],
        db_path=lane_env["db"],
        doctor_runner=_doctor_absent,
        now=NOW,
    )
    assert result.exit_code == 1
    assert "doctor failed" in result.message


def test_enable_refuses_when_lane_already_enabled(lane_env):
    _flags(lane_env, enabled=True)
    assert _enable(lane_env).exit_code == 1


def test_enable_refuses_when_kill_switch_tripped(lane_env):
    connection = schema.connect(lane_env["db"])
    connection.execute(
        """
        INSERT INTO task_kill_switch (
            task_id, killed_ts, killed_by, reason
        ) VALUES ('task-killed', '2026-07-26T00:00:00Z', 'test', 'test')
        """
    )
    connection.commit()
    connection.close()
    result = _enable(lane_env)
    assert result.exit_code == 1
    assert "kill switch" in result.message


def test_enable_refuses_when_within_10pct_of_daily_cost_cap(lane_env):
    ledger.record_call(
        "task-cost",
        "platform",
        provider="openrouter",
        amount_aud=18.01,
        enforce_task_cap=False,
        db_path=lane_env["db"],
    )
    connection = schema.connect(lane_env["db"])
    connection.execute(
        "UPDATE programme_state SET state='RUNNING' WHERE id=1"
    )
    connection.commit()
    connection.close()
    result = _enable(lane_env)
    assert result.exit_code == 1
    assert "within 10%" in result.message


def test_enable_creates_backup_before_write(lane_env):
    before = lane_env["manifest"].read_bytes()
    result = _enable(lane_env)
    backup = Path(result.backup_path)
    assert backup.exists()
    assert backup.read_bytes() == before


def test_enable_atomic_write_uses_temp_and_rename(lane_env, monkeypatch):
    observed = []
    original = os.replace

    def track(source, destination):
        observed.append((Path(source), Path(destination)))
        return original(source, destination)

    monkeypatch.setattr(enable.os, "replace", track)
    assert _enable(lane_env).exit_code == 0
    assert observed
    assert observed[0][0].suffix == ".tmp"
    assert observed[0][1] == lane_env["manifest"]


def test_enable_does_not_touch_publish_enabled(lane_env):
    assert _enable(lane_env).exit_code == 0
    lane = yaml.safe_load(lane_env["manifest"].read_text())["lanes"][0]
    assert lane["enabled"] is True
    assert lane["publish_enabled"] is False


def test_enable_inserts_audit_row_with_correct_fields(lane_env):
    result = _enable(lane_env, notes="Monday")
    row = _audit_rows(lane_env)[0]
    assert result.audit_row_id == row["id"]
    assert (
        row["lane_id"],
        row["action"],
        row["previous_value"],
        row["new_value"],
        row["actor"],
        row["notes"],
    ) == ("tihna", "enable", 0, 1, "cli-operator", "Monday")


def test_enable_prints_json_confirmation(lane_env, monkeypatch, capsys):
    expected = enable.LaneEnableResult(
        lane_id="tihna",
        action="enable",
        success=True,
        exit_code=0,
        previous_enabled=False,
        new_enabled=True,
        backup_path="/tmp/backup",
        audit_row_id=1,
    )
    monkeypatch.setattr(
        lanes_subcommands.lane_enable,
        "enable_lane",
        lambda *_args, **_kwargs: expected,
    )
    args = argparse.Namespace(
        lanes_command="enable",
        lane_id="tihna",
        i_understand_this_is_live=True,
        manifest=str(lane_env["manifest"]),
        db_path=str(lane_env["db"]),
        notes=None,
    )
    assert lanes_subcommands.run_mutation_command(args) == 0
    assert json.loads(capsys.readouterr().out)["new_enabled"] is True


def test_enable_returns_exit_0_on_success(lane_env):
    assert _enable(lane_env).exit_code == 0


def test_disable_refuses_without_force_flag(lane_env):
    _flags(lane_env, enabled=True)
    result = enable.disable_lane(
        "tihna",
        False,
        manifest_path=lane_env["manifest"],
        db_path=lane_env["db"],
    )
    assert result.exit_code == 2


def test_disable_refuses_when_lane_already_disabled(lane_env):
    assert _disable(lane_env).exit_code == 1


def test_disable_refuses_with_active_claimed_tasks(lane_env):
    _flags(lane_env, enabled=True)
    _insert_task(lane_env, status="claimed")
    result = _disable(lane_env)
    assert result.exit_code == 1
    assert "claimed/claiming" in result.message


def test_disable_refuses_with_active_approval_queue_rows(lane_env):
    _flags(lane_env, enabled=True)
    _insert_approval(lane_env)
    result = _disable(lane_env)
    assert result.exit_code == 1
    assert "active approval" in result.message


def test_disable_also_flips_publish_enabled_when_true(lane_env):
    _flags(lane_env, enabled=True, publish_enabled=True)
    assert _disable(lane_env).exit_code == 0
    lane = yaml.safe_load(lane_env["manifest"].read_text())["lanes"][0]
    assert lane["enabled"] is False
    assert lane["publish_enabled"] is False


def test_disable_inserts_one_or_two_audit_rows_as_appropriate(lane_env):
    _flags(lane_env, enabled=True, publish_enabled=False)
    assert _disable(lane_env).exit_code == 0
    assert [row["action"] for row in _audit_rows(lane_env)] == ["disable"]

    _flags(lane_env, enabled=True, publish_enabled=True)
    assert _disable(lane_env).exit_code == 0
    assert [row["action"] for row in _audit_rows(lane_env)] == [
        "disable",
        "disable",
        "disable_publish",
    ]


def test_enable_publish_refuses_when_lane_disabled(lane_env):
    result = enable.enable_publish(
        "tihna",
        True,
        manifest_path=lane_env["manifest"],
        db_path=lane_env["db"],
        now=NOW,
    )
    assert result.exit_code == 1


def test_enable_publish_refuses_without_prior_successful_roundtrip(
    lane_env,
):
    _flags(lane_env, enabled=True)
    result = enable.enable_publish(
        "tihna",
        True,
        manifest_path=lane_env["manifest"],
        db_path=lane_env["db"],
        now=NOW,
    )
    assert result.exit_code == 1
    assert "no successful" in result.message


def test_enable_publish_accepts_prior_approved_approval_within_7_days(
    lane_env,
):
    _flags(lane_env, enabled=True)
    _insert_approval(
        lane_env,
        status="granted",
        grant_ts="2026-07-25T23:00:00Z",
    )
    result = enable.enable_publish(
        "tihna",
        True,
        manifest_path=lane_env["manifest"],
        db_path=lane_env["db"],
        now=NOW,
    )
    assert result.exit_code == 0


def test_enable_publish_accepts_prior_successful_publish_log_row(lane_env):
    _flags(lane_env, enabled=True)
    task_id = _insert_task(lane_env)
    connection = schema.connect(lane_env["db"])
    connection.execute(
        """
        INSERT INTO lane_publish_log (
            lane_id, lane_task_id, approval_token, external_target,
            side_effect_key, payload_json, published_at, outcome
        ) VALUES (
            'tihna', ?, 'token', 'smoke-test:local', 'key', '{}',
            '2026-07-25T23:00:00Z', 'success'
        )
        """,
        (task_id,),
    )
    connection.commit()
    connection.close()
    result = enable.enable_publish(
        "tihna",
        True,
        manifest_path=lane_env["manifest"],
        db_path=lane_env["db"],
        now=NOW,
    )
    assert result.exit_code == 0


def test_enable_publish_refuses_when_already_publish_enabled(lane_env):
    _flags(lane_env, enabled=True, publish_enabled=True)
    result = enable.enable_publish(
        "tihna",
        True,
        manifest_path=lane_env["manifest"],
        db_path=lane_env["db"],
        now=NOW,
    )
    assert result.exit_code == 1


def test_disable_publish_refuses_when_already_publish_disabled(lane_env):
    result = enable.disable_publish(
        "tihna",
        True,
        manifest_path=lane_env["manifest"],
        db_path=lane_env["db"],
        now=NOW,
    )
    assert result.exit_code == 1


def test_disable_publish_inserts_audit_row(lane_env):
    _flags(lane_env, enabled=True, publish_enabled=True)
    result = enable.disable_publish(
        "tihna",
        True,
        manifest_path=lane_env["manifest"],
        db_path=lane_env["db"],
        now=NOW,
    )
    assert result.exit_code == 0
    assert _audit_rows(lane_env)[0]["action"] == "disable_publish"


def test_lane_manifest_audit_table_created_idempotently(lane_env):
    enable.ensure_audit_migrated(lane_env["db"])
    connection = schema.connect(lane_env["db"])
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='lane_manifest_audit'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_lane_manifest_audit_migration_runs_twice_without_error(lane_env):
    enable.ensure_audit_migrated(lane_env["db"])
    enable.ensure_audit_migrated(lane_env["db"])
    assert enable.audit_summary(lane_env["db"]) == (
        "lane_manifest_audit: 0 rows"
    )


def test_hermes_lanes_audit_lists_history_by_lane(lane_env, capsys):
    assert _enable(lane_env).exit_code == 0
    args = argparse.Namespace(
        lane_id="tihna",
        limit=50,
        db_path=str(lane_env["db"]),
    )
    assert lanes_subcommands.run_audit_command(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output[0]["lane_id"] == "tihna"


def test_hermes_lanes_audit_respects_limit_flag(lane_env):
    _flags(lane_env, enabled=True, publish_enabled=True)
    assert _disable(lane_env).exit_code == 0
    assert len(enable.list_audit("tihna", limit=1, db_path=lane_env["db"])) == 1


def test_hermes_doctor_reports_audit_row_count_and_most_recent(
    lane_env,
    capsys,
):
    assert _enable(lane_env).exit_code == 0
    doctor_module._report_lane_manifest_audit()
    output = capsys.readouterr().out
    assert "lane_manifest_audit: 1 rows across 1 lanes" in output
    assert "tihna enable 2026-07-26T00:00:00Z" in output
