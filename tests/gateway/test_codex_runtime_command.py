"""Gateway behavior contracts for the /codex-runtime command."""

from unittest.mock import MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_cli import codex_runtime_switch as crs


def _make_event(text: str = "/codex-runtime on") -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="user-1",
            chat_id="chat-1",
            user_name="tester",
            chat_type="dm",
        ),
    )


@pytest.mark.asyncio
async def test_runtime_change_preserves_cached_agent_until_new_session(monkeypatch):
    """Persisting a runtime must not replace the current conversation owner."""
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._evict_cached_agent = MagicMock()
    event = _make_event()
    session_key = runner._session_key_for_source(event.source)
    cached_agent = object()
    runner._agent_cache = {session_key: (cached_agent, 0.0)}

    persisted = []
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.config.save_config",
        lambda config: persisted.append(config),
    )

    def fake_apply(config, new_value, *, persist_callback):
        config["model"] = {"openai_runtime": new_value}
        persist_callback(config)
        return crs.CodexRuntimeStatus(
            success=True,
            new_value=new_value,
            old_value="auto",
            message=(
                "Saved for the next session. "
                "Run `/new` or `/reset` to apply this change."
            ),
            requires_new_session=True,
        )

    monkeypatch.setattr(crs, "apply", fake_apply)

    result = await runner._handle_codex_runtime_command(event)

    assert persisted == [{"model": {"openai_runtime": "codex_app_server"}}]
    runner._evict_cached_agent.assert_not_called()
    assert runner._agent_cache[session_key][0] is cached_agent
    assert "/new" in result
    assert "/reset" in result
