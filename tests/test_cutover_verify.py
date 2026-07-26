from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli import kanban_db
from hermes_cli.cost import ledger, task_cap_schema
from hermes_cli.cutover.verify import verify_cutover
from hermes_cli.lanes import enable
from hermes_cli.programme import init as programme_init
from hermes_cli.routing import bootstrap, schema as routing_schema
from hermes_cli.subcommands import cutover


NOW = datetime(2026, 7, 26, 1, 0, tzinfo=timezone.utc)
PROCESS_START = datetime(2026, 7, 26, 0, 45, tzinfo=timezone.utc)
HERMES_ROOT = Path("/Users/genesis/.hermes")
EXPECTED_SERVICES = (
    "dashboard",
    "hermes_app",
    "hermes_server",
    "blender_mcp_watchdog_server",
    "tui_slash_worker",
    "blender_mcp_watchdog_tui",
    "atlas_gateway",
    "blender_mcp_watchdog_gateway",
)


@pytest.fixture
def verify_env(tmp_path: Path):
    db = tmp_path / "kanban.db"
    kanban_db.init_db(db)
    programme_init.migrate(db)
    ledger.ensure_migrated(db)
    task_cap_schema.ensure_migrated(db)
    enable.ensure_audit_migrated(db)

    doctrine_seed = tmp_path / "doctrine_v1.json"
    shutil.copy2(
        HERMES_ROOT
        / "profiles/atlas/plugins/task-model-router/doctrine_v1.json",
        doctrine_seed,
    )
    bootstrap.bootstrap_if_needed(db, doctrine_seed)
    routing_schema.ensure_migrated(db)

    connection = sqlite3.connect(db)
    try:
        connection.execute(
            """
            UPDATE programme_state
               SET state='PAUSED',
                   reason='test pause',
                   changed_by='test',
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

    code_files = {}
    for name in ("run_agent.py", "conversation_loop.py", "tihna.py"):
        path = tmp_path / name
        path.write_text(f"# {name}\n", encoding="utf-8")
        old = (NOW - timedelta(hours=1)).timestamp()
        os.utime(path, (old, old))
        code_files[name] = path
    return {
        "db": db,
        "lane_manifest": lane_manifest,
        "service_manifest": service_manifest,
        "doctrine_seed": doctrine_seed,
        "code_files": code_files,
        "repo": tmp_path,
    }


def _processes(_manifest, *, now):
    return [
        {
            "service_id": service_id,
            "expected_name": service_id,
            "pid": 2000 + index,
            "alive": True,
            "start_time": PROCESS_START.isoformat(),
            "age_minutes": (now - PROCESS_START).total_seconds() / 60,
            "command": f"/usr/bin/python -m test_{service_id}",
            "entrypoint": f"test_{service_id}",
            "module_path": f"/tmp/test_{service_id}.py",
        }
        for index, service_id in enumerate(EXPECTED_SERVICES)
    ]


def _smoke(**_kwargs):
    return {
        "overall": "PASS",
        "scenario": "success",
        "lane": "default",
        "commit": False,
        "source_db_path": "/tmp/source.db",
        "working_db_path": None,
        "errors": [],
        "stages": [
            {
                "name": "route_for_turn",
                "outcome": "success",
                "details": {
                    "chosen_provider": "openai-codex",
                    "chosen_model": "gpt-5-6-sol",
                },
            },
            {
                "name": "cost_ledger",
                "outcome": "success",
                "details": {"aud": 0.01},
            },
        ],
    }


def _lane(*_args, **_kwargs):
    return {
        "lane_id": "tihna",
        "stage": "full",
        "success": True,
        "ingested": 2,
        "classified": 2,
        "drafted": 1,
        "fixture_feed_used": True,
        "fake_llm_used": True,
        "kanban_writes": 0,
        "cost_ledger_writes": 0,
        "side_effect_writes": 0,
        "error": None,
    }


def _rehearsal(**_kwargs):
    return {
        "go_no_go": "GO",
        "sha256": "a" * 64,
        "requires_operator_unlock": True,
        "restart_plan": {"would_execute": False},
        "preconditions": [
            {
                "precondition_id": f"P{index}",
                "status": "PASS",
                "label": "test",
                "detail": "ok",
            }
            for index in range(17)
        ],
    }


def _route_context():
    return {
        "success": True,
        "env_cleared_after_read": True,
        "second_read_matches_cached": True,
    }


def _report(verify_env, **overrides):
    values = {
        "restart_not_before": (
            NOW - timedelta(minutes=30)
        ).isoformat(),
        "db_path": verify_env["db"],
        "lane_manifest_path": verify_env["lane_manifest"],
        "service_manifest_path": verify_env["service_manifest"],
        "doctrine_seed_path": verify_env["doctrine_seed"],
        "repo_root": verify_env["repo"],
        "now": NOW,
        "process_snapshotter": _processes,
        "smoke_runner": _smoke,
        "lane_dry_runner": _lane,
        "rehearsal_runner": _rehearsal,
        "route_context_checker": _route_context,
        "key_code_files": verify_env["code_files"],
    }
    values.update(overrides)
    return verify_cutover(**values)


def test_verify_reports_all_8_protected_processes_by_name(verify_env):
    report = _report(verify_env)
    assert len(report.processes) == 8
    assert {
        item["service_id"] for item in report.processes
    } == set(EXPECTED_SERVICES)
    assert next(
        item for item in report.checks
        if item.check_id == "PROCESSES_PRESENT"
    ).status == "PASS"


def test_verify_fails_when_any_process_missing(verify_env):
    def missing(manifest, *, now):
        return _processes(manifest, now=now)[:-1]

    report = _report(verify_env, process_snapshotter=missing)
    assert report.exit_code == 1
    assert "PROCESSES_PRESENT" in report.overall_verdict


def test_verify_fails_when_any_process_has_pre_cutover_start_time(
    verify_env,
):
    def stale(manifest, *, now):
        values = _processes(manifest, now=now)
        values[0]["start_time"] = (
            NOW - timedelta(hours=2)
        ).isoformat()
        return values

    report = _report(verify_env, process_snapshotter=stale)
    check = next(
        item for item in report.checks
        if item.check_id == "PROCESSES_FRESH"
    )
    assert check.status == "FAIL"
    assert report.exit_code == 1


def test_verify_compares_key_file_mtime_against_process_start_time(
    verify_env,
):
    report = _report(verify_env)
    assert all(
        item["newer_than_processes"] == []
        for item in report.code_freshness["files"]
    )
    assert next(
        item for item in report.checks
        if item.check_id == "CODE_LOADED"
    ).status == "PASS"


def test_verify_fails_when_key_file_mtime_newer_than_process_start(
    verify_env,
):
    path = verify_env["code_files"]["tihna.py"]
    newer = (PROCESS_START + timedelta(minutes=1)).timestamp()
    os.utime(path, (newer, newer))
    report = _report(verify_env)
    assert report.exit_code == 1
    assert report.code_freshness["violations"]


def test_verify_reports_programme_state_and_flags_paused_as_ok_but_not_yet_ready(
    verify_env,
):
    report = _report(verify_env)
    assert report.programme["state"] == "PAUSED"
    assert "resume with" in report.programme_status
    assert next(
        item for item in report.checks
        if item.check_id == "PROGRAMME_STATE"
    ).status == "PASS"


def test_verify_invokes_smoke_turn_dry_run_and_reports_result(
    verify_env,
):
    calls = []

    def smoke(**kwargs):
        calls.append(kwargs)
        return _smoke(**kwargs)

    report = _report(verify_env, smoke_runner=smoke)
    assert calls[0]["commit"] is False
    assert report.smoke_dry_run["healthy_round_trip"] is True
    assert report.smoke_dry_run["chosen_provider"] == "openai-codex"


def test_verify_invokes_lane_dry_run_and_reports_result(verify_env):
    calls = []

    def lane(*args, **kwargs):
        calls.append((args, kwargs))
        return _lane(*args, **kwargs)

    report = _report(verify_env, lane_dry_runner=lane)
    assert calls[0][0] == ("tihna",)
    assert calls[0][1]["stage"] == "full"
    assert report.lane_dry_run["drafted"] == 1


def test_verify_invokes_cutover_rehearse_and_compares_preconditions(
    verify_env,
):
    calls = []

    def rehearsal(**kwargs):
        calls.append(kwargs)
        return _rehearsal(**kwargs)

    report = _report(verify_env, rehearsal_runner=rehearsal)
    assert calls
    assert report.cutover_rehearsal["precondition_count"] == 17
    assert report.cutover_rehearsal["go_no_go"] == "GO"


def test_verify_reports_lane_manifest_audit_history(verify_env):
    connection = sqlite3.connect(verify_env["db"])
    try:
        connection.execute(
            """
            INSERT INTO lane_manifest_audit (
                lane_id, action, previous_value, new_value,
                actor, timestamp_utc, notes
            ) VALUES (
                'tihna', 'enable', 0, 1,
                'test', '2026-07-26T00:30:00Z', 'test row'
            )
            """
        )
        connection.commit()
    finally:
        connection.close()
    report = _report(verify_env)
    assert report.lane_manifest_audit["total_rows"] == 1
    assert report.lane_manifest_audit["rows_last_24h"][0]["lane_id"] == "tihna"


def test_verify_checks_route_context_env_var_synthetically(verify_env):
    calls = []

    def route_check():
        calls.append(True)
        return _route_context()

    report = _report(
        verify_env,
        route_context_checker=route_check,
    )
    assert calls == [True]
    assert report.route_context["env_cleared_after_read"] is True


def test_verify_reports_kill_switch_state_and_fails_on_active_entries(
    verify_env,
):
    connection = sqlite3.connect(verify_env["db"])
    try:
        connection.execute(
            """
            INSERT INTO task_kill_switch (
                task_id, killed_ts, killed_by, reason
            ) VALUES (
                'task-1', '2026-07-26T00:30:00Z', 'test', 'test'
            )
            """
        )
        connection.commit()
    finally:
        connection.close()
    report = _report(verify_env)
    assert report.kill_switch["active_rows"] == 1
    assert next(
        item for item in report.checks
        if item.check_id == "KILL_SWITCH"
    ).status == "FAIL"


def test_verify_checks_doctrine_drift_against_seed(verify_env):
    clean = _report(verify_env)
    assert clean.doctrine["drifted_lanes"] == []
    connection = sqlite3.connect(verify_env["db"])
    try:
        connection.execute(
            """
            UPDATE routing_doctrine
               SET primary_model='unexpected-model'
             WHERE lane='tihna'
            """
        )
        connection.commit()
    finally:
        connection.close()
    drifted = _report(verify_env)
    assert "tihna" in drifted.doctrine["drifted_lanes"]
    assert drifted.exit_code == 1


def test_verify_reports_cost_cap_window_and_remaining_budget(verify_env):
    connection = sqlite3.connect(verify_env["db"])
    try:
        connection.execute(
            """
            INSERT INTO cost_ledger (
                ts, task_id, lane, vendor, model_slug,
                usd_amount, aud_amount, fx_rate
            ) VALUES (
                '2026-07-26T00:30:00Z', 'task-cost', 'tihna',
                'openai-codex', 'gpt-5-6-sol', 0.5, 1.25, 1.52
            )
            """
        )
        connection.commit()
    finally:
        connection.close()
    report = _report(verify_env)
    assert report.cost_cap["billable_aud"] == 1.25
    assert report.cost_cap["remaining_aud"] == 18.75
    assert report.cost_cap["within_10_percent"] is False


def test_verify_emits_signed_manifest_with_sha256(verify_env):
    report = _report(verify_env)
    payload = report.to_dict()
    signature = payload.pop("sha256")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert hashlib.sha256(encoded).hexdigest() == signature
    assert signature in report.to_markdown()


def test_verify_exits_0_on_HEALTHY_and_1_on_ABORT(
    verify_env,
    monkeypatch,
):
    healthy = _report(verify_env)

    def missing(manifest, *, now):
        return _processes(manifest, now=now)[:-1]

    abort = _report(verify_env, process_snapshotter=missing)
    assert healthy.exit_code == 0
    assert healthy.overall_verdict == "HEALTHY"
    assert abort.exit_code == 1
    assert abort.overall_verdict.startswith("ABORT")

    reports = iter((healthy, abort))
    monkeypatch.setattr(
        cutover,
        "verify_cutover",
        lambda **_kwargs: next(reports),
    )
    args = argparse.Namespace(
        restart_not_before=NOW.isoformat(),
        db_path="db",
        lane_manifest_path="lanes",
        service_manifest_path="services",
        doctrine_seed_path="seed",
        repo_root="repo",
    )
    assert cutover.run_verify_command(args) == 0
    assert cutover.run_verify_command(args) == 1
