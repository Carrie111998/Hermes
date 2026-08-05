from __future__ import annotations

from datetime import datetime
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


class Manager:
    def __init__(self):
        self.invalidated = []
        self.installed = 0

    def invalidate_session(self, session_id):
        self.invalidated.append(session_id)


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="dm",
        user_id="user-1",
        user_name="tester",
    )


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat-1",
            chat_type="dm",
            user_id="user-1",
        ),
    )


@pytest.mark.asyncio
async def test_gateway_speculative_switch_reports_and_invalidates_cached_agent(monkeypatch):
    from gateway.run import GatewayRunner
    import agent.speculative_compression as speculative

    manager = Manager()
    agent = SimpleNamespace(
        compression_enabled=True,
        speculative_compression_enabled=False,
        context_compressor=SimpleNamespace(),
        api_mode="chat_completions",
        session_id="session-1",
        _speculative_compression_manager=None,
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._agent_cache = {"session-key": (agent, "signature")}
    runner._agent_cache_lock = threading.Lock()
    runner._session_key_for_source = lambda _source: "session-key"

    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"compression": {"speculative": {"enabled": False}}},
    )
    monkeypatch.setattr(speculative, "get_default_manager", lambda: manager)
    monkeypatch.setattr(
        speculative,
        "is_builtin_compression_eligible",
        lambda **_kwargs: True,
    )

    enabled_reply = await runner._handle_speculative_command(_event("/speculative on"))
    assert agent.speculative_compression_enabled is True
    assert "enabled=True" in enabled_reply
    assert "eligible=True" in enabled_reply
    assert "api_mode=chat_completions" in enabled_reply
    assert "context_engine=SimpleNamespace" in enabled_reply

    disabled_reply = await runner._handle_speculative_command(_event("/speculative off"))
    assert agent.speculative_compression_enabled is False
    assert manager.invalidated == ["session-1"]
    assert "enabled=False" in disabled_reply
    assert "install_status=none" in disabled_reply


def _busy_runner():
    from gateway.run import GatewayRunner

    source = _source()
    entry = SessionEntry(
        session_key=build_session_key(source),
        session_id="session-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner, source


@pytest.mark.asyncio
async def test_speculative_off_dispatches_mid_turn_and_replies_authoritatively():
    """The busy gateway path must reach the real switch handler."""
    runner, source = _busy_runner()
    manager = Manager()
    agent = SimpleNamespace(
        compression_enabled=True,
        speculative_compression_enabled=True,
        context_compressor=SimpleNamespace(),
        api_mode="chat_completions",
        session_id="session-1",
        _speculative_compression_manager=manager,
        _speculative_install_status="none",
    )
    session_key = build_session_key(source)
    runner._running_agents[session_key] = agent

    reply = await runner._handle_message(_event("/speculative off"))

    assert "enabled=False" in reply
    assert "eligible=False" in reply
    assert "install_status=none" in reply
    assert "runtime_override=False" in reply
    assert agent.speculative_compression_enabled is False
    assert manager.invalidated == ["session-1"]
    assert manager.installed == 0


def test_speculative_is_available_through_gateway_command_allowlist():
    """The generic slash allowlist admits the canonical command name."""
    from gateway.slash_access import policy_for_source

    config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                token="***",
                extra={
                    "allow_admin_from": ["admin"],
                    "user_allowed_commands": ["speculative"],
                },
            )
        }
    )

    policy = policy_for_source(config, _source())
    assert policy.enabled is True
    assert policy.can_run("user-1", "speculative") is True
