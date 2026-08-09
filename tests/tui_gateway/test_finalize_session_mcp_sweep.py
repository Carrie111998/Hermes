"""_finalize_session must reap MCP stdio orphans on session boundaries (#81880).

The Desktop backend stays alive across many session rotations, so nothing
else sweeps ``_orphan_stdio_pids`` between sessions -- without this call,
orphaned MCP stdio subprocesses (and their watchdog supervisors) accumulate
until the OS starts killing unrelated processes under memory pressure.
"""

import threading
from unittest.mock import MagicMock, patch


def _make_agent(session_id="test_session_001"):
    agent = MagicMock()
    agent._persist_session = MagicMock()
    agent.commit_memory_session = MagicMock()
    agent.session_id = session_id
    agent.model = "test-model"
    agent.platform = "tui"
    agent._session_messages = None
    return agent


def _make_session(agent=None, history=None, session_key="test_key_001"):
    return {
        "agent": agent,
        "history": history or [],
        "history_lock": threading.Lock(),
        "session_key": session_key,
        "_finalized": False,
    }


class TestFinalizeSessionMCPSweep:
    def test_finalize_session_reaps_mcp_orphans(self):
        from tui_gateway.server import _finalize_session

        agent = _make_agent()
        session = _make_session(agent=agent, history=[{"role": "user", "content": "hi"}])

        with patch("tools.mcp_tool._kill_orphaned_mcp_children") as mock_sweep:
            _finalize_session(session, end_reason="test")

        mock_sweep.assert_called_once_with()

    def test_finalize_session_survives_mcp_sweep_failure(self):
        """A broken MCP sweep must never crash session finalize (best-effort)."""
        from tui_gateway.server import _finalize_session

        agent = _make_agent()
        session = _make_session(agent=agent, history=[{"role": "user", "content": "hi"}])

        with patch(
            "tools.mcp_tool._kill_orphaned_mcp_children",
            side_effect=RuntimeError("boom"),
        ):
            _finalize_session(session, end_reason="test")  # must not raise

        agent.commit_memory_session.assert_called_once()

    def test_already_finalized_skips_mcp_sweep(self):
        from tui_gateway.server import _finalize_session

        agent = _make_agent()
        session = _make_session(agent=agent, history=[{"role": "user", "content": "x"}])
        session["_finalized"] = True

        with patch("tools.mcp_tool._kill_orphaned_mcp_children") as mock_sweep:
            _finalize_session(session, end_reason="test")

        mock_sweep.assert_not_called()
