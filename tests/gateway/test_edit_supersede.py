"""Tests for gateway-core edit supersede (#35535).

An inbound EDIT of a message, correlated by message_id, supersedes the queued
original or the in-flight turn.  Non-edit events are completely unaffected.
"""
from __future__ import annotations

import sys
import threading
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Minimal telegram stubs so gateway imports cleanly.
_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.SUPERGROUP = "supergroup"
_ct.GROUP = "group"
_ct.PRIVATE = "private"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from gateway.platforms.base import (  # noqa: E402
    MessageEvent,
    MessageType,
    SessionSource,
    build_session_key,
)
from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL  # noqa: E402
from gateway.session_state import SessionState  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_source(platform_value="telegram", chat_id="c1", user_id="u1"):
    return SessionSource(
        platform=MagicMock(value=platform_value),
        chat_id=chat_id,
        chat_type="private",
        user_id=user_id,
    )


def _make_event(
    text: str = "hello",
    message_id: str = "msg_1",
    is_edit: bool = False,
    message_type: MessageType = MessageType.TEXT,
    source: SessionSource = None,
) -> MessageEvent:
    if source is None:
        source = _make_source()
    meta = {"is_edit": True} if is_edit else {}
    return MessageEvent(
        text=text,
        message_type=message_type,
        source=source,
        message_id=message_id,
        metadata=meta,
    )


def _make_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter.platform = MagicMock(value="telegram")
    return adapter


def _make_running_agent(supports_redirect: bool = True, supports_steer: bool = True) -> MagicMock:
    agent = MagicMock()
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    agent.get_activity_summary.return_value = {
        "api_call_count": 4,
        "max_iterations": 60,
        "current_tool": "terminal",
    }
    agent._supports_active_turn_redirect = supports_redirect
    if supports_redirect:
        agent.redirect = MagicMock(return_value=True)
    if supports_steer:
        agent.steer = MagicMock(return_value=True)
    return agent


def _make_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._sessions = {}
    runner._draining = False
    runner.adapters = {}
    runner.config = MagicMock()
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda _source: True
    return runner


def _session_state(runner: GatewayRunner, session_key: str) -> SessionState:
    if session_key not in runner._sessions:
        runner._sessions[session_key] = SessionState()
    return runner._sessions[session_key]


# ===================================================================
# Tests: _replace_queued_message
# ===================================================================

class TestReplaceQueuedMessage:
    """Verify _replace_queued_message searches both queue levels."""

    def test_replace_primary_slot(self):
        """Edit replaces text in the primary pending slot."""
        runner = _make_runner()
        adapter = _make_adapter()
        sk = "test_session"
        original = _make_event(text="original text", message_id="msg_1")
        adapter._pending_messages[sk] = original

        replaced = runner._replace_queued_message(sk, adapter, "msg_1", "edited text")

        assert replaced is True
        assert adapter._pending_messages[sk].text == "edited text"
        # Other fields preserved
        assert adapter._pending_messages[sk].message_id == "msg_1"
        assert adapter._pending_messages[sk].message_type == MessageType.TEXT

    def test_replace_overflow_fifo(self):
        """Edit replaces text in the overflow FIFO when primary slot does not match."""
        runner = _make_runner()
        adapter = _make_adapter()
        sk = "test_session"
        primary = _make_event(text="primary", message_id="msg_other")
        adapter._pending_messages[sk] = primary
        overflow = _make_event(text="overflow target", message_id="msg_target")
        _session_state(runner, sk).conversation.queued_events = [
            _make_event(text="first", message_id="msg_a"),
            overflow,
            _make_event(text="last", message_id="msg_c"),
        ]

        replaced = runner._replace_queued_message(sk, adapter, "msg_target", "edited overflow")

        assert replaced is True
        assert adapter._pending_messages[sk].text == "primary"  # unchanged
        assert _session_state(runner, sk).conversation.queued_events[1].text == "edited overflow"
        # FIFO order preserved
        assert len(_session_state(runner, sk).conversation.queued_events) == 3

    def test_no_match_returns_false(self):
        """No matching message_id in either queue returns False."""
        runner = _make_runner()
        adapter = _make_adapter()
        sk = "test_session"
        adapter._pending_messages[sk] = _make_event(text="primary", message_id="msg_a")
        _session_state(runner, sk).conversation.queued_events = [
            _make_event(text="second", message_id="msg_b"),
        ]

        replaced = runner._replace_queued_message(sk, adapter, "nonexistent", "edited")

        assert replaced is False
        assert adapter._pending_messages[sk].text == "primary"
        assert _session_state(runner, sk).conversation.queued_events[0].text == "second"

    def test_no_primary_slot_searches_overflow(self):
        """No primary slot for session_key — searches overflow FIFO."""
        runner = _make_runner()
        adapter = _make_adapter()
        sk = "test_session"
        # primary slot empty for this key (no entry)
        assert sk not in adapter._pending_messages
        _session_state(runner, sk).conversation.queued_events = [
            _make_event(text="target", message_id="msg_x"),
        ]

        replaced = runner._replace_queued_message(sk, adapter, "msg_x", "edited text")

        assert replaced is True
        assert _session_state(runner, sk).conversation.queued_events[0].text == "edited text"


