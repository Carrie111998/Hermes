"""Abort superseded turns before run_conversation when /stop hits pending setup.

When a turn still holds only ``_AGENT_PENDING_SENTINEL``, ``/stop`` / ``/new``
can invalidate the run generation but cannot call ``agent.interrupt()``.
Without a generation gate after setup (STT / media / hygiene) and before
``run_conversation``, the stopped turn still executes tools and LLM work;
only the final delivery is discarded afterward, and ``track_agent`` leaves the
stale agent invisible to busy guards.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource


SESSION_KEY = "agent:main:telegram:dm:12345"
SESSION_ID = "sess-abort-stale"


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="u1",
    )


def _event(text: str = "voice note") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="msg-1",
    )


def _runner(monkeypatch, tmp_path):
    """Minimal GatewayRunner that keeps real run-generation helpers."""
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_run_generation = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = None
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    entry = SessionEntry(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    return runner


@pytest.mark.asyncio
async def test_stale_after_inbound_prep_skips_run_agent(monkeypatch, tmp_path):
    """ /stop during setup must abort before ``_run_agent`` / conversation. """
    runner = _runner(monkeypatch, tmp_path)
    stale_gen = runner._begin_session_run_generation(SESSION_KEY)
    runner._running_agents[SESSION_KEY] = gateway_run._AGENT_PENDING_SENTINEL

    async def _prep_then_stop(*_args, **_kwargs):
        # Simulate /stop while STT / media prep awaits.
        runner._invalidate_session_run_generation(SESSION_KEY, reason="stop")
        runner._running_agents.pop(SESSION_KEY, None)
        return "transcribed text"

    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(
        side_effect=_prep_then_stop
    )
    runner._run_agent = AsyncMock(
        side_effect=AssertionError("stale turn must not enter _run_agent")
    )

    result = await runner._handle_message_with_agent(
        _event(), _source(), SESSION_KEY, stale_gen
    )

    assert result is None
    runner._run_agent.assert_not_called()
    assert not runner._is_session_run_current(SESSION_KEY, stale_gen)


@pytest.mark.asyncio
async def test_current_generation_still_runs_agent(monkeypatch, tmp_path):
    """Uninterrupted turns must still reach ``_run_agent``."""
    runner = _runner(monkeypatch, tmp_path)
    gen = runner._begin_session_run_generation(SESSION_KEY)
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(
        return_value="hello"
    )
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "hi",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "api_calls": 1,
            "failed": False,
        }
    )

    result = await runner._handle_message_with_agent(
        _event("hello"), _source(), SESSION_KEY, gen
    )

    assert result == "hi"
    runner._run_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_agent_bails_before_setup_when_stale(monkeypatch, tmp_path):
    """Direct ``_run_agent`` entry must no-op when generation is already stale."""
    runner = _runner(monkeypatch, tmp_path)
    stale_gen = runner._begin_session_run_generation(SESSION_KEY)
    runner._invalidate_session_run_generation(SESSION_KEY, reason="stop")

    load_calls = {"n": 0}

    def _counting_load():
        load_calls["n"] += 1
        return {}

    monkeypatch.setattr(gateway_run, "_load_gateway_config", _counting_load)

    result = await runner._run_agent(
        message="hi",
        context_prompt="",
        history=[],
        source=_source(),
        session_id=SESSION_ID,
        session_key=SESSION_KEY,
        run_generation=stale_gen,
    )

    assert result["final_response"] == ""
    assert result["api_calls"] == 0
    assert load_calls["n"] == 0


def test_abort_result_if_stale_generation_covers_run_conversation_gate(
    monkeypatch, tmp_path
):
    """Secondary gate helper must abort after agent build when generation bumps."""
    runner = _runner(monkeypatch, tmp_path)
    gen = runner._begin_session_run_generation(SESSION_KEY)
    assert (
        runner._abort_result_if_stale_generation(
            SESSION_KEY, gen, history=[], session_id=SESSION_ID, log_label="run_conversation"
        )
        is None
    )

    runner._invalidate_session_run_generation(SESSION_KEY, reason="stop")
    abort = runner._abort_result_if_stale_generation(
        SESSION_KEY,
        gen,
        history=[{"role": "user", "content": "x"}],
        session_id=SESSION_ID,
        log_label="run_conversation",
    )
    assert abort is not None
    assert abort["final_response"] == ""
    assert abort["api_calls"] == 0
    assert abort["history_offset"] == 1
    assert abort["session_id"] == SESSION_ID


def test_promote_or_interrupt_stale_agent_uses_control_stop_reason(
    monkeypatch, tmp_path
):
    """Stale promotion skip must interrupt with a control reason, not prose."""
    runner = _runner(monkeypatch, tmp_path)
    stale_gen = runner._begin_session_run_generation(SESSION_KEY)
    runner._invalidate_session_run_generation(SESSION_KEY, reason="stop")
    # Newer turn already owns the slot.
    runner._running_agents[SESSION_KEY] = "fresh_agent"

    agent = MagicMock()
    promoted = runner._promote_or_interrupt_stale_agent(
        SESSION_KEY, agent, stale_gen
    )

    assert promoted is False
    assert runner._running_agents[SESSION_KEY] == "fresh_agent"
    agent.interrupt.assert_called_once_with(gateway_run._INTERRUPT_REASON_STOP)
    assert gateway_run._is_control_interrupt_message(
        agent.interrupt.call_args.args[0]
    )


def test_promote_or_interrupt_promotes_when_current(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    gen = runner._begin_session_run_generation(SESSION_KEY)
    agent = MagicMock()

    promoted = runner._promote_or_interrupt_stale_agent(SESSION_KEY, agent, gen)

    assert promoted is True
    assert runner._running_agents[SESSION_KEY] is agent
    agent.interrupt.assert_not_called()


def test_promote_or_interrupt_does_not_clobber_other_agent(monkeypatch, tmp_path):
    """Even if generation still matches, never overwrite a different live agent."""
    runner = _runner(monkeypatch, tmp_path)
    gen = runner._begin_session_run_generation(SESSION_KEY)
    fresh = MagicMock(name="fresh")
    stale = MagicMock(name="stale")
    runner._running_agents[SESSION_KEY] = fresh

    promoted = runner._promote_or_interrupt_stale_agent(SESSION_KEY, stale, gen)

    assert promoted is False
    assert runner._running_agents[SESSION_KEY] is fresh
    stale.interrupt.assert_called_once_with(gateway_run._INTERRUPT_REASON_STOP)


@pytest.mark.asyncio
async def test_stop_during_prep_replacement_and_stale_cleanup_interleave(
    monkeypatch, tmp_path
):
    """Full path: stop mid-prep, replacement claims slot, stale unwind must not clobber.

    Covers the sweeper-requested interleaving of:
    1) /stop during pending setup (abort before ``_run_agent``)
    2) a follow-up turn owning the busy slot
    3) generation-scoped outer cleanup that must not pop the replacement
    """
    runner = _runner(monkeypatch, tmp_path)
    stale_gen = runner._begin_session_run_generation(SESSION_KEY)
    runner._running_agents[SESSION_KEY] = gateway_run._AGENT_PENDING_SENTINEL
    runner._running_agents_ts[SESSION_KEY] = 1.0
    runner._busy_ack_ts[SESSION_KEY] = 1.0

    replacement = MagicMock(name="replacement_agent")
    replacement_gen_holder = {"gen": None}

    async def _prep_then_stop_and_replace(*_args, **_kwargs):
        # /stop during pending setup: bump generation and clear the slot.
        runner._invalidate_session_run_generation(SESSION_KEY, reason="stop")
        assert runner._release_running_agent_state(SESSION_KEY) is True

        # Replacement turn claims the busy slot while stale prep is still
        # unwinding (same ownership race as #11016 / #71133).
        replacement_gen_holder["gen"] = runner._begin_session_run_generation(
            SESSION_KEY
        )
        runner._running_agents[SESSION_KEY] = replacement
        runner._running_agents_ts[SESSION_KEY] = 2.0
        runner._busy_ack_ts[SESSION_KEY] = 2.0
        return "transcribed text"

    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(
        side_effect=_prep_then_stop_and_replace
    )
    runner._run_agent = AsyncMock(
        side_effect=AssertionError("stale turn must not enter _run_agent")
    )

    result = await runner._handle_message_with_agent(
        _event(), _source(), SESSION_KEY, stale_gen
    )

    assert result is None
    runner._run_agent.assert_not_called()
    assert not runner._is_session_run_current(SESSION_KEY, stale_gen)

    # Stale outer finally (generation-scoped) must leave the replacement alone.
    released = runner._release_running_agent_state(
        SESSION_KEY, run_generation=stale_gen
    )
    assert released is False
    assert runner._running_agents[SESSION_KEY] is replacement
    assert runner._running_agents_ts[SESSION_KEY] == 2.0
    assert runner._busy_ack_ts[SESSION_KEY] == 2.0

    # Replacement turn can still clear its own slot when it finishes.
    assert (
        runner._release_running_agent_state(
            SESSION_KEY, run_generation=replacement_gen_holder["gen"]
        )
        is True
    )
    assert SESSION_KEY not in runner._running_agents

