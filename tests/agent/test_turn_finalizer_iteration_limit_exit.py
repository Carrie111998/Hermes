"""Regression tests for iteration-limit exit normalization (#61631)."""

import json
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.turn_finalizer import finalize_turn
from hermes_cli import kanban_db as kb
from tests.attempt_fence_helpers import (
    create_bound_attempt,
    isolated_home,
    logical_board_snapshot,
    registered_current_process,
)


class _LimitAgent:
    def __init__(
        self,
        *,
        max_iterations=60,
        budget_remaining=0,
        completion_explainer=False,
    ):
        self.max_iterations = max_iterations
        self.iteration_budget = SimpleNamespace(
            remaining=budget_remaining, used=max_iterations, max_total=max_iterations
        )
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-test"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self.persisted_messages = None
        self._handle_max_iterations_called = False
        self._completion_explainer = completion_explainer

    def _handle_max_iterations(self, messages, api_call_count):
        self._handle_max_iterations_called = True
        return "summary from extra call"

    def _emit_status(self, *_args, **_kwargs):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _save_trajectory(self, *_args, **_kwargs):
        pass

    def _cleanup_task_resources(self, *_args, **_kwargs):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        pass

    def _persist_session(self, messages, conversation_history):
        self.persisted_messages = list(messages)

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return self._completion_explainer

    def _format_turn_completion_explanation(self, _reason):
        return "iteration-limit explanation"

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **_kwargs):
        pass


def _finalize(
    agent,
    *,
    final_response,
    exit_reason,
    api_call_count=60,
    pending_verification_response=None,
):
    return finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=api_call_count,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason=exit_reason,
        _pending_verification_response=pending_verification_response,
    )
















@pytest.mark.parametrize(
    ("exit_reason", "interrupted", "failed"),
    [
        ("interrupted_by_user", True, False),
        ("all_retries_exhausted_no_response", False, False),
        ("provider_failure", False, True),
    ],
)
def test_pending_response_does_not_mask_later_terminal_exit(
    monkeypatch, exit_reason, interrupted, failed
):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent()

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=60,
        interrupted=interrupted,
        failed=failed,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason=exit_reason,
        _pending_verification_response="stale premature report",
    )

    assert result["final_response"] is None
    assert result["turn_exit_reason"] == exit_reason
    assert result["completed"] is False
    assert agent._handle_max_iterations_called is False


def test_pending_response_records_kanban_timeout(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "41")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "claim-exact")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"failure_limit": 7}},
    )
    record = MagicMock(name="record_task_failure")
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda: conn)
    monkeypatch.setattr("hermes_cli.kanban_db._record_task_failure", record)
    agent = _LimitAgent()

    result = _finalize(
        agent,
        final_response=None,
        exit_reason="unknown",
        pending_verification_response="composed report",
    )

    assert result["turn_exit_reason"] == "max_iterations_reached(60/60)"
    record.assert_called_once_with(
        conn,
        "task-123",
        error=(
            "Iteration budget exhausted (60/60) — task could not complete "
            "within the allowed iterations"
        ),
        outcome="timed_out",
        failure_limit=7,
        release_claim=True,
        end_run=True,
        event_payload_extra={"budget_used": 60, "budget_max": 60},
        expected_run_id=41,
        expected_claim_lock="claim-exact",
    )


