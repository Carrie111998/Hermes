"""Durable session activity heartbeats from AIAgent._touch_activity (#72016)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import run_agent


def _agent_with_db(session_id: str = "sess-1"):
    agent = SimpleNamespace(
        session_id=session_id,
        _session_db=MagicMock(),
        _last_activity_ts=0.0,
        _last_activity_desc="",
        _session_activity_last_persist_mono=0.0,
    )
    agent._touch_activity = run_agent.AIAgent._touch_activity.__get__(agent, SimpleNamespace)
    agent._persist_session_activity_if_due = (
        run_agent.AIAgent._persist_session_activity_if_due.__get__(agent, SimpleNamespace)
    )
    return agent


def test_touch_activity_persists_session_heartbeat_once_per_minute(monkeypatch):
    agent = _agent_with_db()
    mono = {"t": 1000.0}
    monkeypatch.setattr(run_agent.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(run_agent.time, "monotonic", lambda: mono["t"])
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    agent._touch_activity("starting API call #1")
    agent._session_db.touch_session_activity.assert_called_once_with(
        "sess-1", 1_700_000_000.0
    )

    agent._session_db.touch_session_activity.reset_mock()
    mono["t"] = 1030.0  # within 60s window
    agent._touch_activity("receiving stream response")
    agent._session_db.touch_session_activity.assert_not_called()

    mono["t"] = 1061.0
    agent._touch_activity("API call #1 completed")
    agent._session_db.touch_session_activity.assert_called_once_with(
        "sess-1", 1_700_000_000.0
    )


def test_touch_activity_skips_persist_without_session_db(monkeypatch):
    agent = _agent_with_db()
    agent._session_db = None
    monkeypatch.setattr(run_agent.time, "time", lambda: 1.0)
    monkeypatch.setattr(run_agent.time, "monotonic", lambda: 1.0)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    agent._touch_activity("starting API call #1")
    assert agent._last_activity_desc == "starting API call #1"


def test_touch_activity_persist_errors_are_swallowed(monkeypatch):
    agent = _agent_with_db()
    agent._session_db.touch_session_activity.side_effect = RuntimeError("db locked")
    monkeypatch.setattr(run_agent.time, "time", lambda: 1.0)
    monkeypatch.setattr(run_agent.time, "monotonic", lambda: 1.0)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    agent._touch_activity("tool completed: terminal (1.0s)")
    assert agent._last_activity_desc == "tool completed: terminal (1.0s)"
