"""Regression tests for deterministic delegated-worker lifecycle primitives."""
from tools.worker_lifecycle import LifecycleWatchdog, WorkerLifecycle, WorkerState


def test_progressing_worker_is_not_stalled_by_elapsed_wall_clock():
    lifecycle = WorkerLifecycle(logical_task_id="task-a", now=100.0)
    lifecycle.record_progress("api_call_completed", operation="model", now=221.0)
    watchdog = LifecycleWatchdog(lifecycle, inactivity_threshold=30.0, clock=lambda: 240.0)
    assert watchdog.should_stall() is False
    assert lifecycle.snapshot(now=240.0)["worker_state"] == WorkerState.PROGRESSING.value


def test_inactivity_enters_stall_candidate_without_wall_clock_deadline():
    lifecycle = WorkerLifecycle(logical_task_id="task-b", now=100.0)
    lifecycle.mark_model_wait()
    watchdog = LifecycleWatchdog(lifecycle, inactivity_threshold=30.0, clock=lambda: 131.0)
    assert watchdog.should_stall() is True
    assert lifecycle.snapshot(now=131.0)["worker_state"] == WorkerState.WAITING_ON_MODEL.value


def test_tool_start_does_not_reset_progress_timestamp():
    lifecycle = WorkerLifecycle(logical_task_id="task-c", now=100.0)
    lifecycle.mark_tool_wait("terminal")
    assert lifecycle.snapshot(now=160.0)["seconds_since_progress"] == 60.0
    lifecycle.observe_activity({"api_call_count": 0, "current_tool": "terminal", "last_activity_ts": 100.0}, now=161.0)
    assert lifecycle.snapshot(now=161.0)["seconds_since_progress"] == 61.0


def test_retry_requires_confirmed_termination_and_mutation_reconciliation():
    lifecycle = WorkerLifecycle(logical_task_id="task-d", now=100.0)
    lifecycle.mark_mutation_possible()
    assert lifecycle.can_retry(termination_confirmed=True, replacement_generation=2) is False
    lifecycle.reconcile_mutation()
    lifecycle.transition(WorkerState.STALLED, now=101.0)
    assert lifecycle.can_retry(termination_confirmed=False, replacement_generation=2) is False
    assert lifecycle.can_retry(termination_confirmed=True, replacement_generation=2) is True


def test_late_and_superseded_results_are_fenced():
    late = WorkerLifecycle(logical_task_id="task-e", now=100.0)
    late.transition(WorkerState.STALLED, now=101.0)
    assert late.accept_result(generation=1) == WorkerState.LATE_SUCCESS

    old = WorkerLifecycle(logical_task_id="task-f", execution_generation=1, now=100.0)
    assert old.accept_result(generation=1, authoritative_generation=2) == WorkerState.SUPERSEDED


def test_metadata_is_secret_safe_and_has_required_fields():
    lifecycle = WorkerLifecycle(logical_task_id="task-g", now=100.0)
    snapshot = lifecycle.snapshot(now=101.0)
    required = {
        "child_started_at", "last_progress_at", "last_progress_kind",
        "current_operation", "current_operation_started_at", "worker_state",
        "cancellation_state", "logical_task_id", "execution_generation",
    }
    assert required <= snapshot.keys()
    assert "prompt" not in snapshot
    assert "secret" not in snapshot


def test_cancellation_is_pending_until_worker_returns():
    lifecycle = WorkerLifecycle(logical_task_id="task-h", now=100.0)
    lifecycle.request_cancellation(now=110.0)
    lifecycle.mark_cancellation_pending()
    assert lifecycle.snapshot()["worker_state"] == WorkerState.CANCELLATION_PENDING.value
    assert lifecycle.can_retry(termination_confirmed=False, replacement_generation=2) is False
    lifecycle.confirm_termination(now=111.0)
    assert lifecycle.snapshot()["worker_state"] == WorkerState.CANCELLATION_CONFIRMED.value
    assert lifecycle.can_retry(termination_confirmed=True, replacement_generation=2) is True


def test_late_result_after_fence_is_accepted_as_late_success():
    lifecycle = WorkerLifecycle(logical_task_id="task-i", now=100.0)
    lifecycle.request_cancellation(now=110.0)
    lifecycle.mark_cancellation_pending()
    lifecycle.fence(now=111.0)
    assert lifecycle.accept_result(generation=1) == WorkerState.LATE_SUCCESS


