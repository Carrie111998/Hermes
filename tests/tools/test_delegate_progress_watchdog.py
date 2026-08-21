"""Focused synchronous progress-watchdog integration tests."""
import threading
import time
from unittest.mock import MagicMock, patch

from tools.delegate_tool import _run_single_child


def _child(summary_fn, run_fn):
    child = MagicMock()
    child.get_activity_summary.side_effect = summary_fn
    child.run_conversation.side_effect = run_fn
    child._delegate_saved_tool_names = []
    child._delegate_output_schema = None
    child._subagent_id = None
    child.model = "test-model"
    child.session_prompt_tokens = 0
    child.session_completion_tokens = 0
    child.session_reasoning_tokens = 0
    child.session_estimated_cost_usd = 0.0
    child.session_cost_status = "unknown"
    return child


def _parent():
    parent = MagicMock()
    parent._current_task_id = None
    parent._touch_activity = lambda _desc: None
    return parent


def test_sync_worker_can_cross_old_120_second_boundary_with_progress():
    counter = {"calls": 0}
    done = threading.Event()

    def summary():
        counter["calls"] += 1
        return {
            "api_call_count": counter["calls"],
            "current_tool": None,
            "last_activity_ts": float(counter["calls"]),
            "last_activity_desc": "api call completed",
            "max_iterations": 40,
        }

    def run(**_kwargs):
        time.sleep(0.08)
        done.set()
        return {"final_response": "long useful result", "completed": True, "api_calls": 3}

    child = _child(summary, run)
    with patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.01), \
         patch("tools.delegate_tool._HEARTBEAT_STALE_CYCLES_IDLE", 500):
        result = _run_single_child(0, "safe synthetic long task", child, _parent())

    assert done.is_set()
    assert result["status"] == "completed"
    assert result["worker_state"] == "success"


def test_sync_worker_stall_requests_cancellation_and_does_not_retry():
    release = threading.Event()
    interrupt = MagicMock()

    def summary():
        return {
            "api_call_count": 0,
            "current_tool": None,
            "last_activity_ts": 1.0,
            "last_activity_desc": "",
        }

    def run(**_kwargs):
        release.wait(1.0)
        return {"final_response": "late", "completed": True, "api_calls": 0}

    child = _child(summary, run)
    with patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.01), \
         patch("tools.delegate_tool._HEARTBEAT_STALE_CYCLES_IDLE", 2), \
         patch("tools.delegate_tool._get_sync_watchdog_settings", return_value=(0.01, 0.02, 0.02, 0.05, 0.0)), \
         patch("tools.delegate_tool.request_hard_interrupt", side_effect=lambda c: interrupt()):
        result = _run_single_child(0, "safe synthetic stalled task", child, _parent())

    try:
        assert result["status"] == "stalled"
        assert result["worker_state"] == "cancellation_pending"
        assert result["cancellation_pending"] is True
        interrupt.assert_called()
    finally:
        release.set()


def test_sync_stall_result_does_not_report_normal_timeout():
    release = threading.Event()

    def summary():
        return {"api_call_count": 1, "current_tool": None, "last_activity_ts": 1.0}

    child = _child(summary, lambda **_kwargs: (release.wait(1), {})[1])
    with patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.01), \
         patch("tools.delegate_tool._HEARTBEAT_STALE_CYCLES_IDLE", 2), \
         patch("tools.delegate_tool._get_sync_watchdog_settings", return_value=(0.01, 0.02, 0.02, 0.05, 0.0)), \
         patch("tools.delegate_tool.request_hard_interrupt"):
        result = _run_single_child(0, "stall classification", child, _parent())
    release.set()
    assert result["status"] == "stalled"
    assert result["timeout_seconds"] is None
    assert result["stall_reason"] == "inactivity"


def test_sync_watchdog_is_local_and_does_not_invoke_llm():
    calls = []

    def summary():
        return {"api_call_count": 1, "current_tool": None, "last_activity_ts": 1.0}

    child = _child(summary, lambda **_kwargs: {"final_response": "ok", "completed": True})
    parent = _parent()
    parent._touch_activity = lambda desc: calls.append(desc)
    with patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.01):
        result = _run_single_child(0, "local watchdog", child, parent)
    assert result["status"] == "completed"
    assert calls
    assert all("llm" not in str(value).lower() for value in calls)


def test_mutating_uncertain_stall_is_not_automatically_retried():
    """The lifecycle primitive fails closed when a side effect may have happened."""
    from tools.worker_lifecycle import WorkerLifecycle
    lifecycle = WorkerLifecycle(logical_task_id="mutation", now=1.0)
    lifecycle.mark_mutation_possible()
    lifecycle.request_cancellation(now=2.0)
    lifecycle.mark_cancellation_pending()
    lifecycle.confirm_termination(now=3.0)
    assert lifecycle.can_retry(termination_confirmed=True, replacement_generation=2) is False
    lifecycle.reconcile_mutation()
    assert lifecycle.can_retry(termination_confirmed=True, replacement_generation=2) is True
