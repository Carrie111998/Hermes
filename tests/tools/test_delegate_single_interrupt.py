"""Regression: /stop must actually stop a SINGLE (n == 1) delegation.

The batch path (n > 1) runs its children on a daemon pool and polls
``parent_agent._interrupt_requested`` every 0.5s, so an interrupt lands
within ~half a second.  The single-task path used to call
``_run_single_child`` inline on the parent thread, leaving nowhere to
observe the flag: /stop set it, the UI answered "stopped", and
``delegate_task`` kept blocking until the child finished on its own — a
falsely-successful stop.

These tests pin both halves of the contract:
  1. an interrupted single delegation returns fast, with the same
     fabricated "interrupted" entry shape the batch path produces;
  2. an *un*-interrupted single delegation returns exactly what it
     always did (hot path — no shape drift allowed).
"""

import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from tools import delegate_tool as dt
from tests.tools.test_delegate import _make_mock_parent


CREDS = {
    "model": "m",
    "provider": None,
    "base_url": None,
    "api_key": None,
    "api_mode": None,
    "command": None,
    "args": None,
}


@pytest.fixture
def fake_child(monkeypatch):
    """Patch child construction + credential resolution away."""
    child = MagicMock()
    child._delegate_role = "leaf"
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: CREDS)
    return child


def test_single_delegation_returns_promptly_on_parent_interrupt(monkeypatch, fake_child):
    gate = threading.Event()
    entered = threading.Event()

    def _blocking_child(task_index, goal, child=None, parent_agent=None, **kw):
        entered.set()
        gate.wait(timeout=8)
        return {
            "task_index": task_index,
            "status": "completed",
            "summary": f"done: {goal}",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "exit_reason": "completed",
        }

    monkeypatch.setattr(dt, "_run_single_child", _blocking_child)

    parent = _make_mock_parent()
    parent._interrupt_requested = False

    def _interrupter():
        entered.wait(timeout=5)
        time.sleep(0.2)
        parent._interrupt_requested = True

    t = threading.Thread(target=_interrupter, daemon=True)
    t.start()

    t0 = time.monotonic()
    try:
        out = json.loads(dt.delegate_task(goal="hang forever", parent_agent=parent))
    finally:
        gate.set()
        t.join(timeout=5)
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0, f"interrupt ignored: delegate_task blocked {elapsed:.2f}s"
    assert len(out["results"]) == 1
    entry = out["results"][0]
    # Same fabricated shape the batch path uses for unfinished children.
    assert entry["status"] == "interrupted"
    assert entry["task_index"] == 0
    assert entry["summary"] is None
    assert "interrupt" in (entry.get("error") or "").lower()
    assert entry["api_calls"] == 0
    # The child was told to stop, not just abandoned.
    assert fake_child.interrupt.called


def test_single_delegation_uninterrupted_result_shape_unchanged(monkeypatch, fake_child):
    """Hot path: the un-interrupted single delegation must be byte-identical."""
    payload = {
        "task_index": 0,
        "status": "completed",
        "summary": "Done!",
        "api_calls": 3,
        "duration_seconds": 5.0,
        "exit_reason": "completed",
        "model": "m",
    }
    calls = []

    def _child(task_index, goal, child=None, parent_agent=None, **kw):
        calls.append((task_index, goal, child, parent_agent))
        return dict(payload)

    monkeypatch.setattr(dt, "_run_single_child", _child)

    parent = _make_mock_parent()
    parent._interrupt_requested = False

    out = json.loads(
        dt.delegate_task(goal="Fix tests", context="error log...", parent_agent=parent)
    )

    assert set(out.keys()) == {"results", "total_duration_seconds"}
    assert len(out["results"]) == 1
    assert out["results"][0] == payload
    # Call convention preserved: (task_index, goal, child, parent_agent)
    assert len(calls) == 1
    assert calls[0][0] == 0
    assert calls[0][1] == "Fix tests"
    assert calls[0][2] is fake_child
    assert calls[0][3] is parent
    assert not fake_child.interrupt.called


def test_single_delegation_child_exception_still_propagates(monkeypatch, fake_child):
    """A raising child must behave exactly as it did when called inline."""

    def _boom(task_index, goal, child=None, parent_agent=None, **kw):
        raise RuntimeError("child exploded")

    monkeypatch.setattr(dt, "_run_single_child", _boom)

    parent = _make_mock_parent()
    parent._interrupt_requested = False

    with pytest.raises(RuntimeError, match="child exploded"):
        dt.delegate_task(goal="explode", parent_agent=parent)


def test_cap_fallback_single_delegation_honours_interrupt(monkeypatch, fake_child):
    """Background dispatch rejected at capacity falls back to an inline run
    (`_cap_result`), which goes through the same `_execute_and_aggregate` —
    so /stop must work there too."""
    monkeypatch.setattr(dt, "_get_max_async_children", lambda: 0)

    gate = threading.Event()
    entered = threading.Event()

    def _blocking_child(task_index, goal, child=None, parent_agent=None, **kw):
        entered.set()
        gate.wait(timeout=8)
        return {
            "task_index": task_index,
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "exit_reason": "completed",
        }

    monkeypatch.setattr(dt, "_run_single_child", _blocking_child)

    parent = _make_mock_parent()
    parent._interrupt_requested = False

    def _interrupter():
        entered.wait(timeout=5)
        time.sleep(0.2)
        parent._interrupt_requested = True

    t = threading.Thread(target=_interrupter, daemon=True)
    t.start()

    t0 = time.monotonic()
    try:
        out = json.loads(
            dt.delegate_task(goal="hang forever", background=True, parent_agent=parent)
        )
    finally:
        gate.set()
        t.join(timeout=5)
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0, f"cap-fallback ignored interrupt: blocked {elapsed:.2f}s"
    assert out.get("status") != "dispatched"
    assert "note" in out  # the sync-fallback marker
    assert out["results"][0]["status"] == "interrupted"
