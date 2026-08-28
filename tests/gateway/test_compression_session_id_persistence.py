"""Behavioral coverage for agent-result compression route publication."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        user_id="12345",
    )


def _event() -> MessageEvent:
    return MessageEvent(
        text="compress during this turn",
        source=_source(),
        message_id="m1",
    )


def _runner(monkeypatch, tmp_path):
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source_arg: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = None
    runner._recover_telegram_topic_thread_id = lambda _source_arg: None
    runner._cache_session_source = lambda _key, _source_arg: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._reply_anchor_for_event = lambda _event_arg: None
    runner._get_guild_id = lambda _event_arg: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    entry = runner.session_store.get_or_create_session(_source())
    runner.session_store.load_transcript = MagicMock(return_value=[])
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._sync_telegram_topic_binding = MagicMock()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "fake"},
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    return runner, entry


@pytest.mark.asyncio
async def test_agent_result_rotation_invalidates_clarify_before_lease_rebind(
    monkeypatch,
    tmp_path,
):
    """The live post-agent path CAS-advances before using the rotated route."""
    from tools import clarify_gateway as cm

    runner, entry = _runner(monkeypatch, tmp_path)
    original_session_id = entry.session_id
    target_session_id = "agent-compression-child"
    pending = cm.register(
        "agent-result-pending",
        entry.session_key,
        "Pick",
        ["A"],
        origin=cm.ClarifyOrigin("12345", "-1001"),
        session_id=original_session_id,
        active_session_transaction=lambda action: runner.session_store.run_if_session_current(
            entry.session_key,
            original_session_id,
            action,
        ),
    )
    observations = []

    def _observe_rebind(_key, _generation, new_session_id):
        observations.append(
            (
                new_session_id,
                runner.session_store.peek_session_id(entry.session_key),
                cm.resolve_bound_choice(
                    pending.clarify_id,
                    0,
                    binding=pending.binding,
                    observed_origin=pending.binding.origin,
                ),
            )
        )

    runner._rebind_turn_lease = _observe_rebind
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "done",
            "messages": [
                {"role": "user", "content": "compress during this turn"},
                {"role": "assistant", "content": "done"},
            ],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "session_id": target_session_id,
            "api_calls": 1,
            "failed": False,
        }
    )

    response = await runner._handle_message_with_agent(
        _event(),
        _source(),
        entry.session_key,
        1,
    )

    assert response == "done"
    assert observations == [(target_session_id, target_session_id, False)]
    assert runner.session_store.peek_session_id(entry.session_key) == target_session_id
    assert pending.event.is_set()
    assert pending.response == ""
