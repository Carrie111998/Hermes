"""Regression: /stop must actually stop a BATCH (n > 1) delegation.

The batch poll loop already noticed `parent_agent._interrupt_requested`
within ~0.5s and fabricated "interrupted" entries for the unfinished
children -- but it then fell out of ``with DaemonThreadPoolExecutor(...)``,
whose ``__exit__`` calls ``shutdown(wait=True)`` and joins every worker.
So the interrupt was honoured in the loop and immediately un-honoured on
the way out: ``delegate_task`` blocked on exactly the wedged child it had
just decided to abandon. Same falsely-successful /stop as the single-task
path, one layer down.
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
    child = MagicMock()
    child._delegate_role = "leaf"
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: CREDS)
    return child


def test_batch_delegation_does_not_join_wedged_child_on_interrupt(
    monkeypatch, fake_child
):
    gate = threading.Event()
    entered = threading.Event()

    def _child(task_index, goal, child=None, parent_agent=None, **kw):
        if task_index == 0:
            return {
                "task_index": 0,
                "status": "completed",
                "summary": "fast one",
                "api_calls": 1,
                "duration_seconds": 0.01,
                "exit_reason": "completed",
            }
        # Task 1 is wedged: it only unblocks when the test releases it.
        entered.set()
        gate.wait(timeout=30)
        return {
            "task_index": task_index,
            "status": "completed",
            "summary": "wedged one",
            "api_calls": 1,
            "duration_seconds": 30.0,
            "exit_reason": "completed",
        }

    monkeypatch.setattr(dt, "_run_single_child", _child)

    parent = _make_mock_parent()
    parent._interrupt_requested = False

    box = {}

    def _runner():
        t0 = time.monotonic()
        box["out"] = dt.delegate_task(
            tasks=[{"goal": "fast"}, {"goal": "wedged"}], parent_agent=parent
        )
        box["elapsed"] = time.monotonic() - t0

    th = threading.Thread(target=_runner, daemon=True)

    def _interrupter():
        entered.wait(timeout=5)
        time.sleep(0.2)
        parent._interrupt_requested = True

    ti = threading.Thread(target=_interrupter, daemon=True)

    try:
        th.start()
        ti.start()
        # Bounded join: before the fix delegate_task sits in shutdown(wait=True)
        # joining the wedged worker, so the thread is still alive here.
        th.join(timeout=6)
        assert not th.is_alive(), (
            "interrupt ignored: delegate_task still blocked after 6s "
            "(joining the wedged child on executor shutdown)"
        )
    finally:
        gate.set()
        ti.join(timeout=5)
        th.join(timeout=35)

    assert box["elapsed"] < 2.0, f"delegate_task blocked {box['elapsed']:.2f}s"

    out = json.loads(box["out"])
    assert len(out["results"]) == 2
    # Results stay ordered by task_index.
    assert [r["task_index"] for r in out["results"]] == [0, 1]
    # The child that finished before the interrupt keeps its real result.
    assert out["results"][0]["status"] == "completed"
    assert out["results"][0]["summary"] == "fast one"
    # The wedged one is reported as interrupted, not silently dropped.
    assert out["results"][1]["status"] == "interrupted"
    assert out["results"][1]["summary"] is None
    assert "interrupt" in (out["results"][1].get("error") or "").lower()


def test_batch_delegation_uninterrupted_results_unchanged(monkeypatch, fake_child):
    """Hot path: an un-interrupted batch must still join on all children and
    return their real results, in task_index order."""
    payloads = {
        0: {
            "task_index": 0,
            "status": "completed",
            "summary": "Result A",
            "api_calls": 2,
            "duration_seconds": 3.0,
            "exit_reason": "completed",
        },
        1: {
            "task_index": 1,
            "status": "completed",
            "summary": "Result B",
            "api_calls": 4,
            "duration_seconds": 6.0,
            "exit_reason": "completed",
        },
    }

    def _child(task_index, goal, child=None, parent_agent=None, **kw):
        # Task 1 finishes last so the ordering assertion is not vacuous.
        time.sleep(0.05 * task_index)
        return dict(payloads[task_index])

    monkeypatch.setattr(dt, "_run_single_child", _child)

    parent = _make_mock_parent()
    parent._interrupt_requested = False

    out = json.loads(
        dt.delegate_task(
            tasks=[{"goal": "A"}, {"goal": "B"}], parent_agent=parent
        )
    )

    assert set(out.keys()) == {"results", "total_duration_seconds"}
    assert out["results"] == [payloads[0], payloads[1]]
    assert not fake_child.interrupt.called
