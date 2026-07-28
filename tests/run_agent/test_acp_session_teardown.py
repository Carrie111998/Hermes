"""Tests for ACP client session teardown on AIAgent.close().

ACPClientSession owns a long-lived subprocess (the ACP-compliant agent).
It is lazily created on first turn and stored as ``agent._acp_session``.
Before this fix, ``AIAgent.close()`` never called ``_acp_session.close()``,
so the subprocess leaked on /new, gateway shutdown, and subagent exit.

These tests verify the hard-teardown path closes the session exactly once
and clears the reference — even when the session itself raises on close.
"""
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure repo root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# Stub out optional heavy dependencies not installed in the test environment
sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

from run_agent import AIAgent


def _make_minimal_agent() -> AIAgent:
    """Return an AIAgent constructed via __new__ (skips __init__).

    Seeds only the attributes that close() reads, so we can exercise the
    real production code path without network or filesystem side-effects.
    """
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = None
    agent.client = None
    agent._session_messages = []
    agent._end_session_on_close = False  # skip session DB in tests
    agent._session_db = None
    agent._active_children = set()
    # _active_children_lock is created by __init__; provide a real one.
    import threading
    agent._active_children_lock = threading.Lock()
    return agent


class TestACPSessionClosedOnAgentClose:
    """AIAgent.close() must tear down _acp_session."""

    def test_acp_session_closed_exactly_once_and_cleared(self):
        """Happy path: session.close() called once, attribute set to None."""
        agent = _make_minimal_agent()
        mock_session = MagicMock()
        agent._acp_session = mock_session

        agent.close()

        mock_session.close.assert_called_once_with()
        assert agent._acp_session is None

    def test_acp_session_not_present_does_not_raise(self):
        """If _acp_session was never created (no ACP turn happened), close()
        must not raise."""
        agent = _make_minimal_agent()
        # _acp_session is never set — close() should handle its absence.
        assert not hasattr(agent, "_acp_session")

        agent.close()  # must not raise

    def test_acp_session_none_does_not_raise(self):
        """If _acp_session was explicitly set to None (retired session),
        close() must not raise."""
        agent = _make_minimal_agent()
        agent._acp_session = None

        agent.close()  # must not raise

    def test_acp_session_close_exception_does_not_propagate(self):
        """If session.close() raises, AIAgent.close() must not propagate the
        error AND must still clear the reference to None."""
        agent = _make_minimal_agent()
        mock_session = MagicMock()
        mock_session.close.side_effect = RuntimeError("subprocess gone")
        agent._acp_session = mock_session

        agent.close()  # must not raise

        mock_session.close.assert_called_once_with()
        assert agent._acp_session is None

    def test_acp_session_close_runs_before_other_cleanup_unaffected(self):
        """If session.close() raises, the remaining cleanup steps in close()
        must still execute (independent guarding)."""
        agent = _make_minimal_agent()
        mock_session = MagicMock()
        mock_session.close.side_effect = RuntimeError("boom")
        agent._acp_session = mock_session

        # These should still be exercised even after the ACP close raises.
        agent.client = None
        agent._session_messages = ["msg1"]

        agent.close()  # must not raise

        assert agent._acp_session is None
        assert agent._session_messages == []
