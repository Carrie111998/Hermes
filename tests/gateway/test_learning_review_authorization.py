"""Authorization contracts for profile-global learning review commands."""

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="ordinary-user",
            chat_id="room",
        ),
    )


def _runner(*, admin: bool):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._resume_caller_is_admin = lambda source: admin
    runner._session_key_for_source = lambda source: "test-session"
    runner._evict_cached_agent = lambda session_key: None
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "command"),
    [
        ("_handle_memory_command", "/memory pending"),
        ("_handle_skills_command", "/skills pending"),
    ],
)
async def test_non_admin_cannot_access_profile_global_learning_queue(
    tmp_path, monkeypatch, handler, command
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    result = await getattr(_runner(admin=False), handler)(_event(command))

    assert "admin" in result.lower()
    assert "pending" not in result.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "subcommand",
    ["history", "ledger", "audit", "compile", "compilations", "eval missing", "evaluate missing", "rollback missing"],
)
async def test_admin_can_reach_each_learning_command_with_skill_gate_off(
    tmp_path, monkeypatch, subcommand
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    result = await _runner(admin=True)._handle_skills_command(
        _event(f"/skills {subcommand}")
    )

    assert "approval is off" not in result.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "command"),
    [
        ("_handle_memory_command", "/memory approval on"),
        ("_handle_skills_command", "/skills approval on"),
    ],
)
async def test_gateway_toggle_writes_active_profile_config(
    tmp_path, monkeypatch, handler, command
):
    profile_home = tmp_path / ".hermes" / "profiles" / "other"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    result = await getattr(_runner(admin=True), handler)(_event(command))

    assert "on" in result.lower()
    assert (profile_home / "config.yaml").exists()
    assert not (tmp_path / ".hermes" / "config.yaml").exists()
