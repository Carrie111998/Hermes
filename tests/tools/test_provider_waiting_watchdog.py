"""Provider waiting must not masquerade as forward progress."""
import threading
from unittest.mock import MagicMock, patch

from tools.delegate_tool import _run_single_child


def test_sync_provider_wait_heartbeats_do_not_reset_watchdog():
    release = threading.Event()
    child = MagicMock()
    ticks = {"n": 0}

    def summary():
        ticks["n"] += 1
        return {
            "api_call_count": 1,
            "current_tool": None,
            # Simulates the provider wait heartbeat advancing while no
            # response chunk, tool completion, or API completion arrives.
            "last_activity_ts": float(ticks["n"]),
            "last_activity_desc": "waiting for non-streaming API response",
        }

    child.get_activity_summary.side_effect = summary
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
    with patch(
        "tools.delegate_tool._get_sync_watchdog_settings",
        return_value=(0.01, 0.02, 0.02, 0.01, 0.0),
    ), patch("tools.delegate_tool.request_hard_interrupt"):
        result = _run_single_child(0, "blocked provider", child, parent)

    try:
        assert result["status"] == "stalled"
        assert result["stall_reason"] == "inactivity"
    finally:
        release.set()
        child._interrupt_requested = True


def test_stream_response_activity_is_meaningful_progress():
    from tools.worker_lifecycle import WorkerLifecycle

    lifecycle = WorkerLifecycle(logical_task_id="stream", now=1.0)
    assert lifecycle.observe_activity(
        {
            "api_call_count": 1,
            "last_activity_ts": 2.0,
            "last_activity_desc": "receiving stream response",
        },
        now=2.0,
    ) is True
    assert lifecycle.snapshot(now=2.0)["last_progress_kind"] == "api_call_completed"


def test_provider_wait_activity_is_not_meaningful_progress():
    from tools.worker_lifecycle import WorkerLifecycle

    lifecycle = WorkerLifecycle(logical_task_id="provider", now=1.0)
    lifecycle.observe_activity(
        {
            "api_call_count": 1,
            "last_activity_ts": 2.0,
            "last_activity_desc": "waiting for non-streaming API response",
        },
        now=2.0,
    )
    lifecycle.observe_activity(
        {
            "api_call_count": 1,
            "last_activity_ts": 3.0,
            "last_activity_desc": "waiting for non-streaming API response",
        },
        now=3.0,
    )
    assert lifecycle.snapshot(now=3.0)["seconds_since_progress"] == 2.0
    assert lifecycle.snapshot(now=3.0)["last_progress_kind"] == "started"
    assert lifecycle.snapshot(now=3.0)["worker_state"] == "running"


def test_tool_completion_activity_is_meaningful_progress():
    from tools.worker_lifecycle import WorkerLifecycle

    lifecycle = WorkerLifecycle(logical_task_id="tool", now=1.0)
    assert lifecycle.observe_activity(
        {
            "api_call_count": 1,
            "last_activity_ts": 2.0,
            "last_activity_desc": "tool results posted, continuing iteration #1",
        },
        now=2.0,
    ) is True
    assert lifecycle.snapshot(now=2.0)["last_progress_kind"] == "api_call_completed"
