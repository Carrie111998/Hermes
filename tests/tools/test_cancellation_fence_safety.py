"""Regression tests for uncertain cancellation and mutating retry fences."""
import threading
from unittest.mock import MagicMock, patch

from tools.delegate_tool import _run_single_child
from tools.worker_lifecycle import WorkerLifecycle, WorkerState


def test_fence_is_not_termination_confirmation():
    lifecycle = WorkerLifecycle(logical_task_id="fenced", now=1.0)
    lifecycle.transition(WorkerState.STALLED, now=2.0)
    lifecycle.request_cancellation(now=2.0)
    lifecycle.mark_cancellation_pending()
    lifecycle.fence(now=3.0)
    assert lifecycle.termination_confirmed() is False
    assert lifecycle.retry_decision(replacement_generation=2) == "cancellation_pending"


def test_sync_stall_does_not_claim_provider_termination():
    release = threading.Event()
    child = MagicMock()
    child.get_activity_summary.return_value = {
        "api_call_count": 0,
        "current_tool": None,
        "last_activity_ts": 1.0,
        "last_activity_desc": "",
    }
    child.run_conversation.side_effect = lambda **_kwargs: (
        release.wait(1.0), {"final_response": "late", "completed": True}
    )[1]
    child._delegate_saved_tool_names = []
    child._delegate_output_schema = None
    child._subagent_id = None
    child.model = "test-model"
    child.session_prompt_tokens = 0
    child.session_completion_tokens = 0
    child.session_reasoning_tokens = 0
    child.session_estimated_cost_usd = 0.0
    child.session_cost_status = "unknown"
    parent = MagicMock()
    parent._current_task_id = None
    parent._touch_activity = lambda _desc: None
    try:
        with patch(
            "tools.delegate_tool._get_sync_watchdog_settings",
            return_value=(0.01, 0.02, 0.02, 0.01, 0.0),
        ), patch("tools.delegate_tool.request_hard_interrupt"):
            result = _run_single_child(0, "uncertain cancellation", child, parent)
        assert result["status"] == "stalled"
        assert result["cancellation_state"] == "pending"
        assert result["cancellation_confirmed_at"] is None
    finally:
        release.set()


def test_mutating_tool_boundary_sets_fail_closed_retry_flag():
    lifecycle = WorkerLifecycle(logical_task_id="mutation", now=1.0)
    assert lifecycle.note_tool_event("tool.started", "write_file", mutating=True, now=2.0) is False
    assert lifecycle.snapshot(now=2.0)["mutation_possible"] is True
    lifecycle.transition(WorkerState.STALLED, now=3.0)
    lifecycle.request_cancellation(now=3.0)
    lifecycle.mark_cancellation_pending()
    lifecycle.fence(now=4.0)
    lifecycle.confirm_termination(now=5.0)
    assert lifecycle.retry_decision(replacement_generation=2) == "reconcile_mutation"
