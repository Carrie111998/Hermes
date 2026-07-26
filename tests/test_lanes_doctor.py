from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml

from hermes_cli.lanes.doctor import run_lane_doctor


def _raw(*, module="hermes_cli.lanes.impls.tihna"):
    return {
        "schema_version": 1,
        "lanes": [
            {
                "lane_id": "tihna",
                "enabled": False,
                "module": module,
                "approval_channel": "dashboard",
                "approval_timeout_hours": 168,
                "per_lane_daily_cost_cap_aud": 2.0,
                "per_lane_daily_task_cap": 15,
                "per_lane_hourly_ingest_cap": 5,
                "publish_enabled": False,
                "description": "Tihna test",
            }
        ],
    }


def _manifest(tmp_path: Path, *, module=None) -> Path:
    path = tmp_path / "lane_manifest.yaml"
    path.write_text(
        yaml.safe_dump(
            _raw(
                module=module
                if module is not None
                else "hermes_cli.lanes.impls.tihna"
            )
        ),
        encoding="utf-8",
    )
    return path


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "kanban.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE routing_doctrine(
                 id INTEGER PRIMARY KEY, version INTEGER NOT NULL,
                 lane TEXT NOT NULL, primary_provider TEXT NOT NULL,
                 primary_model TEXT NOT NULL,
                 fallback_chain_json TEXT NOT NULL)"""
        )
        conn.execute(
            """INSERT INTO routing_doctrine(
                 id,version,lane,primary_provider,primary_model,
                 fallback_chain_json)
               VALUES(4,1,'tihna','openai-codex','gpt-5-6-sol','[]')"""
        )
    return path


def _report(tmp_path: Path, *, module=None):
    return run_lane_doctor(
        "tihna",
        manifest_path=_manifest(tmp_path, module=module),
        db_path=_db(tmp_path),
    )


def test_doctor_reports_lane_registered_for_tihna(tmp_path):
    assert _report(tmp_path).registered is True


def test_doctor_reports_module_resolvable_for_tihna(tmp_path):
    assert _report(tmp_path).module_status == "RESOLVABLE"


def test_doctor_confirms_tihna_satisfies_BusinessLane_protocol(tmp_path):
    assert _report(tmp_path).protocol_satisfied is True


def test_doctor_reads_manifest_enabled_publish_cap_channels_for_tihna(
    tmp_path,
):
    manifest = _report(tmp_path).manifest
    assert manifest == {
        "approval_channel": "dashboard",
        "daily_cost_cap_aud": 2.0,
        "enabled": False,
        "publish_channel": "local:file:tihna-digests",
        "publish_enabled": False,
    }


def test_doctor_reads_doctrine_row_provider_model_for_tihna_readonly(
    tmp_path,
):
    report = _report(tmp_path)
    assert report.doctrine["provider"] == "openai-codex"
    assert report.doctrine["model"] == "gpt-5-6-sol"


def test_doctor_reads_rate_limit_config_for_tihna(tmp_path):
    assert _report(tmp_path).rate_limits == {
        "daily_cost_cap_aud": 2.0,
        "daily_task_cap": 15,
        "hourly_ingest_cap": 5,
    }


def test_doctor_runs_all_eight_hygiene_checks_against_tihna_module(
    tmp_path,
):
    checks = _report(tmp_path).hygiene_checks
    assert len(checks) == 8
    assert all(checks.values())


def test_doctor_confirms_all_ten_harness_methods_present_with_signatures(
    tmp_path,
):
    methods = _report(tmp_path).harness_methods
    assert len(methods) == 10
    assert all(result["signature_ok"] for result in methods.values())


def test_doctor_confirms_publish_disabled_raises_PublishDisabled(
    tmp_path,
):
    assert _report(tmp_path).publish_disabled_guard is True


def test_doctor_returns_exit_0_when_all_checks_pass(tmp_path):
    report = _report(tmp_path)
    assert report.success is True
    assert report.exit_code == 0


def test_doctor_returns_exit_1_when_any_check_fails(tmp_path):
    report = _report(tmp_path, module="does.not.exist")
    assert report.success is False
    assert report.exit_code == 1
    assert report.module_status == "ABSENT"


def test_doctor_writes_zero_rows_to_kanban_db(tmp_path):
    manifest = _manifest(tmp_path)
    db = _db(tmp_path)
    before = db.read_bytes()
    run_lane_doctor(
        "tihna",
        manifest_path=manifest,
        db_path=db,
    )
    assert db.read_bytes() == before


def test_doctor_json_report_shape_stable(tmp_path):
    report = _report(tmp_path)
    payload = json.loads(report.to_json())
    assert list(payload) == sorted(payload)
    assert {
        "checks",
        "doctrine",
        "errors",
        "harness_methods",
        "hygiene_checks",
        "lane_id",
        "manifest",
        "module_status",
        "protocol_satisfied",
        "publish_disabled_guard",
        "rate_limits",
        "registered",
        "success",
    } == set(payload)


def test_doctor_handles_missing_lane_id_gracefully(tmp_path):
    report = run_lane_doctor(
        "missing",
        manifest_path=_manifest(tmp_path),
        db_path=_db(tmp_path),
    )
    assert report.exit_code == 1
    assert report.registered is False
    assert report.errors == ("unknown lane: missing",)
