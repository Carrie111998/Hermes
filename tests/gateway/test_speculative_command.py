from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


class Manager:
    def __init__(self):
        self.invalidated = []

    def invalidate_session(self, session_id):
        self.invalidated.append(session_id)


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
