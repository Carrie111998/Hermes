from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import yaml

from hermes_cli.lanes.cli import register_cli


def _manifest(tmp_path: Path) -> Path:
    path = tmp_path / "lane_manifest.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "lanes": [
                    {
                        "lane_id": "tihna",
                        "enabled": False,
                        "module": "hermes_cli.lanes.impls.tihna",
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
            """INSERT INTO routing_doctrine VALUES(
                 4,1,'tihna','openai-codex','gpt-5-6-sol','[]')"""
        )
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes")
    register_cli(
        parser.add_subparsers(dest="command", required=True)
    )
    return parser


def _invoke(tmp_path: Path, argv: list[str]) -> tuple[int, dict]:
    args = _parser().parse_args(
        [
            "lanes",
            *argv,
            "--manifest",
            str(_manifest(tmp_path)),
            "--db-path",
            str(_db(tmp_path)),
        ]
    )
    try:
        result = args.func(args)
        code = int(result or 0)
    except SystemExit as exc:
        code = int(exc.code)
    return code, {}


def _payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_cli_lanes_doctor_tihna_exits_0(tmp_path, capsys):
    code, _ = _invoke(tmp_path, ["doctor", "tihna"])
    assert code == 0
    assert _payload(capsys)["success"] is True


def test_cli_lanes_doctor_unknown_lane_exits_1(tmp_path, capsys):
    code, _ = _invoke(tmp_path, ["doctor", "unknown"])
    assert code == 1
    assert _payload(capsys)["registered"] is False


def test_cli_lanes_dry_run_tihna_ingest_exits_0(tmp_path, capsys):
    code, _ = _invoke(
        tmp_path,
        ["dry-run", "tihna", "--stage", "ingest"],
    )
    assert code == 0
    assert _payload(capsys)["ingested"] == 2


def test_cli_lanes_dry_run_tihna_digest_exits_0(tmp_path, capsys):
    code, _ = _invoke(
        tmp_path,
        ["dry-run", "tihna", "--stage", "digest"],
    )
    assert code == 0
    assert _payload(capsys)["drafted"] == 1


def test_cli_lanes_dry_run_tihna_full_exits_0(tmp_path, capsys):
    code, _ = _invoke(
        tmp_path,
        ["dry-run", "tihna", "--stage", "full"],
    )
    assert code == 0
    payload = _payload(capsys)
    assert payload["success"] is True
    assert payload["kanban_writes"] == 0


def test_cli_lanes_dry_run_unknown_lane_exits_1(tmp_path, capsys):
    code, _ = _invoke(
        tmp_path,
        ["dry-run", "unknown", "--stage", "full"],
    )
    assert code == 1
    assert _payload(capsys)["success"] is False
