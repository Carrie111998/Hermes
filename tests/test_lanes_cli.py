from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

from hermes_cli.cost.ledger import record_call
from hermes_cli.lanes import approvals, schema
from hermes_cli.lanes.cli import register_cli
from hermes_cli.lanes.contracts import LaneDraft, LaneTask
from hermes_cli.programme.init import migrate


def _raw(
    *,
    enabled: bool = False,
    module: str = "json",
    publish_enabled: bool = False,
) -> dict:
    return {
        "schema_version": 1,
        "lanes": [
            {
                "lane_id": "dayroute",
                "enabled": enabled,
                "module": module,
                "approval_channel": "dashboard",
                "approval_timeout_hours": 24,
                "per_lane_daily_cost_cap_aud": 3.0,
                "per_lane_daily_task_cap": 50,
                "per_lane_hourly_ingest_cap": 20,
                "publish_enabled": publish_enabled,
                "description": "DayRoute test",
            },
            {
                "lane_id": "tihna",
                "enabled": False,
                "module": "json",
                "approval_channel": "dashboard",
                "approval_timeout_hours": 168,
                "per_lane_daily_cost_cap_aud": 2.0,
                "per_lane_daily_task_cap": 15,
                "per_lane_hourly_ingest_cap": 5,
                "publish_enabled": False,
                "description": "Tihna test",
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
                "description": "Green Captains test",
            },
        ],
    }


def _manifest(tmp_path: Path, **kwargs) -> Path:
    path = tmp_path / "lane_manifest.yaml"
    path.write_text(yaml.safe_dump(_raw(**kwargs)), encoding="utf-8")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes")
    register_cli(parser.add_subparsers(dest="command", required=True))
    return parser


def _invoke(argv: list[str]) -> int:
    args = _parser().parse_args(["lanes", *argv])
    try:
        result = args.func(args)
    except SystemExit as exc:
        return int(exc.code)
    return int(result or 0)


def _paths_args(manifest: Path, db: Path) -> list[str]:
    return ["--manifest", str(manifest), "--db-path", str(db)]


def _task(db: Path) -> LaneTask:
    schema.ensure_migrated(db)
    conn = schema.connect(db)
    cursor = conn.execute(
        """INSERT INTO lane_task(
             lane_id,external_id,task_id,ingested_at,status,payload_json)
           VALUES('dayroute','external-1','task-1',
                  '2026-01-01T00:00:00Z','drafted','{}')"""
    )
    conn.commit()
    lane_task_id = int(cursor.lastrowid)
    conn.close()
    return LaneTask(
        lane_id="dayroute",
        external_id="external-1",
        payload={},
        task_id="task-1",
        id=lane_task_id,
        status="drafted",
    )


def _approval(db: Path) -> str:
    request = approvals.enqueue(
        task=_task(db),
        draft=LaneDraft("draft"),
        channel="dashboard",
        timeout_hours=24,
        db_path=db,
    )
    return request.token


