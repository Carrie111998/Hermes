from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_cli import kanban_db
from hermes_cli.cost import ledger, task_cap_schema, turns_schema
from hermes_cli.cutover.rehearse import rehearse_cutover
from hermes_cli.programme import init as programme_init
from hermes_cli.programme import ingress
from hermes_cli.routing import bootstrap, schema as routing_schema
from hermes_cli.subcommands import cutover


REPO = Path(__file__).parents[1]
HERMES_ROOT = Path("/Users/genesis/.hermes")


@pytest.fixture
def rehearsal_env(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    kanban_db.init_db(db_path)
    programme_init.migrate(db_path)
    ingress.migrate(db_path)
    doctrine_path = tmp_path / "doctrine_v1.json"
    shutil.copy2(
        HERMES_ROOT
        / "profiles/atlas/plugins/task-model-router/doctrine_v1.json",
        doctrine_path,
    )
    monkeypatch.setenv("HERMES_DOCTRINE_V1_PATH", str(doctrine_path))
    bootstrap.bootstrap_if_needed(db_path, doctrine_path)
    routing_schema.ensure_migrated(db_path)
    ledger.ensure_migrated(db_path)
    turns_schema.ensure_migrated(db_path)
    task_cap_schema.ensure_migrated(db_path)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            UPDATE programme_state
               SET state='PAUSED',
                   reason='cap hit: per_lane_platform: 2.01 AUD',
                   changed_by='cost_gate',
                   changed_at='2026-07-25T10:40:57Z'
             WHERE id=1
            """
        )
        connection.commit()
    finally:
        connection.close()

    lane_manifest = tmp_path / "lane_manifest.yaml"
    service_manifest = tmp_path / "service_manifest.yaml"
    shutil.copy2(HERMES_ROOT / "lane_manifest.yaml", lane_manifest)
    shutil.copy2(HERMES_ROOT / "service_manifest.yaml", service_manifest)
    return {
        "db_path": db_path,
        "lane_manifest": lane_manifest,
        "service_manifest": service_manifest,
    }


def _smoke(**_kwargs):
    return {
        "overall": "PASS",
        "scenario": "success",
        "lane": "default",
        "commit": False,
        "working_db_path": None,
        "stages": [{"name": "route_context_flush", "outcome": "success"}],
        "errors": [],
    }


def _doctor(*_args, **_kwargs):
    return {"success": True, "errors": []}


def _lane(*_args, **_kwargs):
    return {
        "lane_id": "tihna",
        "stage": "full",
        "success": True,
        "ingested": 2,
        "classified": 2,
        "drafted": 1,
        "approvals_enqueued": 1,
        "kanban_writes": 0,
        "cost_ledger_writes": 0,
        "side_effect_writes": 0,
    }


def _processes(manifest):
    return [
        {
            "service_id": service.id,
            "pid": 1000 + index,
            "alive": True,
            "start_time": "Sun Jul 26 08:00:00 2026",
            "command": service.command[0],
        }
        for index, service in enumerate(manifest.services)
    ]


def _report(rehearsal_env, **kwargs):
    return rehearse_cutover(
        db_path=rehearsal_env["db_path"],
        lane_manifest_path=rehearsal_env["lane_manifest"],
        service_manifest_path=rehearsal_env["service_manifest"],
        repo_root=REPO,
        now=datetime(2026, 7, 26, 1, 0, tzinfo=timezone.utc),
        smoke_runner=_smoke,
        lane_dry_runner=_lane,
        lane_doctor_runner=_doctor,
        process_snapshotter=_processes,
        **kwargs,
    )


def test_snapshots_programme_cost_lane_and_service_manifests(
    rehearsal_env,
):
    report = _report(rehearsal_env)
    assert report.programme["state"] == "PAUSED"
    assert report.cost_today_melbourne["calendar_date"] == "2026-07-26"
    assert report.lane_manifest["schema_version"] == 1
    assert report.service_manifest["operator_review_required"] is True


def test_reports_inferred_unlanded_specs_with_caveat(rehearsal_env):
    report = _report(rehearsal_env)
    assert {item["spec_id"] for item in report.inferred_unlanded_specs}
    assert all("not proof of git history" in item["caveat"] for item in report.inferred_unlanded_specs)


def test_restart_plan_has_stop_drain_restart_order_and_never_executes(
    rehearsal_env,
):
    plan = _report(rehearsal_env).restart_plan
    assert plan["would_execute"] is False
    assert plan["stop_order"] == list(reversed(plan["restart_order"]))
    assert len(plan["drain"]) == len(plan["restart_order"])


def test_rehearsal_requires_operator_unlock(rehearsal_env):
    report = _report(rehearsal_env)
    assert report.requires_operator_unlock is True
    assert report.restart_plan["requires_operator_unlock"] is True


def test_all_seventeen_preconditions_are_explicit_pass_or_fail(
    rehearsal_env,
):
    checks = _report(rehearsal_env).preconditions
    assert len(checks) == 17
    assert {item.status for item in checks} <= {"PASS", "FAIL"}


def test_smoke_dry_run_result_is_embedded(rehearsal_env):
    report = _report(rehearsal_env)
    assert report.smoke_dry_run["overall"] == "PASS"
    assert report.smoke_dry_run["commit"] is False


def test_lane_full_dry_run_result_is_embedded(rehearsal_env):
    report = _report(rehearsal_env)
    assert report.lane_dry_run["success"] is True
    assert report.lane_dry_run["stage"] == "full"


def test_cost_options_are_printed_but_not_executed(rehearsal_env):
    options = _report(rehearsal_env).cap_options
    assert options[0]["command"] == (
        "hermes programme resume --acknowledge-cap-hit"
    )
    assert options[1]["command"] == (
        "hermes programme resume --roll-cap-window"
    )


def test_signed_manifest_sha256_matches_unsigned_content(rehearsal_env):
    report = _report(rehearsal_env)
    payload = report.to_dict()
    signature = payload.pop("sha256")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert hashlib.sha256(encoded.encode()).hexdigest() == signature


def test_rehearsal_changes_zero_production_database_bytes(rehearsal_env):
    path = rehearsal_env["db_path"]
    before = path.read_bytes()
    _report(rehearsal_env)
    assert path.read_bytes() == before


def test_rehearsal_performs_no_restart(rehearsal_env, monkeypatch):
    called = []
    monkeypatch.setattr(
        "hermes_cli.service.runner.RestartRunner.execute",
        lambda *_args, **_kwargs: called.append(True),
    )
    _report(rehearsal_env)
    assert called == []


def test_rehearsal_performs_no_programme_resume(rehearsal_env, monkeypatch):
    called = []
    monkeypatch.setattr(
        "hermes_cli.programme.gate.set_state",
        lambda *_args, **_kwargs: called.append(True),
    )
    _report(rehearsal_env)
    assert called == []


def test_service_manifest_operator_lock_remains_unchanged(rehearsal_env):
    path = rehearsal_env["service_manifest"]
    before = path.read_bytes()
    _report(rehearsal_env)
    assert path.read_bytes() == before


def test_exit_zero_when_every_precondition_passes(rehearsal_env):
    report = _report(
        rehearsal_env,
        precondition_overrides={
            item: True
            for item in (
                "CS01c", "CS04", "CS05rev", "CS05b", "CS06",
                "CS10a", "CS10brev", "CS12", "CS13", "CS14",
                "CS15", "CS16", "CS18", "DB", "TIHNA_DOCTRINE",
                "LANE_MANIFEST", "KILL_SWITCH",
            )
        },
    )
    assert report.go_no_go == "GO"
    assert report.exit_code == 0


def test_exit_one_when_any_precondition_fails(rehearsal_env):
    report = _report(
        rehearsal_env,
        precondition_overrides={"CS04": False},
    )
    assert report.go_no_go == "NO-GO"
    assert report.exit_code == 1


def test_cli_prints_json_and_markdown_and_returns_zero(
    monkeypatch,
    capsys,
):
    class Report:
        exit_code = 0

        @staticmethod
        def to_json():
            return '{"go_no_go":"GO"}'

        @staticmethod
        def to_markdown():
            return "# GO"

    monkeypatch.setattr(cutover, "rehearse_cutover", lambda **_kwargs: Report())
    code = cutover.run_rehearse_command(
        argparse.Namespace(
            db_path="db",
            lane_manifest_path="lanes",
            service_manifest_path="services",
            repo_root="repo",
        )
    )
    assert code == 0
    assert '{"go_no_go":"GO"}' in capsys.readouterr().out


def test_cli_returns_one_for_no_go(monkeypatch):
    class Report:
        exit_code = 1

        @staticmethod
        def to_json():
            return "{}"

        @staticmethod
        def to_markdown():
            return "# NO-GO"

    monkeypatch.setattr(cutover, "rehearse_cutover", lambda **_kwargs: Report())
    assert cutover.run_rehearse_command(
        argparse.Namespace(
            db_path="db",
            lane_manifest_path="lanes",
            service_manifest_path="services",
            repo_root="repo",
        )
    ) == 1


def test_markdown_carries_operator_actions_and_signature(rehearsal_env):
    report = _report(rehearsal_env)
    markdown = report.to_markdown()
    assert "Required operator actions" in markdown
    assert report.sha256 in markdown