def test_iteration_timeout_without_exact_attempt_does_not_record(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_CLAIM_LOCK", raising=False)
    record = MagicMock(name="record_task_failure")
    monkeypatch.setattr("hermes_cli.kanban_db._record_task_failure", record)
    agent = _LimitAgent()

    result = _finalize(
        agent,
        final_response=None,
        exit_reason="unknown",
        pending_verification_response="composed report",
    )

    assert result["turn_exit_reason"] == "max_iterations_reached(60/60)"
    record.assert_not_called()


@pytest.mark.parametrize(
    ("raw_run_id", "claim_lock"),
    [
        ("", "claim-exact"),
        ("0", "claim-exact"),
        ("-1", "claim-exact"),
        ("+1", "claim-exact"),
        ("01", "claim-exact"),
        ("1.0", "claim-exact"),
        (" 1", "claim-exact"),
        ("1 ", "claim-exact"),
        ("not-an-integer", "claim-exact"),
        ("1", ""),
        ("1", " "),
    ],
)
def test_iteration_timeout_with_malformed_attempt_does_not_open_db(
    monkeypatch, raw_run_id, claim_lock
):
    """Malformed ownership input must fail before any board access."""
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", raw_run_id)
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claim_lock)
    connect = MagicMock(name="connect")
    record = MagicMock(name="record_task_failure")
    monkeypatch.setattr("hermes_cli.kanban_db.connect", connect)
    monkeypatch.setattr("hermes_cli.kanban_db._record_task_failure", record)

    _finalize(
        _LimitAgent(),
        final_response=None,
        exit_reason="unknown",
        pending_verification_response="composed report",
    )

    connect.assert_not_called()
    record.assert_not_called()


@pytest.mark.parametrize(
    "raw_limit",
    [
        True,
        False,
        1.0,
        2.5,
        "1",
        "2",
        " 2 ",
        "",
        None,
        0,
        -1,
        {},
        [],
    ],
)
def test_iteration_timeout_malformed_failure_limit_uses_default(
    monkeypatch, raw_limit
):
    """Only a positive built-in int satisfies the YAML config contract."""
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "41")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "claim-exact")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"failure_limit": raw_limit}},
    )
    record = MagicMock(name="record_task_failure")
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda: conn)
    monkeypatch.setattr("hermes_cli.kanban_db._record_task_failure", record)

    _finalize(
        _LimitAgent(),
        final_response=None,
        exit_reason="unknown",
        pending_verification_response="composed report",
    )

    assert record.call_args.kwargs["failure_limit"] == 2


