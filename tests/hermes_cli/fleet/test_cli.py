from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

from hermes_cli.fleet.config import parse_fleet_config
from hermes_cli.fleet.service import FleetService
from hermes_cli.fleet.state import FleetStore
from hermes_cli.subcommands.fleet import (
    EXIT_DISABLED,
    EXIT_NO_ROUTE,
    build_fleet_parser,
    fleet_command,
)

from test_service import _service


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes")
    build_fleet_parser(parser.add_subparsers(dest="command"))
    return parser


def test_help_exposes_only_the_bounded_v1_surface(capsys):
    parser = _parser()

    with pytest.raises(SystemExit) as caught:
        parser.parse_args(["fleet", "--help"])

    assert caught.value.code == 0
    output = capsys.readouterr().out
    for command in ("doctor", "plan", "run", "status", "audit", "release", "refresh-usage"):
        assert command in output
    assert "continue" not in output


def test_real_main_parser_keeps_fleet_registered(
    tmp_path, capsys, monkeypatch
):
    import hermes_cli.main as main_module
    import hermes_cli.subcommands.fleet as fleet_subcommand

    service, _, _ = _service(tmp_path)
    monkeypatch.setattr(main_module, "_set_process_title", lambda: None)
    monkeypatch.setattr(main_module, "_cleanup_quarantined_exes", lambda: None)
    monkeypatch.setattr(
        main_module,
        "_recover_from_interrupted_install",
        lambda: None,
    )
    monkeypatch.setattr(fleet_subcommand, "_default_service", lambda: service)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "fleet", "status", "--json"],
    )

    main_module.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "status"
    assert set(payload["purposes"]) == {"task_worker", "desktop_parent"}
    assert "fleet" in main_module._BUILTIN_SUBCOMMANDS


def test_refresh_usage_json_reports_measured_numeric_claude_lane(
    tmp_path, capsys, monkeypatch
):
    from agent import account_usage

    token = "cc-synthetic-cli-refresh-token-never-real"
    http_calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "seven_day": {"utilization": 0.14},
                "seven_day_opus": {"utilization": 0.22},
            }

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def get(self, url, headers, **kwargs):
            http_calls.append(
                {"method": "GET", "url": url, "headers": headers, "kwargs": kwargs}
            )
            return Response()

        def post(self, *_args, **_kwargs):
            raise AssertionError("CLI refresh must never invoke Anthropic inference")

    monkeypatch.setattr(account_usage, "resolve_anthropic_token", lambda: token)
    monkeypatch.setattr(account_usage, "_fetch_codex_account_usage", lambda **_kwargs: None)
    monkeypatch.setattr(account_usage.httpx, "Client", lambda timeout: Client())
    monkeypatch.setattr(
        "hermes_cli.fleet.usage_refresh._probe_console_lane_health",
        lambda lane_id: (None, f"{lane_id} disabled in CLI contract test"),
    )
    path = tmp_path / "usage-weekly.json"
    args = _parser().parse_args(
        [
            "fleet",
            "refresh-usage",
            "--path",
            str(path),
            "--no-mirror",
            "--json",
        ]
    )
    service, _, _ = _service(tmp_path)

    code = fleet_command(args, service=service)
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert code == 0
    claude = next(lane for lane in payload["lanes"] if lane["lane_id"] == "claude_code")
    assert claude["updated"] is True
    assert claude["weekly_pct_used"] == 22.0
    assert claude["checked_at"] is not None
    assert claude["detail"] == "ok"
    assert payload["ok"] is True
    assert token not in output
    assert [call["method"] for call in http_calls] == ["GET"]


def test_plan_json_is_stable_provenance_rich_and_read_only(tmp_path, capsys):
    service, _, _ = _service(tmp_path)
    task = tmp_path / "task.txt"
    task.write_text("bounded task", encoding="utf-8")
    args = _parser().parse_args(
        [
            "fleet",
            "plan",
            "--task-file",
            str(task),
            "--cwd",
            str(tmp_path),
            "--json",
        ]
    )

    code = fleet_command(args, service=service)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["schema_version"] == 1
    assert payload["command"] == "plan"
    assert payload["ok"] is True
    assert payload["selected"]["lane_id"] == "chatgpt_codex"
    capacity = payload["evaluations"][0]["capacity"]
    assert capacity["source_kind"] == "bridge_file"
    assert capacity["source_id"]
    assert capacity["source_hash"]
    assert capacity["captured_at"]
    assert capacity["read_at"]
    assert capacity["expires_at"]
    assert capacity["freshness"] == "fresh"
    assert capacity["confidence"] == "high"
    assert capacity["remaining_pct"] == "80.000"
    assert capacity["reserved_pct"] == "0.000"
    assert capacity["effective_remaining_pct"] == "80.000"
    assert not service.store.path.exists()