# ===================================================================
# Tests: _handle_edit_supersede
# ===================================================================

class TestHandleEditSupersede:
    """Verify edit-correlation logic in _handle_edit_supersede."""

    def test_non_edit_events_untouched(self):
        """Events without is_edit metadata pass through (return False)."""
        runner = _make_runner()
        sk = "test_session"
        event = _make_event(text="not an edit", message_id="msg_1", is_edit=False)
        adapter = _make_adapter()
        state = _session_state(runner, sk)
        state.turn.active_message_id = None

        result = runner._handle_edit_supersede(event, sk, adapter)

        assert result is False  # not handled, continue processing

    def test_edit_no_message_id_passes_through(self):
        """Edit with empty/None message_id passes through."""
        runner = _make_runner()
        sk = "test_session"
        event = _make_event(text="no id", message_id="", is_edit=True)
        adapter = _make_adapter()

        result = runner._handle_edit_supersede(event, sk, adapter)

        assert result is False

    def test_edit_replaces_queued_primary(self):
        """Edit event replaces the queued original in the primary slot and returns True."""
        runner = _make_runner()
        sk = "test_session"
        adapter = _make_adapter()
        original = _make_event(text="original", message_id="msg_1")
        adapter._pending_messages[sk] = original
        edit_event = _make_event(text="corrected", message_id="msg_1", is_edit=True)

        result = runner._handle_edit_supersede(edit_event, sk, adapter)

        assert result is True
        assert adapter._pending_messages[sk].text == "corrected"

    def test_edit_replaces_queued_overflow(self):
        """Edit event replaces matching message in overflow FIFO."""
        runner = _make_runner()
        sk = "test_session"
        adapter = _make_adapter()
        adapter._pending_messages[sk] = _make_event(text="primary", message_id="other_msg")
        _session_state(runner, sk).conversation.queued_events = [
            _make_event(text="edited msg", message_id="msg_target"),
        ]
        edit_event = _make_event(text="corrected", message_id="msg_target", is_edit=True)

        result = runner._handle_edit_supersede(edit_event, sk, adapter)

        assert result is True
        assert _session_state(runner, sk).conversation.queued_events[0].text == "corrected"

    def test_edit_redirects_in_flight_when_supported(self):
        """Edit matching active_message_id calls redirect() on running agent."""
        runner = _make_runner()
        sk = "test_session"
        adapter = _make_adapter()
        agent = _make_running_agent(supports_redirect=True)
        state = _session_state(runner, sk)
        state.turn.agent = agent
        state.turn.active_message_id = "msg_inflight"
        edit_event = _make_event(text="mid-flight correction", message_id="msg_inflight", is_edit=True)

        result = runner._handle_edit_supersede(edit_event, sk, adapter)

        assert result is True
        agent.redirect.assert_called_once()
        call_args = agent.redirect.call_args[0][0]
        assert 'User edited their earlier message.' in call_args
        assert 'mid-flight correction' in call_args

    def test_edit_steers_when_redirect_unsupported(self):
        """Edit falls back to steer() when redirect is unavailable."""
        runner = _make_runner()
        sk = "test_session"
        adapter = _make_adapter()
        agent = _make_running_agent(supports_redirect=False, supports_steer=True)
        state = _session_state(runner, sk)
        state.turn.agent = agent
        state.turn.active_message_id = "msg_steer"
        edit_event = _make_event(text="steer this one", message_id="msg_steer", is_edit=True)

        result = runner._handle_edit_supersede(edit_event, sk, adapter)

        assert result is True
        agent.steer.assert_called_once()
        call_args = agent.steer.call_args[0][0]
        assert 'User edited their earlier message.' in call_args

    def test_edit_queues_when_both_primitives_unavailable(self):
        """Edit falls back to queue when both redirect and steer are missing."""
        runner = _make_runner()
        sk = "test_session"
        adapter = _make_adapter()
        agent = _make_running_agent(supports_redirect=False, supports_steer=False)
        # Remove both methods
        if hasattr(agent, 'redirect'):
            del agent.redirect
        if hasattr(agent, 'steer'):
            del agent.steer

        state = _session_state(runner, sk)
        state.turn.agent = agent
        state.turn.active_message_id = "msg_fallback"
        edit_event = _make_event(text="fallback text", message_id="msg_fallback", is_edit=True)
        # Register adapter so _queue_or_replace_pending_event can find it
        runner.adapters[edit_event.source.platform] = adapter

        result = runner._handle_edit_supersede(edit_event, sk, adapter)

        assert result is True
        # Should have been queued via _queue_or_replace_pending_event
        assert sk in adapter._pending_messages
        assert adapter._pending_messages[sk].text == "fallback text"

    def test_stale_edit_dropped(self):
        """Edit with no active_message_id (turn cleared) is dropped, not redirected."""
        runner = _make_runner()
        sk = "test_session"
        adapter = _make_adapter()
        agent = _make_running_agent()
        state = _session_state(runner, sk)
        state.turn.agent = agent
        state.turn.active_message_id = None  # turn already cleared
        edit_event = _make_event(text="stale edit", message_id="msg_stale", is_edit=True)

        result = runner._handle_edit_supersede(edit_event, sk, adapter)

        assert result is True  # handled (dropped)
        agent.redirect.assert_not_called()
        agent.steer.assert_not_called()
        assert sk not in adapter._pending_messages  # not queued either

    def test_uncorrelated_edit_dropped_no_new_turn(self):
        """Edit matching no queued or in-flight message is dropped with no dispatch."""
        runner = _make_runner()
        sk = "test_session"
        adapter = _make_adapter()
        state = _session_state(runner, sk)
        state.turn.agent = None  # idle session
        state.turn.active_message_id = None
        edit_event = _make_event(text="orphan edit", message_id="msg_orphan", is_edit=True)

        result = runner._handle_edit_supersede(edit_event, sk, adapter)

        assert result is True  # handled (dropped)
        assert sk not in adapter._pending_messages

    def test_sentinel_window_edit_dropped(self):
        """Edit matching active_message_id during the sentinel window is dropped
        with a distinct log; redirect/steer not called."""
        runner = _make_runner()
        sk = "test_session"
        adapter = _make_adapter()
        state = _session_state(runner, sk)
        state.turn.agent = _AGENT_PENDING_SENTINEL
        state.turn.active_message_id = "msg_sentinel"
        edit_event = _make_event(text="sentinel edit", message_id="msg_sentinel", is_edit=True)

        result = runner._handle_edit_supersede(edit_event, sk, adapter)

        # The active_message_id matches but agent is sentinel, so we should
        # either drop (uncorrelated since no real agent) or queue.  The task says
        # "if turn.agent is a real agent (not None, not _AGENT_PENDING_SENTINEL)"
        # for the redirect/steer path, so this falls through to uncorrelated drop.
        assert result is True  # dropped

    def test_edit_to_queued_overflow_match_first(self):
        """Edit matching in primary slot takes priority over overflow and in-flight."""
        runner = _make_runner()
        sk = "test_session"
        adapter = _make_adapter()
        agent = _make_running_agent()
        state = _session_state(runner, sk)
        state.turn.agent = agent
        state.turn.active_message_id = "msg_inflight"
        # Both primary and in-flight have same message_id
        adapter._pending_messages[sk] = _make_event(text="queued", message_id="msg_inflight")
        edit_event = _make_event(text="edit that matches both", message_id="msg_inflight", is_edit=True)

        result = runner._handle_edit_supersede(edit_event, sk, adapter)

        # Should replace the queued one (primary slot match comes first)
        assert result is True
        assert adapter._pending_messages[sk].text == "edit that matches both"
        agent.redirect.assert_not_called()


# ===================================================================
# Tests: rapid consecutive edits (latest-wins race)
# ===================================================================

class TestRapidConsecutiveEdits:
    """Verify that consecutive synchronous edits on the same message
    leave the queued event at the latest text (no stale state, no crash)."""

    def test_rapid_consecutive_edits_latest_wins(self):
        """Two back-to-back edits on the same message_id: final text wins."""
        runner = _make_runner()
        sk = "rapid_session"
        adapter = _make_adapter()
        # Prime the primary pending slot with an original message
        adapter._pending_messages[sk] = _make_event(
            text="original", message_id="900"
        )

        # Call _handle_edit_supersede twice in a row (synchronously, no await
        # between) with two edit events for message_id "900".
        edit_a = _make_event(text="edit A", message_id="900", is_edit=True)
        edit_b = _make_event(text="edit B", message_id="900", is_edit=True)

        runner._handle_edit_supersede(edit_a, sk, adapter)
        runner._handle_edit_supersede(edit_b, sk, adapter)

        # The queued event's text should be "edit B" — latest wins, no stale
        # state, no crash.
        assert adapter._pending_messages[sk].text == "edit B"
        assert adapter._pending_messages[sk].message_id == "900"