def test_published_pending_candidate_is_not_duplicated_by_finalizer(monkeypatch):
    """When budget exhaustion preserves a verification candidate that is
    already the tail assistant message, the finalizer must NOT append a
    duplicate. The content-comparison guard prevents this. (#65919 §7)
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent()
    report = "the composed report"

    result = finalize_turn(
        agent,
        final_response=report,
        api_call_count=60,
        interrupted=False,
        failed=False,
        # The candidate is already in messages as the tail assistant.
        messages=[
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": report},
        ],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="unknown",
        _pending_verification_response=report,
    )

    # The tail assistant already matches final_response — no duplicate appended.
    roles = [m["role"] for m in result["messages"]]
    assert roles == ["user", "assistant"]
    # Persisted messages should also have no duplicate.
    assert agent.persisted_messages is not None
    persisted_roles = [m["role"] for m in agent.persisted_messages]
    assert persisted_roles == ["user", "assistant"]


@pytest.mark.macos_only
def test_late_iteration_finalizer_has_zero_db_delta(isolated_home, monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    conn = kb.connect()
    identity = kb._darwin_process_identity(os.getpgid(0))
    assert identity is not None
    task_id, stale_claim, _raw_fence = create_bound_attempt(
        conn, leader_identity=identity
    )
    fresh_claim_lock = "fresh:claim"
    now = int(time.time())
    fresh_run_id = conn.execute(
        "INSERT INTO task_runs(task_id, profile, status, claim_lock, "
        "claim_expires, started_at) VALUES (?, 'dor-coo', 'running', ?, ?, ?)",
        (task_id, fresh_claim_lock, now + 300, now),
    ).lastrowid
    fresh_fence = json.dumps(
        {
            "run_id": fresh_run_id,
            "claim_lock": fresh_claim_lock,
            "host": kb._host_id(),
            "leader_pid": identity.pid,
            "worker_pgid": identity.pgid,
            "worker_identity": identity.token,
            "reason": "running",
            "created_at": now,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        "UPDATE tasks SET current_run_id=?, claim_lock=?, claim_expires=?, "
        "worker_pid=?, worker_pgid=?, worker_identity=?, worker_fence=? "
        "WHERE id=?",
        (
            fresh_run_id, fresh_claim_lock, now + 300, identity.pid,
            identity.pgid, identity.token, fresh_fence, task_id,
        ),
    )
    conn.execute(
        "UPDATE task_runs SET worker_pid=?, worker_pgid=?, worker_identity=?, "
        "worker_fence=? WHERE id=?",
        (identity.pid, identity.pgid, identity.token, fresh_fence, fresh_run_id),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(stale_claim.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", stale_claim.claim_lock)
    before_conn = kb.connect()
    before = logical_board_snapshot(before_conn)
    before_conn.close()

    agent = _LimitAgent()
    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=agent.max_iterations,
        interrupted=False,
        failed=False,
        messages=[],
        conversation_history=[],
        effective_task_id=None,
        turn_id="late-finalizer",
        user_message="fixture",
        original_user_message="fixture",
        _should_review_memory=False,
        _turn_exit_reason="budget_exhausted",
    )

    assert result["final_response"] is not None
    after_conn = kb.connect()
    try:
        assert logical_board_snapshot(after_conn) == before
    finally:
        after_conn.close()


@pytest.mark.macos_only
def test_iteration_finalizer_uses_exact_attempt_and_configured_limit(
    registered_current_process, monkeypatch
):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"failure_limit": 1}},
    )
    fixture = registered_current_process
    monkeypatch.setenv("HERMES_KANBAN_TASK", fixture.task_id)
    monkeypatch.setenv(
        "HERMES_KANBAN_RUN_ID", str(fixture.claimed.current_run_id)
    )
    monkeypatch.setenv(
        "HERMES_KANBAN_CLAIM_LOCK", fixture.claimed.claim_lock
    )
    before_task = kb.get_task(fixture.conn, fixture.task_id)
    before_tuple = (
        before_task.current_run_id,
        before_task.claim_lock,
        before_task.worker_pid,
        before_task.worker_pgid,
        before_task.worker_identity,
        before_task.worker_fence,
    )

    agent = _LimitAgent()
    finalize_turn(
        agent,
        final_response=None,
        api_call_count=agent.max_iterations,
        interrupted=False,
        failed=False,
        messages=[],
        conversation_history=[],
        effective_task_id=None,
        turn_id="exact-finalizer",
        user_message="fixture",
        original_user_message="fixture",
        _should_review_memory=False,
        _turn_exit_reason="budget_exhausted",
    )

    task = kb.get_task(fixture.conn, fixture.task_id)
    assert task.status == "blocked"
    assert task.consecutive_failures == 1
    assert (
        task.current_run_id, task.claim_lock, task.worker_pid, task.worker_pgid,
        task.worker_identity, task.worker_fence,
    ) == before_tuple


@pytest.mark.macos_only
@pytest.mark.parametrize("operation", ["comment", "create", "link", "attachment"])
def test_late_worker_mutation_after_terminal_has_zero_delta(
    registered_current_process, monkeypatch, operation
):
    from tools import kanban_tools

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    fixture = registered_current_process
    target_id = kb.create_task(fixture.conn, title="late mutation target")
    attachment_dir = kb.task_attachments_dir(target_id)
    assert not attachment_dir.exists()
    assert kb.complete_task(
        fixture.conn,
        fixture.task_id,
        result="terminal",
        expected_run_id=fixture.claimed.current_run_id,
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", fixture.task_id)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_CLAIM_LOCK", raising=False)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "late-fixture")
    before = logical_board_snapshot(fixture.conn)

    if operation == "comment":
        result = kanban_tools._handle_comment({"task_id": target_id, "body": "late"})
        assert "error" in __import__("json").loads(result)
    elif operation == "create":
        result = kanban_tools._handle_create(
            {"title": "late child", "assignee": "dor-coo"}
        )
        assert "error" in __import__("json").loads(result)
    elif operation == "link":
        result = kanban_tools._handle_link(
            {"parent_id": fixture.task_id, "child_id": target_id}
        )
        assert "error" in __import__("json").loads(result)
    else:
        with pytest.raises(kb.StaleAttemptError):
            kb.store_attachment_bytes(
                fixture.conn, target_id, "late.txt", b"late", uploaded_by="dor-coo"
            )

    assert logical_board_snapshot(fixture.conn) == before
    assert not attachment_dir.exists()