def test_cli_lanes_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        _parser().parse_args(["lanes", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    for command in ("list", "describe", "approvals", "run", "enable", "disable"):
        assert command in output


def test_cli_lanes_list_shows_three_lanes_all_disabled(tmp_path, capsys):
    db = tmp_path / "kanban.db"
    manifest = _manifest(tmp_path)
    assert _invoke(["list", *_paths_args(manifest, db)]) == 0
    output = capsys.readouterr().out
    assert output.count("DISABLED") == 3
    assert all(
        lane in output for lane in ("dayroute", "tihna", "green_captains")
    )


def test_cli_lanes_enable_flips_manifest_and_records_state(tmp_path):
    db = tmp_path / "kanban.db"
    manifest = _manifest(tmp_path)
    assert _invoke(
        [
            "enable",
            "--lane",
            "dayroute",
            "--reason",
            "test",
            *_paths_args(manifest, db),
        ]
    ) == 0
    raw = yaml.safe_load(manifest.read_text())
    assert raw["lanes"][0]["enabled"] is True
    conn = schema.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM lane_manifest_state"
    ).fetchone()[0] == 1
    conn.close()


def test_cli_lanes_disable_flips_manifest_and_records_state(tmp_path):
    db = tmp_path / "kanban.db"
    manifest = _manifest(tmp_path, enabled=True)
    assert _invoke(
        [
            "disable",
            "--lane",
            "dayroute",
            "--reason",
            "test",
            *_paths_args(manifest, db),
        ]
    ) == 0
    assert yaml.safe_load(manifest.read_text())["lanes"][0]["enabled"] is False


def test_cli_lanes_enable_publish_true_requires_confirmation_flag(tmp_path):
    db = tmp_path / "kanban.db"
    manifest = _manifest(tmp_path, publish_enabled=True)
    base = [
        "enable",
        "--lane",
        "dayroute",
        "--reason",
        "test",
        *_paths_args(manifest, db),
    ]
    assert _invoke(base) == 1
    assert _invoke(
        [
            *base,
            "--i-understand-lane-will-write-external-side-effects",
        ]
    ) == 0


def test_cli_lanes_enable_missing_module_reports_LaneModuleNotFound(
    tmp_path, capsys
):
    db = tmp_path / "kanban.db"
    manifest = _manifest(tmp_path, module="does.not.exist")
    assert _invoke(
        [
            "enable",
            "--lane",
            "dayroute",
            "--reason",
            "test",
            *_paths_args(manifest, db),
        ]
    ) == 1
    assert "lane module is not installed" in capsys.readouterr().out


def test_cli_lanes_run_refuses_when_programme_paused_no_dry_run(
    tmp_path, capsys
):
    db = tmp_path / "kanban.db"
    migrate(db)
    conn = schema.connect(db)
    conn.execute(
        "UPDATE programme_state SET state='PAUSED',reason='test' WHERE id=1"
    )
    conn.commit()
    conn.close()
    manifest = _manifest(tmp_path, enabled=True)
    assert _invoke(
        [
            "run",
            "--lane",
            "dayroute",
            "--stage",
            "ingest",
            *_paths_args(manifest, db),
        ]
    ) == 2
    assert "programme is PAUSED" in capsys.readouterr().out


def test_cli_lanes_run_proceeds_dry_run_when_programme_paused(
    tmp_path, capsys
):
    db = tmp_path / "kanban.db"
    migrate(db)
    conn = schema.connect(db)
    conn.execute("UPDATE programme_state SET state='PAUSED' WHERE id=1")
    conn.commit()
    conn.close()
    manifest = _manifest(tmp_path)
    assert _invoke(
        [
            "run",
            "--lane",
            "dayroute",
            "--stage",
            "draft",
            "--dry-run",
            *_paths_args(manifest, db),
        ]
    ) == 0
    assert "dry-run" in capsys.readouterr().out


def test_cli_lanes_full_cycle_stops_at_approve_stage_by_default(
    tmp_path, capsys
):
    db = tmp_path / "kanban.db"
    manifest = _manifest(tmp_path)
    assert _invoke(
        [
            "run",
            "--lane",
            "dayroute",
            "--full-cycle",
            "--dry-run",
            *_paths_args(manifest, db),
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "ingest -> draft -> approve" in output
    assert "publish intentionally skipped" in output


def test_cli_lanes_approvals_list_shows_pending(tmp_path, capsys):
    db = tmp_path / "kanban.db"
    manifest = _manifest(tmp_path)
    token = _approval(db)
    assert _invoke(
        ["approvals", "list", *_paths_args(manifest, db)]
    ) == 0
    assert token in capsys.readouterr().out


def test_cli_lanes_approvals_grant_marks_granted(tmp_path):
    db = tmp_path / "kanban.db"
    manifest = _manifest(tmp_path)
    token = _approval(db)
    assert _invoke(
        [
            "approvals",
            "grant",
            "--token",
            token,
            *_paths_args(manifest, db),
        ]
    ) == 0
    assert approvals.check(token, db_path=db).status == "granted"


def test_cli_lanes_approvals_grant_bad_token_exits_1(tmp_path):
    db = tmp_path / "kanban.db"
    manifest = _manifest(tmp_path)
    assert _invoke(
        [
            "approvals",
            "grant",
            "--token",
            "BADTOKEN0000",
            *_paths_args(manifest, db),
        ]
    ) == 1


def test_cli_lanes_approvals_reject_captures_reason(tmp_path):
    db = tmp_path / "kanban.db"
    manifest = _manifest(tmp_path)
    token = _approval(db)
    assert _invoke(
        [
            "approvals",
            "reject",
            "--token",
            token,
            "--reason",
            "needs changes",
            *_paths_args(manifest, db),
        ]
    ) == 0
    assert approvals.check(token, db_path=db).reject_reason == "needs changes"


def test_cli_lanes_describe_shows_config(tmp_path, capsys):
    db = tmp_path / "kanban.db"
    manifest = _manifest(tmp_path)
    assert _invoke(
        [
            "describe",
            "--lane",
            "dayroute",
            *_paths_args(manifest, db),
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "lane_id: dayroute" in output
    assert "approval_channel: dashboard" in output


def test_cli_lanes_describe_shows_recent_tasks(tmp_path, capsys):
    db = tmp_path / "kanban.db"
    manifest = _manifest(tmp_path)
    _task(db)
    assert _invoke(
        [
            "describe",
            "--lane",
            "dayroute",
            *_paths_args(manifest, db),
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "recent_tasks: 1" in output
    assert "external-1" in output


def test_cli_lanes_describe_shows_aggregate_costs(tmp_path, capsys):
    db = tmp_path / "kanban.db"
    manifest = _manifest(tmp_path)
    record_call(
        task_id=None,
        lane="dayroute",
        vendor="openai-codex",
        model="mock",
        db_path=db,
    )
    assert _invoke(
        [
            "describe",
            "--lane",
            "dayroute",
            *_paths_args(manifest, db),
        ]
    ) == 0
    assert "aggregate_cost_aud: 0.0000" in capsys.readouterr().out