def test_human_plan_names_lane_adapter_and_capacity_provenance(tmp_path, capsys):
    service, _, _ = _service(tmp_path)
    task = tmp_path / "task.txt"
    task.write_text("bounded task", encoding="utf-8")
    args = _parser().parse_args(
        ["fleet", "plan", "--task-file", str(task)]
    )

    code = fleet_command(args, service=service)
    output = capsys.readouterr().out

    assert code == 0
    assert "chatgpt_codex" in output
    assert "native_provider" in output
    assert "bridge_file" in output
    assert "fresh" in output
    assert "high" in output


def test_run_disabled_has_dedicated_exit_code_and_no_state(tmp_path, capsys):
    service, _, _ = _service(tmp_path, enabled=False)
    task = tmp_path / "task.txt"
    task.write_text("bounded task", encoding="utf-8")
    args = _parser().parse_args(
        ["fleet", "run", "--task-file", str(task), "--json"]
    )

    code = fleet_command(args, service=service)
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_DISABLED
    assert payload["reason"] == "FLEET_DISABLED"
    assert not service.store.path.exists()


def test_doctor_no_eligible_lane_is_nonzero_with_complete_matrix(tmp_path, capsys):
    service, _, _ = _service(tmp_path)
    service.qualifications.clear()
    args = _parser().parse_args(["fleet", "doctor", "--json"])

    code = fleet_command(args, service=service)
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_NO_ROUTE
    assert payload["ok"] is False
    assert len(payload["evaluations"]) == 2
    assert all(item["reasons"] for item in payload["evaluations"])
    assert all(
        item["selectable"] == (item["eligible"] or item["fallback_eligible"])
        for item in payload["evaluations"]
    )


def test_status_is_read_only_even_when_no_lane_is_eligible(tmp_path, capsys):
    service, _, _ = _service(tmp_path)
    service.qualifications.clear()
    args = _parser().parse_args(["fleet", "status", "--json"])

    code = fleet_command(args, service=service)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["reason"] == "NO_ELIGIBLE_LANE"
    assert not service.store.path.exists()


def test_human_status_names_parent_and_worker_matrices(tmp_path, capsys):
    service, _, _ = _service(tmp_path)
    args = _parser().parse_args(["fleet", "status"])

    assert fleet_command(args, service=service) == 0

    output = capsys.readouterr().out
    assert "[task_worker]" in output
    assert "[desktop_parent]" in output
    assert "adapter=native_provider" in output
    assert "model=" in output
    assert "pins task_worker=0 desktop_parent=0" in output
    assert not service.store.path.exists()


def test_skill_and_user_docs_distinguish_parent_from_worker_routes():
    root = Path(__file__).parents[3]
    skill = (
        root
        / "skills"
        / "autonomous-ai-agents"
        / "fleet-balanced-router"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    docs = (
        root / "docs" / "user-guide" / "features" / "fleet-balanced-router.md"
    ).read_text(encoding="utf-8")

    assert "do not call `hermes fleet run` to readmit or replace it" in skill
    assert "`task_worker`" in skill
    assert "`desktop_parent`" in skill
    assert "Claude Code OAuth" in docs
    assert "Claude Opus 4.8" in docs
    assert "Antigravity" in docs
    assert "parent-ineligible" in docs
    assert "GOOGLE_API_KEY" in docs
    assert "GEMINI_API_KEY" in docs


def test_run_and_filtered_jsonl_audit_use_real_store(tmp_path, capsys):
    service, adapter, _ = _service(tmp_path)
    task = tmp_path / "task.txt"
    task.write_text("bounded task", encoding="utf-8")
    run_args = _parser().parse_args(
        [
            "fleet",
            "run",
            "--task-file",
            str(task),
            "--task-id",
            "bfdb2ca5-9d89-41c5-a8ff-60fb1f552001",
            "--json",
        ]
    )
    assert fleet_command(run_args, service=service) == 0
    run_payload = json.loads(capsys.readouterr().out)
    assert run_payload["output"] == "worker complete"
    assert len(adapter.calls) == 1

    audit_args = _parser().parse_args(
        [
            "fleet",
            "audit",
            "--task-id",
            "bfdb2ca5-9d89-41c5-a8ff-60fb1f552001",
            "--reason",
            "MET",
            "--jsonl",
        ]
    )
    assert fleet_command(audit_args, service=service) == 0
    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert lines
    assert all(
        event["task_id"] == "bfdb2ca5-9d89-41c5-a8ff-60fb1f552001"
        and event["reason_code"] == "MET"
        for event in lines
    )
    serialized = json.dumps(lines)
    assert "bounded task" not in serialized
    assert "worker complete" not in serialized


def test_release_is_idempotent_and_reports_missing_live_lease(tmp_path, capsys):
    service, _, _ = _service(tmp_path)
    args = _parser().parse_args(
        ["fleet", "release", "missing-task", "--outcome", "cancelled", "--json"]
    )

    code = fleet_command(args, service=service)
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_NO_ROUTE
    assert payload["released"] is False
