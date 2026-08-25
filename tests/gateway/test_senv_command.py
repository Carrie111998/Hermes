"""Gateway /senv is pre-model and never echoes secret values."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent
from gateway.session import SessionSource
from hermes_cli.senv import adapter_can_delete_user_message

SECRET = "live-senv-secret-value-not-for-model"


def _make_source(*, profile: str = "") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
        profile=profile,
    )


def _make_event(text: str, *, profile: str = "") -> MessageEvent:
    return MessageEvent(
        text=text,
        source=_make_source(profile=profile),
        message_id="m-senv",
    )


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner._adapter_for_source = lambda _source: None
    runner._resolve_profile_home_for_source = lambda _source: None
    runner._run_agent = MagicMock(side_effect=AssertionError("model must not run for /senv"))
    runner._handle_message_with_agent = MagicMock(
        side_effect=AssertionError("agent path must not run for /senv")
    )
    return runner


@pytest.mark.asyncio
async def test_senv_set_writes_active_profile_env_without_echo(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_cli.senv.get_hermes_home", lambda: home)

    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "get_env_path", lambda: home / ".env")
    monkeypatch.setattr(config_mod, "is_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.managed_scope.is_env_managed", lambda _key: False)

    runner = _make_runner()
    model_called = {"n": 0}

    def _boom(*_a, **_k):
        model_called["n"] += 1
        raise AssertionError("model must not run for /senv")

    runner._run_agent = _boom

    event = _make_event(f"/senv main BOOKING_PASSWORD={SECRET}")
    text = await runner._handle_senv_command(event)

    assert SECRET not in text
    assert "BOOKING_PASSWORD" in text
    assert model_called["n"] == 0
    stored = (home / ".env").read_text()
    assert f"BOOKING_PASSWORD={SECRET}" in stored
    assert SECRET not in text


@pytest.mark.asyncio
async def test_senv_multiplex_writes_stamped_profile_not_root(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    profile_home = root / "profiles" / "milo"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from gateway.run import _profile_runtime_scope

    def _resolve(_source):
        return profile_home

    runner = _make_runner()
    runner.config.multiplex_profiles = True
    runner._resolve_profile_home_for_source = _resolve

    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "is_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.managed_scope.is_env_managed", lambda _key: False)

    event = _make_event(f"/senv OPENROUTER_API_KEY={SECRET}", profile="milo")
    with _profile_runtime_scope(profile_home):
        # Handler installs its own scope; this just proves the helper exists.
        pass
    text = await runner._handle_senv_command(event)

    assert SECRET not in text
    assert (root / ".env").exists() is False
    profile_env = profile_home / ".env"
    assert profile_env.is_file()
    assert SECRET in profile_env.read_text()
    assert SECRET not in text


@pytest.mark.asyncio
async def test_senv_delete_unavailable_falls_back_to_manual_hint(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_cli.senv.get_hermes_home", lambda: home)
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "get_env_path", lambda: home / ".env")
    monkeypatch.setattr(config_mod, "is_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.managed_scope.is_env_managed", lambda _key: False)

    class _NoDelete(BasePlatformAdapter):
        def __init__(self):
            pass

        async def connect(self, *, is_reconnect=False):
            return True

        async def disconnect(self):
            return None

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return None

        async def get_chat_info(self, chat_id):
            return {"name": "test", "type": "dm"}

    runner = _make_runner()
    adapter = _NoDelete()
    assert adapter_can_delete_user_message(adapter) is False
    runner._adapter_for_source = lambda _source: adapter
    text = await runner._handle_senv_command(
        _make_event(f"/senv KEY={SECRET}")
    )
    assert SECRET not in text
    assert "delete your original message" in text.lower()


@pytest.mark.asyncio
async def test_senv_multiplex_unresolved_home_does_not_write(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr("hermes_cli.senv.get_hermes_home", lambda: root)
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "get_env_path", lambda: root / ".env")
    monkeypatch.setattr(config_mod, "is_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.managed_scope.is_env_managed", lambda _key: False)

    runner = _make_runner()
    runner.config.multiplex_profiles = True
    runner._resolve_profile_home_for_source = lambda _source: None

    text = await runner._handle_senv_command(
        _make_event(f"/senv LEAK_KEY={SECRET}", profile="milo")
    )
    assert "not saved" in text.lower()
    assert SECRET not in text
    assert (root / ".env").exists() is False


@pytest.mark.asyncio
async def test_senv_does_not_persist_transcript_tools_or_logs(
    tmp_path, monkeypatch, caplog
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_cli.senv.get_hermes_home", lambda: home)
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "get_env_path", lambda: home / ".env")
    monkeypatch.setattr(config_mod, "is_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.managed_scope.is_env_managed", lambda _key: False)

    tool_calls = {"n": 0}

    def _fake_tool(*_a, **_k):
        tool_calls["n"] += 1
        raise AssertionError("senv must not produce tool results")

    monkeypatch.setattr("model_tools.handle_function_call", _fake_tool, raising=False)

    runner = _make_runner()
    with caplog.at_level(logging.DEBUG):
        text = await runner._handle_senv_command(
            _make_event(f"/senv main BOOKING_PASSWORD={SECRET}")
        )

    assert SECRET not in text
    assert SECRET not in caplog.text
    assert tool_calls["n"] == 0
    runner.session_store.append_to_transcript.assert_not_called()
    runner._run_agent.assert_not_called()
    runner._handle_message_with_agent.assert_not_called()


@pytest.mark.asyncio
async def test_senv_busy_dispatch_uses_handler_not_agent(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_cli.senv.get_hermes_home", lambda: home)
    from hermes_cli import config as config_mod
    from hermes_cli.commands import is_gateway_known_command, resolve_command

    monkeypatch.setattr(config_mod, "get_env_path", lambda: home / ".env")
    monkeypatch.setattr(config_mod, "is_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.managed_scope.is_env_managed", lambda _key: False)

    cmd = resolve_command("senv")
    assert cmd is not None
    assert cmd.name == "senv"
    assert is_gateway_known_command("senv")

    runner = _make_runner()
    event = _make_event(f"/senv main BOOKING_PASSWORD={SECRET}")
    text = await runner._dispatch_busy_slash_command(
        event, cmd, "telegram:c1", event.source
    )
    assert SECRET not in text
    assert "BOOKING_PASSWORD" in text
    runner._run_agent.assert_not_called()
    runner._handle_message_with_agent.assert_not_called()
