"""Agent-originated model-routing config writes require explicit consent."""

from __future__ import annotations

import argparse
import json
import threading
import time
from unittest.mock import MagicMock

import tools.approval as approval
import tools.terminal_tool as terminal_tool
from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.subcommands.config import build_config_parser


def _allow_tirith(monkeypatch) -> None:
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
    )


def test_model_config_confirm_defaults_on() -> None:
    assert DEFAULT_CONFIG["approvals"]["model_config_confirm"] is True


def test_detects_only_model_routing_config_set_commands() -> None:
    protected = {
        "hermes config set model.default gpt-5": "model.default",
        "hermes config set --force model.provider openrouter": "model.provider",
        "hermes -p coder config set delegation.model sonnet": "delegation.model",
        "hermes config set delegation.provider anthropic": "delegation.provider",
        "env hermes config set model.default gpt-5": "model.default",
        "echo ready && hermes config set model.provider openai": "model.provider",
        "hermes config unset delegation.model": "delegation.model",
        "hermes config set auxiliary.title_generation.model fast":
            "auxiliary.title_generation.model",
        "hermes config set auxiliary.vision.base_url https://example.test":
            "auxiliary.vision.base_url",
    }
    for command, key in protected.items():
        change = approval.detect_model_config_change(command)
        assert change is not None, command
        assert change.key == key
        assert change.user_directed is False

    for command in (
        "hermes config get delegation.model",
        "hermes config set display.skin mono",
        "echo hermes config set model.default gpt-5",
        "printf 'hermes config set model.default gpt-5'",
        "hermes config set auxiliary.vision.timeout 30",
    ):
        assert approval.detect_model_config_change(command) is None, command


def test_yes_marks_explicit_user_direction() -> None:
    change = approval.detect_model_config_change(
        "hermes config set --yes delegation.model sonnet"
    )
    assert change is not None
    assert change.user_directed is True


def test_model_config_gate_survives_approvals_off(monkeypatch) -> None:
    _allow_tirith(monkeypatch)
    monkeypatch.setattr(
        approval,
        "_get_approval_config",
        lambda: {"mode": "off", "model_config_confirm": True},
    )
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    token = approval.set_hermes_interactive_context(True)
    seen = []
    try:
        result = approval.check_all_command_guards(
            "hermes config set delegation.model sonnet",
            "local",
            approval_callback=lambda command, description, **kwargs: (
                seen.append((command, description, kwargs)) or "once"
            ),
        )
    finally:
        approval.reset_hermes_interactive_context(token)

    assert result["approved"] is True
    assert result["user_approved"] is True
    assert seen
    assert "delegation.model" in seen[0][1]


def test_user_directed_or_opted_out_writes_skip_model_gate(monkeypatch) -> None:
    _allow_tirith(monkeypatch)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    token = approval.set_hermes_interactive_context(True)
    try:
        for command, config in (
            (
                "hermes config set --yes delegation.model sonnet",
                {"mode": "off", "model_config_confirm": True},
            ),
            (
                "hermes config set delegation.model sonnet",
                {"mode": "off", "model_config_confirm": False},
            ),
        ):
            monkeypatch.setattr(approval, "_get_approval_config", lambda config=config: config)
            result = approval.check_all_command_guards(
                command,
                "local",
                approval_callback=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("approval callback should not run")
                ),
            )
            assert result == {"approved": True, "message": None}
    finally:
        approval.reset_hermes_interactive_context(token)


def test_always_approve_disables_future_model_config_confirmation(monkeypatch) -> None:
    _allow_tirith(monkeypatch)
    monkeypatch.setattr(
        approval,
        "_get_approval_config",
        lambda: {"mode": "manual", "model_config_confirm": True},
    )
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    saved = []
    monkeypatch.setattr(
        "hermes_cli.config.set_config_value",
        lambda key, value: saved.append((key, value)),
    )
    token = approval.set_hermes_interactive_context(True)
    try:
        result = approval.check_all_command_guards(
            "hermes config set delegation.provider anthropic",
            "local",
            approval_callback=lambda *_args, **_kwargs: "always",
        )
    finally:
        approval.reset_hermes_interactive_context(token)

    assert result["approved"] is True
    assert saved == [("approvals.model_config_confirm", "false")]


def test_gateway_receives_model_config_confirmation_when_mode_is_off(monkeypatch) -> None:
    _allow_tirith(monkeypatch)
    session_key = "model-config-gateway"
    approval.clear_session(session_key)
    approval._gateway_queues.clear()
    approval._gateway_notify_cbs.clear()
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.setenv("HERMES_SESSION_KEY", session_key)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setattr(
        approval,
        "_get_approval_config",
        lambda: {"mode": "off", "model_config_confirm": True, "timeout": 5},
    )
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    notified = []
    approval.register_gateway_notify(session_key, notified.append)
    result_holder = {}

    thread = threading.Thread(
        target=lambda: result_holder.setdefault(
            "result",
            approval.check_all_command_guards(
                "hermes config set model.default gpt-5", "local"
            ),
        )
    )
    thread.start()
    for _ in range(200):
        if approval._gateway_queues.get(session_key):
            break
        time.sleep(0.005)
    approval.resolve_gateway_approval(session_key, "once")
    thread.join(timeout=5)

    assert result_holder["result"]["approved"] is True
    assert notified
    assert "model.default" in notified[0]["description"]


def test_config_set_parser_accepts_user_directed_yes_flag() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_config_parser(subparsers, cmd_config=lambda _args: None)

    args = parser.parse_args(
        ["config", "set", "--yes", "delegation.model", "sonnet"]
    )
    assert args.yes is True
    assert args.key == "delegation.model"

    args = parser.parse_args(
        ["config", "unset", "--yes", "delegation.model"]
    )
    assert args.yes is True
    assert args.key == "delegation.model"


def test_terminal_result_keeps_model_config_change_visible(monkeypatch, tmp_path) -> None:
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "saved", "returncode": 0}
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {
            "env_type": "local",
            "timeout": 30,
            "cwd": str(tmp_path),
            "host_cwd": None,
            "modal_mode": "auto",
            "docker_image": "",
            "singularity_image": "",
            "modal_image": "",
            "daytona_image": "",
        },
    )
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_tool, "_check_all_guards", lambda *_args, **_kwargs: {"approved": True}
    )
    monkeypatch.setitem(terminal_tool._active_environments, "default", mock_env)
    monkeypatch.setitem(terminal_tool._last_activity, "default", 0.0)

    result = json.loads(
        terminal_tool.terminal_tool(
            command="hermes config set --yes delegation.model sonnet"
        )
    )

    assert result["exit_code"] == 0
    assert result["notice"] == (
        "⚠ Agent changed model-routing config: delegation.model."
    )
    assert result["output"].startswith(result["notice"])