"""Tests for the resume-size guard on the gateway's per-message transcript load.

``gateway/run.py::_handle_message_with_agent`` calls
``self.async_session_store.load_transcript()`` on EVERY incoming message for a
live session — unlike the CLI/TUI resume paths, it never checked
``assert_resume_safe()`` (added this week to stop a runaway lineage from
exhausting memory on resume/export). A session that keeps growing while
staying live in ``_sessions`` would otherwise re-materialize its full,
unbounded transcript on every single message.
"""

import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource
from hermes_state import SessionResumeTooLargeError


def _bootstrap(monkeypatch, tmp_path):
    """Minimal GatewayRunner setup, mirroring test_42039_duplicate_user_message."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    config = GatewayConfig()
    runner = gateway_run.GatewayRunner(config)
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._begin_session_run_generation = lambda _key: 1
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:group:-1001:12345",
        session_id="sess-guard",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.has_platform_message_id.return_value = False
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


def _event():
    return MessageEvent(
        text="hello world",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            user_id="12345",
        ),
        message_id="msg-guard",
    )


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        user_id="12345",
    )


@pytest.mark.asyncio
async def test_agent_path_rejects_oversized_transcript_before_loading(
    monkeypatch, tmp_path
):
    """A runaway lineage must abort before the unbounded load, not after."""
    runner = _bootstrap(monkeypatch, tmp_path)
    runner._session_db.assert_resume_safe = AsyncMock(
        side_effect=SessionResumeTooLargeError(20_001, 20_000)
    )
    runner.session_store.load_transcript.side_effect = AssertionError(
        "transcript must not load once the session is over the resume limit"
    )
    runner._run_agent = pytest.fail

    with pytest.raises(SessionResumeTooLargeError):
        await runner._handle_message_with_agent(
            _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
        )

    runner.session_store.load_transcript.assert_not_called()


@pytest.mark.asyncio
async def test_full_dispatch_rejects_oversized_transcript_with_bounded_notice(
    monkeypatch, tmp_path
):
    """The outer dispatch turns the guard's error into a bounded chat reply,
    cleans up the session-env tokens, and never runs the /goal hook — the
    same shape as the existing turn-lease-timeout rejection."""
    runner = _bootstrap(monkeypatch, tmp_path)
    runner._session_db.assert_resume_safe = AsyncMock(
        side_effect=SessionResumeTooLargeError(20_001, 20_000)
    )
    runner.session_store.load_transcript.side_effect = AssertionError(
        "transcript must not load once the session is over the resume limit"
    )
    runner._run_agent = pytest.fail
    session_env_tokens = object()
    runner._set_session_env = MagicMock(return_value=session_env_tokens)
    runner._clear_session_env = MagicMock()
    runner._post_turn_goal_continuation = AsyncMock()

    response = await runner._handle_message(_event())

    assert isinstance(response, str)
    assert "too large" in response.lower()
    runner.session_store.load_transcript.assert_not_called()
    runner._clear_session_env.assert_called_once_with(session_env_tokens)
    runner._post_turn_goal_continuation.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_path_still_loads_transcript_within_the_limit(
    monkeypatch, tmp_path
):
    """The new guard must not regress the ordinary (within-limit) path."""
    runner = _bootstrap(monkeypatch, tmp_path)
    runner._session_db.assert_resume_safe = AsyncMock(return_value=3)
    runner._run_agent = AsyncMock(
        return_value={"final_response": "", "failed": True}
    )

    await runner._handle_message_with_agent(
        _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
    )

    runner.session_store.load_transcript.assert_called_once_with("sess-guard")
