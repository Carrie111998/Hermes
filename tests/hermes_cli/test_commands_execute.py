"""Invariant tests for registry-owned slash execution (CommandDef.execute)."""

from __future__ import annotations

import json
from pathlib import Path

from unittest.mock import patch

import pytest

from hermes_cli.commands import COMMAND_REGISTRY, resolve_command
from hermes_cli.slash_exec import (
    CommandContext,
    CommandReply,
    execute_command,
    get_executor_keys,
    resolve_executor,
    run_execute,
)

MIGRATED = frozenset(get_executor_keys())
_FAST_SURFACE_CMDS = ("version", "egress", "profile", "bundles", "status", "help")


def test_registry_matches_executor_keys() -> None:
    registry_executes = {cmd.execute for cmd in COMMAND_REGISTRY if cmd.execute}
    assert registry_executes == MIGRATED


def test_unmigrated_commands_have_no_executor() -> None:
    for cmd in COMMAND_REGISTRY:
        key = cmd.execute
        if not key or key not in MIGRATED:
            assert resolve_executor(cmd) is None
            assert run_execute(cmd, CommandContext()) is None


def test_every_executor_key_is_registered() -> None:
    for key in MIGRATED:
        cmd = next((c for c in COMMAND_REGISTRY if c.execute == key), None)
        assert cmd is not None
        assert resolve_executor(cmd) is not None


def test_all_executors_resolve() -> None:
    for cmd in COMMAND_REGISTRY:
        if not cmd.execute:
            continue
        assert resolve_executor(cmd) is not None
        # Do not execute here: several migrated commands spawn real
        # subprocesses (briefing/backup/cleanup/health/logs/dashboard/...).
        # Running them in pytest hangs on a clean test env.
        # They are wired separately via the CLI/Gateway dispatch fallbacks.


@pytest.mark.parametrize("cmd_name", _FAST_SURFACE_CMDS)
def test_executors_are_surface_invariant(cmd_name: str) -> None:
    cmd = resolve_command(cmd_name)
    assert cmd is not None
    assert resolve_executor(cmd) is not None

    def run(name: str, surface: str) -> CommandReply:
        return execute_command(name, CommandContext(surface=surface))

    base = run(cmd_name, "cli")
    for surface in ("gateway", "tui"):
        other = run(cmd_name, surface)
        assert other.text == base.text
        assert other.format == base.format


def _write_gateway_state(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    state = {
        "gateway_state": "online",
        "platforms": {
            "telegram": {
                "state": "connected",
            }
        },
    }
    (root / "gateway_state.json").write_text(
        json.dumps(state, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "hardware_monitor_state.json").write_text(
        json.dumps({"cpu_last_alert": None, "mem_last_alert": None}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "visual_capture_state.json").write_text(
        json.dumps({"last_capture": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "agent.log").write_text("", encoding="utf-8")
    (logs / "gateway.log").write_text("", encoding="utf-8")


_SUBPROCESS_CMDS = (
    "briefing",
    "backup",
    "cleanup",
    "health",
    "logs",
    "dashboard",
    "capture",
    "listen",
    "voicectl",
)


def _executor_key_for(cmd_name: str) -> str:
    cmd = resolve_command(cmd_name)
    assert cmd is not None
    assert cmd.execute
    return cmd.execute


def test_registry_matches_executor_keys() -> None:
    registry_executes = {cmd.execute for cmd in COMMAND_REGISTRY if cmd.execute}
    assert registry_executes == MIGRATED


def test_unmigrated_commands_have_no_executor() -> None:
    for cmd in COMMAND_REGISTRY:
        key = cmd.execute
        if not key or key not in MIGRATED:
            assert resolve_executor(cmd) is None
            assert run_execute(cmd, CommandContext()) is None


def test_every_executor_key_is_registered() -> None:
    for key in MIGRATED:
        cmd = next((c for c in COMMAND_REGISTRY if c.execute == key), None)
        assert cmd is not None
        assert resolve_executor(cmd) is not None


def test_all_executors_resolve() -> None:
    for cmd in COMMAND_REGISTRY:
        if not cmd.execute:
            continue
        assert resolve_executor(cmd) is not None
        # Do not execute here: several migrated commands spawn real
        # subprocesses (briefing/backup/cleanup/health/logs/dashboard/...).
        # Running them in pytest hangs on a clean test env.
        # They are wired separately via the CLI/Gateway dispatch fallbacks.


@pytest.mark.parametrize("cmd_name", _FAST_SURFACE_CMDS)
def test_executors_are_surface_invariant(cmd_name: str) -> None:
    cmd = resolve_command(cmd_name)
    assert cmd is not None
    assert resolve_executor(cmd) is not None

    def run(name: str, surface: str) -> CommandReply:
        return execute_command(name, CommandContext(surface=surface))

    base = run(cmd_name, "cli")
    for surface in ("gateway", "tui"):
        other = run(cmd_name, surface)
        assert other.text == base.text
        assert other.format == base.format


def _write_gateway_state(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    state = {
        "gateway_state": "online",
        "platforms": {
            "telegram": {
                "state": "connected",
            }
        },
    }
    (root / "gateway_state.json").write_text(
        json.dumps(state, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "hardware_monitor_state.json").write_text(
        json.dumps({"cpu_last_alert": None, "mem_last_alert": None}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "visual_capture_state.json").write_text(
        json.dumps({"last_capture": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "agent.log").write_text("", encoding="utf-8")
    (logs / "gateway.log").write_text("", encoding="utf-8")


def test_smoke_file_executors_under_fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_gateway_state(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    for name in _FAST_SURFACE_CMDS:
        reply = execute_command(name, CommandContext())
        assert isinstance(reply, CommandReply)
        assert isinstance(reply.text, str)
        assert reply.text.strip() or reply.data


@pytest.mark.parametrize("cmd_name", _SUBPROCESS_CMDS)
def test_subprocess_executors_are_mocked_and_return(cmd_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Subprocess-based executors must return a CommandReply and be registered."""
    cmd = resolve_command(cmd_name)
    assert cmd is not None
    key = _executor_key_for(cmd_name)
    assert key in MIGRATED

    fake_reply = CommandReply("mocked-output", format="plain")

    import hermes_cli.slash_exec as slash_exec

    original = slash_exec.EXECUTORS[key]
    slash_exec.EXECUTORS[key] = lambda ctx: fake_reply
    try:
        reply = execute_command(cmd_name, CommandContext())
    finally:
        slash_exec.EXECUTORS[key] = original

    assert isinstance(reply, CommandReply)
    assert isinstance(reply.text, str)
    assert reply.text == "mocked-output"