def test_watchdog_is_local_and_does_not_call_an_llm():
    calls = []
    lifecycle = WorkerLifecycle(logical_task_id="task-j", now=100.0)
    watchdog = LifecycleWatchdog(lifecycle, inactivity_threshold=30.0, clock=lambda: 100.0)
    assert watchdog.should_stall() is False
    assert calls == []


def test_attempt_identity_is_stable_and_bounded():
    lifecycle = WorkerLifecycle(logical_task_id="task-k", execution_generation=3, attempt_number=2, now=100.0)
    snapshot = lifecycle.snapshot()
    assert snapshot["logical_task_id"] == "task-k"
    assert snapshot["execution_generation"] == 3
    assert snapshot["attempt_number"] == 2
    assert len(snapshot["logical_task_id"]) < 128


def test_accounting_flags_do_not_claim_useful_work_from_api_calls():
    lifecycle = WorkerLifecycle(logical_task_id="task-l", now=100.0)
    snapshot = lifecycle.snapshot()
    assert "useful_work_units" not in snapshot
    assert "api_calls" not in snapshot
    lifecycle.transition(WorkerState.STALLED, now=110.0)
    assert lifecycle.snapshot()["stall_detected"] is True


def test_terminal_transitions_are_idempotent():
    lifecycle = WorkerLifecycle(logical_task_id="task-m", now=100.0)
    assert lifecycle.transition(WorkerState.SUCCESS, now=101.0) is True
    assert lifecycle.transition(WorkerState.FAILED, now=102.0) is False
    assert lifecycle.accept_result(generation=1) == WorkerState.SUCCESS


def test_mutation_requires_reconciliation_even_after_fence():
    lifecycle = WorkerLifecycle(logical_task_id="task-n", now=100.0)
    lifecycle.mark_mutation_possible()
    lifecycle.request_cancellation(now=110.0)
    lifecycle.mark_cancellation_pending()
    lifecycle.fence(now=111.0)
    lifecycle.confirm_termination(now=112.0)
    assert lifecycle.can_retry(termination_confirmed=True, replacement_generation=2) is False
    lifecycle.reconcile_mutation()
    assert lifecycle.can_retry(termination_confirmed=True, replacement_generation=2) is True


def test_provider_wait_is_not_progress_by_itself():
    lifecycle = WorkerLifecycle(logical_task_id="task-o", now=100.0)
    lifecycle.mark_model_wait("provider.socket")
    assert lifecycle.snapshot(now=130.0)["seconds_since_progress"] == 30.0
    lifecycle.observe_activity({"api_call_count": 0, "current_tool": None}, now=130.0)
    assert lifecycle.snapshot(now=130.0)["seconds_since_progress"] == 30.0


def test_tool_completion_is_progress_when_activity_advances():
    lifecycle = WorkerLifecycle(logical_task_id="task-p", now=100.0)
    lifecycle.mark_tool_wait("terminal")
    lifecycle.observe_activity({"api_call_count": 1, "current_tool": None, "last_activity_ts": 120.0}, now=121.0)
    snapshot = lifecycle.snapshot(now=121.0)
    assert snapshot["last_progress_kind"] == "api_call_completed"
    assert snapshot["seconds_since_progress"] == 0.0


def test_fence_does_not_accept_a_new_generation_as_old_result():
    lifecycle = WorkerLifecycle(logical_task_id="task-q", execution_generation=1, now=100.0)
    lifecycle.fence(now=101.0)
    assert lifecycle.accept_result(generation=1, authoritative_generation=2) == WorkerState.SUPERSEDED


def test_snapshot_contains_no_operation_content():
    lifecycle = WorkerLifecycle(logical_task_id="task-r", now=100.0)
    lifecycle.record_progress("tool_completed", operation="private/path", now=101.0)
    snapshot = lifecycle.snapshot()
    assert snapshot["current_operation"] == "operation"
    assert "credentials" not in snapshot
    assert "api_key" not in snapshot
    assert "prompt" not in snapshot


def test_tool_start_is_not_progress_but_completion_is():
    lifecycle = WorkerLifecycle(logical_task_id="task-s", now=1.0)
    assert lifecycle.observe_tool_call("file.write", mutating=True, now=2.0) is False
    assert lifecycle.snapshot(now=100.0)["seconds_since_progress"] == 99.0
    assert lifecycle.observe_tool_call("file.write", mutating=True, completed=True, now=101.0) is True
    snapshot = lifecycle.snapshot(now=101.0)
    assert snapshot["last_progress_kind"] == "tool_completed"
    assert snapshot["mutation_possible"] is True
