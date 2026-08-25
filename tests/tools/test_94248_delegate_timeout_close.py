"""#94248: delegate timeout must not hard-close the child's TLS/SQLite from the parent.

Mirrors the production sequence: worker blocked in a Codex-like read,
``result(timeout=)`` fires, parent ``child.close()`` runs ~tens of ms later
while the worker is still on-stack.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from run_agent import AIAgent
from tests.run_agent.test_94248_in_flight_close import (
    _RecordingClient,
    _RecordingDB,
    _bare_agent,
)


class _TimeoutChild(AIAgent):
    """Real close()/interrupt ABI, but hard_interrupt does not unblock SSL."""

    def hard_interrupt(self, message=None, *, tool_reason=None):
        return


def _child_for_timeout():
    child = _bare_agent()
    # Re-bind as the subclass so request_hard_interrupt finds hard_interrupt
    # on this type, not the real AIAgent implementation.
    child.__class__ = _TimeoutChild
    child._delegate_saved_tool_names = ["terminal"]
    child.session_id = "timed-out-codex-child"
    child._hang = threading.Event()
    child._started = threading.Event()
    child.client = _RecordingClient()
    child._session_db = _RecordingDB()
    child._owns_session_db = True

    def run_conversation(**_kwargs):
        child._execution_thread_id = threading.current_thread().ident
        child._mark_run_conversation_started()
        child._model_request_active.set()
        child._started.set()
        try:
            child._hang.wait(timeout=30)
            return {
                "final_response": "",
                "completed": False,
                "interrupted": True,
                "api_calls": 1,
                "messages": [],
            }
        finally:
            child._model_request_active.clear()
            child._mark_run_conversation_finished()

    child.run_conversation = run_conversation
    child.get_activity_summary = lambda: {"api_call_count": 1}
    return child


def test_timed_out_delegate_does_not_close_sqlite_while_ssl_read_blocked(
    monkeypatch, tmp_path
):
    from tools.delegate_tool import _run_single_child

    parent = MagicMock()
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    child = _child_for_timeout()
    parent._active_children.append(child)
    client = child.client
    db = child._session_db
    monkeypatch.setattr("tools.delegate_tool._get_child_timeout", lambda: 0.15)

    token = set_hermes_home_override(tmp_path / "profile-94248")
    try:
        result = _run_single_child(
            task_index=0,
            goal="blocked Codex SSL read",
            child=child,
            parent_agent=parent,
        )
        assert child._started.wait(timeout=2.0)
        assert result["status"] == "timeout"
        # Parent close() ran in _run_single_child's finally while the worker
        # was still in hang.wait (SSL stand-in). Must not close SQLite or
        # httpx FDs yet.
        assert db.close_calls == 0
        assert client.close_calls == 0
        assert child.client is None
        assert child._owns_session_db is True
    finally:
        child._hang.set()
        deferred = getattr(child, "_deferred_close_thread", None)
        if deferred is not None:
            deferred.join(timeout=2.0)
        reset_hermes_home_override(token)

    # After the worker unwinds, deferred close releases the dedicated handle.
    assert db.close_calls == 1
    assert client.close_calls == 0
    assert child._owns_session_db is False
