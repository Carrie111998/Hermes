"""Regression tests for iteration-limit exit normalization (#61631)."""

import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.turn_finalizer import finalize_turn
from agent import kanban_auto_handoff as handoff
from run_agent import AIAgent


@pytest.fixture(autouse=True)
def _isolate_pending_handoff_receipts_between_tests():
    """Keep each finalizer test's supervised receipts inside that test.

    Several tests deliberately exercise the real non-daemon receipt supervisor
    while replacing its persistence boundary with a per-test mock.  Waiting at
    both boundaries prevents a successful retry from crossing monkeypatch
    teardown and being observed by the following test's mock.
    """
    assert handoff.wait_for_pending_handoff_controls(timeout=5)
    yield
    assert handoff.wait_for_pending_handoff_controls(timeout=5)


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
        self._interrupt_requested = False
        self._pending_redirect = None
        self._pending_redirect_lock = threading.Lock()
        self._pending_steer = None
        self._pending_steer_lock = threading.Lock()
        self._pending_steer_receipts = []
        self._pending_steer_receipt_seq = 0
        self._pending_steer_inflight_receipt_ids = []
        self._model_request_active = threading.Event()
        self._auto_handoff_control_lock = threading.RLock()
        self._auto_handoff_control_phase = "idle"
        self._auto_handoff_control_source = None
        self._auto_handoff_control_target = None
        self._auto_handoff_control_events = []
        self._auto_handoff_control_seq = 0
        self._suppress_session_end_learning = False
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self.persisted_messages = None
        self.status_messages = []
        self._handle_max_iterations_called = False
        self._handle_max_iterations_kwargs = None
        self._completion_explainer = completion_explainer
        self._spawn_background_review = MagicMock(name="spawn_background_review")
        self._sync_external_memory_for_turn = MagicMock(
            name="sync_external_memory_for_turn"
        )

    _queue_auto_handoff_control = AIAgent._queue_auto_handoff_control
    _persist_auto_handoff_control_event = (
        AIAgent._persist_auto_handoff_control_event
    )
    _snapshot_unconsumed_steer_receipts = (
        AIAgent._snapshot_unconsumed_steer_receipts
    )

    def _handle_max_iterations(self, messages, api_call_count, **_kwargs):
        self._handle_max_iterations_called = True
        self._handle_max_iterations_kwargs = dict(_kwargs)
        return "summary from extra call"

    def _emit_status(self, message, *_args, **_kwargs):
        self.status_messages.append(message)

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
        with self._pending_steer_lock:
            text = self._pending_steer
            self._pending_steer = None
        return text

    def clear_interrupt(self):
        self._interrupt_requested = False
        self._interrupt_message = None
        self._pending_redirect = None

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


def _arm_managed_hard_budget(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "77")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "claim-77")
    monkeypatch.setattr(
        handoff,
        "resolve_policy",
        lambda *_args, **_kwargs: SimpleNamespace(enabled=True),
    )
    monkeypatch.setattr(
        handoff,
        "worker_failure_limit",
        lambda *, strict: 2,
    )


def _hard_receipt(
    control_id,
    seq,
    kind,
    message,
    *,
    delivery_slot="steer",
    injected=False,
):
    return {
        "control_id": control_id,
        "seq": seq,
        "kind": kind,
        "message": message,
        "delivery_slot": delivery_slot,
        "injected": injected,
    }


def _finalize_managed_hard_budget(
    agent,
    *,
    messages=None,
    should_review_memory=False,
):
    agent._managed_short_task_bootstrap_verified = True
    return finalize_turn(
        agent,
        final_response=None,
        api_call_count=agent.max_iterations,
        interrupted=False,
        failed=False,
        messages=(messages if messages is not None else [{"role": "user", "content": "task"}]),
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=should_review_memory,
        _turn_exit_reason="unknown",
    )


def test_pending_verify_response_is_preserved_for_cron_delivery(monkeypatch):
    """A held-back verification response survives last-turn exhaustion."""
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent()
    report = "complete cron report body"

    result = _finalize(
        agent,
        final_response=None,
        exit_reason="unknown",
        pending_verification_response=report,
    )

    assert result["final_response"] == report
    assert result["turn_exit_reason"] == "max_iterations_reached(60/60)"
    assert agent._handle_max_iterations_called is False


def test_pending_pre_verify_response_is_preserved_on_budget_exhaustion(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent()
    report = "budget exhausted but complete"

    result = _finalize(
        agent,
        final_response=None,
        exit_reason="budget_exhausted",
        pending_verification_response=report,
    )

    assert result["final_response"] == report
    assert result["turn_exit_reason"] == "max_iterations_reached(60/60)"
    assert agent._handle_max_iterations_called is False


def test_empty_pending_verification_response_uses_summary_fallback(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent()

    result = _finalize(
        agent,
        final_response=None,
        exit_reason="unknown",
        pending_verification_response="",
    )

    assert result["final_response"] == "summary from extra call"
    assert result["turn_exit_reason"] == "max_iterations_reached(60/60)"
    assert agent._handle_max_iterations_called is True
    assert agent.status_messages == [
        "⚠️ 本轮处理已达到预设上限，正在整理当前进展。"
    ]


def test_short_generated_summary_keeps_abnormal_turn_explainer(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent(completion_explainer=True)
    agent._handle_max_iterations = lambda *_args: "The"

    result = _finalize(agent, final_response=None, exit_reason="unknown")

    assert result["final_response"] == "The\n\niteration-limit explanation"


def test_short_preserved_verification_response_is_not_rewritten(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent(completion_explainer=True)

    result = _finalize(
        agent,
        final_response=None,
        exit_reason="unknown",
        pending_verification_response="The",
    )

    assert result["final_response"] == "The"


def test_text_response_exit_not_rewritten_at_iteration_limit(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent(budget_remaining=5)
    exit_reason = "text_response(finish_reason=stop)"

    result = _finalize(
        agent,
        final_response="normal answer",
        exit_reason=exit_reason,
        api_call_count=59,
    )

    assert result["turn_exit_reason"] == exit_reason
    assert agent._handle_max_iterations_called is False


@pytest.mark.parametrize(
    "exit_reason",
    [
        "error_near_max_iterations(boom)",
        "guardrail_halt",
        "partial_stream_recovery",
        "fallback_prior_turn_content",
        "empty_response_exhausted",
    ],
)
def test_unrelated_non_success_response_is_not_reclassified(monkeypatch, exit_reason):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent()

    result = _finalize(
        agent,
        final_response="diagnostic or partial content",
        exit_reason=exit_reason,
    )

    assert result["turn_exit_reason"] == exit_reason
    assert result["completed"] is False
    assert agent._handle_max_iterations_called is False


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
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "123")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "claim-123")
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
        summary="composed report",
        failure_limit=2,
        release_claim=True,
        end_run=True,
        event_payload_extra={"budget_used": 60, "budget_max": 60},
    )


def test_iteration_fallback_checkpoint_reaches_fresh_managed_worker(
    tmp_path, monkeypatch
):
    """The 90/90 backstop must carry its summary into the retry context."""
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="bounded retry", assignee="default"
        )
        task = kb.claim_task(conn, task_id, claimer="managed-worker")
        assert task is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                (os.getpid(), task_id),
            )
            conn.execute(
                """
                UPDATE task_runs
                   SET worker_pid = ?, owner_node_id = 'test-node',
                       owner_boot_id = 'test-boot',
                       worker_start_token = 'test-start', worker_pgid = ?,
                       handoff_safety_required = 1
                 WHERE id = ?
                """,
                (os.getpid(), os.getpid(), int(task.current_run_id)),
            )

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(task.claim_lock))
    monkeypatch.setenv(
        handoff.POLICY_SNAPSHOT_ENV,
        handoff.encode_dispatcher_policy_snapshot(
            {
                "agent": {"max_turns": 60},
                "kanban": {
                    "failure_limit": 2,
                    "short_task_handoff": {
                        "enabled": True,
                        "soft_iteration_limit": 36,
                        "max_handoffs": 8,
                    },
                },
            }
        ),
    )
    agent = _LimitAgent()
    result = _finalize(agent, final_response=None, exit_reason="unknown")

    assert result["final_response"] == "summary from extra call"
    with kb.connect_closing() as conn:
        parked = kb.get_task(conn, task_id)
        ended = kb.latest_run(conn, task_id)
        assert parked.status == "todo"
        assert ended.summary == "summary from extra call"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_exit_gates "
            "WHERE child_task_id = ? AND released_at IS NULL",
            (task_id,),
        ).fetchone()[0] == 1

        monkeypatch.setattr(
            kb, "_exit_gate_release_reason", lambda _row: "test_exit"
        )
        assert kb.release_handoff_exit_gates(conn) == 1
        fresh = kb.claim_task(conn, task_id, claimer="fresh-worker")
        assert fresh is not None
        assert "summary from extra call" in kb.build_worker_context(conn, task_id)


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


def test_auto_handoff_returns_machine_readable_successor_and_skips_learning(monkeypatch):
    plugin_hook = MagicMock(name="plugin_hook", return_value=[])
    context_turn_hook = MagicMock(name="context_turn_hook")
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", plugin_hook)
    monkeypatch.setattr(
        "agent.conversation_loop._notify_context_engine_turn_complete",
        context_turn_hook,
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    monkeypatch.setattr(
        "agent.kanban_auto_handoff.create_successor_and_close",
        lambda **_kw: {
            "status": "handed_off",
            "task_id": "task-123",
            "successor_task_id": "task-456",
            "generation": 1,
        },
    )
    agent = _LimitAgent(max_iterations=90, budget_remaining=54)
    agent._managed_short_task_bootstrap_verified = True
    agent.iteration_budget.used = 36

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=36,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=True,
        _turn_exit_reason="kanban_auto_handoff_requested",
    )

    assert result["completed"] is True
    assert result["auto_handoff"]["successor_task_id"] == "task-456"
    assert result["turn_exit_reason"] == "kanban_auto_handoff(successor=task-456)"
    assert agent.status_messages == [
        "🔄 正在整理本段进展，准备自动交接。",
        "✅ 本段工作已收口，系统正在自动接续下一段。",
    ]
    assert all("task-456" not in message for message in agent.status_messages)
    assert "planned short-task checkpoint" in (
        agent._handle_max_iterations_kwargs["summary_request"]
    )
    assert "Short-task checkpoint" in agent._handle_max_iterations_kwargs["status_label"]
    assert "iteration_budget" not in result
    agent._spawn_background_review.assert_not_called()
    agent._sync_external_memory_for_turn.assert_not_called()
    plugin_hook.assert_not_called()
    context_turn_hook.assert_not_called()
    assert agent._suppress_session_end_learning is True


def test_auto_handoff_shutdown_closes_providers_without_learning():
    """Checkpoint teardown releases resources but never extracts shared memory."""
    memory_manager = MagicMock(name="memory_manager")
    context_engine = MagicMock(name="context_engine")
    agent = SimpleNamespace(
        _memory_manager=memory_manager,
        context_compressor=context_engine,
        session_id="sess-checkpoint",
        _suppress_session_end_learning=True,
    )

    # A session rotation can run before process teardown.  It must neither
    # ingest the checkpoint nor consume the marker needed by shutdown.
    AIAgent.commit_memory_session(
        agent,
        [{"role": "assistant", "content": "provisional checkpoint"}],
    )
    memory_manager.on_session_end.assert_not_called()
    context_engine.on_session_end.assert_not_called()
    assert agent._suppress_session_end_learning is True

    AIAgent.shutdown_memory_provider(
        agent,
        [{"role": "assistant", "content": "provisional checkpoint"}],
    )

    memory_manager.on_session_end.assert_not_called()
    memory_manager.shutdown_all.assert_called_once()
    context_engine.on_session_end.assert_not_called()
    assert agent._suppress_session_end_learning is False

    # The marker is one-shot: a later real session boundary keeps the
    # repository's baseline learning behaviour.
    AIAgent.shutdown_memory_provider(
        agent,
        [{"role": "assistant", "content": "real completed session"}],
    )
    memory_manager.on_session_end.assert_called_once()
    context_engine.on_session_end.assert_called_once()


def test_auto_handoff_safety_limit_is_not_false_completion(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        "agent.kanban_auto_handoff.create_successor_and_close",
        lambda **_kw: {
            "status": "safety_limit",
            "task_id": "task-123",
            "generation": 9,
        },
    )
    agent = _LimitAgent(max_iterations=90, budget_remaining=54)
    agent._managed_short_task_bootstrap_verified = True

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=36,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=True,
        _turn_exit_reason="kanban_auto_handoff_requested",
    )

    assert result["completed"] is False
    assert result["failed"] is False
    assert result["turn_exit_reason"] == "kanban_auto_handoff_safety_limit"
    assert result["final_response"].endswith(
        "自动接力已达到本次设置的安全上限，工作已暂停，等待你确认是否继续。"
    )
    agent._spawn_background_review.assert_not_called()
    agent._sync_external_memory_for_turn.assert_not_called()


def test_auto_handoff_noncommit_message_is_plain_chinese(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        "agent.kanban_auto_handoff.create_successor_and_close",
        lambda **_kw: {"status": "failed", "task_id": "task-123"},
    )
    agent = _LimitAgent(max_iterations=90, budget_remaining=54)
    agent._managed_short_task_bootstrap_verified = True

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=36,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=True,
        _turn_exit_reason="kanban_auto_handoff_requested",
    )

    assert result["completed"] is False
    assert result["failed"] is True
    assert result["final_response"].endswith(
        "自动接力暂时未能安全完成，当前工作没有被误报为完成。"
        "请稍后查看状态或重试。"
    )
    assert "task-123" not in result["final_response"]


def test_auto_handoff_commit_failure_is_not_false_completion(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])

    def fail_handoff(**_kwargs):
        raise RuntimeError("stale worker CAS")

    monkeypatch.setattr(
        "agent.kanban_auto_handoff.create_successor_and_close", fail_handoff
    )
    agent = _LimitAgent(max_iterations=90, budget_remaining=54)
    agent._managed_short_task_bootstrap_verified = True

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=36,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=True,
        _turn_exit_reason="kanban_auto_handoff_requested",
    )

    assert result["completed"] is False
    assert result["failed"] is True
    assert result["auto_handoff"]["status"] == "failed"
    assert result["turn_exit_reason"] == "kanban_auto_handoff_failed"
    assert result["final_response"].endswith(
        "自动接力的进度保存失败，当前工作没有被误报为完成。"
        "系统已停止继续接力，避免重复执行。"
    )
    agent._spawn_background_review.assert_not_called()
    agent._sync_external_memory_for_turn.assert_not_called()


@pytest.mark.parametrize(
    ("direction", "expected_message"),
    [
        ("hard_stop", "/stop"),
        ("redirect", "change the implementation approach"),
        ("steer", "check the migration first"),
    ],
)
def test_user_direction_during_checkpoint_summary_prevents_handoff(
    monkeypatch,
    direction,
    expected_message,
):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "77")
    commit = MagicMock(name="create_successor_and_close")
    monkeypatch.setattr(
        "agent.kanban_auto_handoff.create_successor_and_close",
        commit,
    )
    persist = MagicMock(
        name="persist_handoff_control",
        return_value={"status": "recorded", "target_task_id": "task-123"},
    )
    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr("hermes_cli.kanban_db.persist_handoff_control", persist)
    agent = _LimitAgent(max_iterations=90, budget_remaining=54)

    def summary_with_direction(messages, _api_call_count, **_kwargs):
        assert agent._model_request_active.is_set()
        messages.extend(
            [
                {"role": "user", "content": "synthetic checkpoint request"},
                {"role": "assistant", "content": "stale checkpoint summary"},
            ]
        )
        kind = "stop" if direction == "hard_stop" else direction
        queued = agent._queue_auto_handoff_control(kind, expected_message)
        assert queued["state"] == "queued"
        return "stale checkpoint summary"

    agent._handle_max_iterations = summary_with_direction
    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=36,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="kanban_auto_handoff_requested",
    )

    commit.assert_not_called()
    assert result["completed"] is False
    assert result["failed"] is False
    assert result["interrupted"] is True
    assert result["auto_handoff"]["status"] == "cancelled_by_user_direction"
    assert result["auto_handoff"]["persisted"] is True
    assert result["auto_handoff"]["control_kind"] == (
        "stop" if direction == "hard_stop" else direction
    )
    assert result["turn_exit_reason"] == "interrupted_by_user_direction"
    assert result["messages"] == [{"role": "user", "content": "task"}]
    assert result.get("pending_steer") is None
    assert persist.call_count == 2
    for call in persist.call_args_list:
        assert call.kwargs["phase"] == "before_commit"
        assert call.kwargs["message"] == expected_message
        assert call.kwargs["expected_run_id"] == 77
    assert not agent._model_request_active.is_set()


def test_soft_checkpoint_preserves_pre_window_and_during_summary_receipts_once(
    monkeypatch,
):
    """Pre-window and live directions each retain one exact durable identity."""
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "77")
    agent = _LimitAgent(max_iterations=90, budget_remaining=54)
    agent._pending_redirect = "pre-window A"
    agent._interrupt_requested = True
    agent._pending_steer_receipts = [
        _hard_receipt(
            "hc-soft-pre-window",
            1,
            "redirect",
            "pre-window A",
            delivery_slot="redirect",
        )
    ]

    def summarize_with_second_redirect(*_args, **_kwargs):
        queued = agent._queue_auto_handoff_control(
            "redirect", "during-summary B"
        )
        assert queued["state"] == "queued"
        return "discarded checkpoint summary"

    agent._handle_max_iterations = summarize_with_second_redirect
    persist = MagicMock(return_value={"status": "recorded"})
    create = MagicMock(name="create_successor_and_close")
    monkeypatch.setattr(
        handoff, "persist_worker_handoff_control", persist
    )
    monkeypatch.setattr(handoff, "create_successor_and_close", create)

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=36,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="kanban_auto_handoff_requested",
    )

    create.assert_not_called()
    assert handoff.wait_for_pending_handoff_controls(timeout=5)
    controls = {
        call.args[0]["control_id"]: call.args[0]
        for call in persist.call_args_list
    }
    assert set(controls) == {
        "hc-soft-pre-window",
        agent._auto_handoff_control_events[1]["control_id"],
    }
    assert controls["hc-soft-pre-window"]["phase"] == "before_commit"
    assert controls["hc-soft-pre-window"]["message"] == "pre-window A"
    follower = next(
        control
        for control_id, control in controls.items()
        if control_id != "hc-soft-pre-window"
    )
    assert follower["phase"] == "after_terminal"
    assert follower["message"] == "during-summary B"
    assert result["turn_exit_reason"] == "interrupted_by_user_direction"
    assert result["failed"] is False


def test_stop_waiting_on_commit_is_routed_to_gated_successor(monkeypatch):
    """A stop that loses the commit race is persisted after commit, not cleared."""
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    monkeypatch.setattr(
        handoff, "stage_pending_handoff_control", _stage_without_supervisor
    )
    entered_commit = threading.Event()
    allow_commit = threading.Event()

    def commit_handoff(**_kwargs):
        entered_commit.set()
        assert allow_commit.wait(timeout=5)
        return {
            "status": "handed_off",
            "task_id": "task-123",
            "successor_task_id": "task-456",
            "generation": 1,
        }

    monkeypatch.setattr(
        "agent.kanban_auto_handoff.create_successor_and_close", commit_handoff
    )
    agent = _LimitAgent(max_iterations=90, budget_remaining=54)
    routed = []
    agent._persist_auto_handoff_control_event = lambda event: (
        routed.append(dict(event)) or True
    )
    result_box = {}

    def finalize_worker():
        result_box["result"] = finalize_turn(
            agent,
            final_response=None,
            api_call_count=36,
            interrupted=False,
            failed=False,
            messages=[{"role": "user", "content": "task"}],
            conversation_history=[],
            effective_task_id="task",
            turn_id="turn",
            user_message="task",
            original_user_message="task",
            _should_review_memory=False,
            _turn_exit_reason="kanban_auto_handoff_requested",
        )

    finalize_thread = threading.Thread(target=finalize_worker)
    finalize_thread.start()
    assert entered_commit.wait(timeout=5)

    control_box = {}
    control_thread = threading.Thread(
        target=lambda: control_box.setdefault(
            "result", agent._queue_auto_handoff_control("stop", "/stop")
        )
    )
    control_thread.start()
    allow_commit.set()
    finalize_thread.join(timeout=5)
    control_thread.join(timeout=5)

    assert not finalize_thread.is_alive()
    assert not control_thread.is_alive()
    assert result_box["result"]["auto_handoff"]["successor_task_id"] == "task-456"
    assert control_box["result"]["state"] == "routed"
    assert control_box["result"]["accepted"] is True
    assert len(routed) == 1
    assert routed[0]["target_task_id"] == "task-456"
    assert routed[0]["kind"] == "stop"


def test_stop_waiting_on_failed_commit_is_routed_back_to_current_task(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "77")
    monkeypatch.setattr(
        handoff, "stage_pending_handoff_control", _stage_without_supervisor
    )
    entered_commit = threading.Event()
    allow_failure = threading.Event()

    def fail_commit(**_kwargs):
        entered_commit.set()
        assert allow_failure.wait(timeout=5)
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(
        "agent.kanban_auto_handoff.create_successor_and_close", fail_commit
    )
    agent = _LimitAgent(max_iterations=90, budget_remaining=54)
    routed = []
    agent._persist_auto_handoff_control_event = lambda event: (
        routed.append(dict(event)) or True
    )
    result_box = {}

    def finalize_worker():
        result_box["result"] = finalize_turn(
            agent,
            final_response=None,
            api_call_count=36,
            interrupted=False,
            failed=False,
            messages=[{"role": "user", "content": "task"}],
            conversation_history=[],
            effective_task_id="task",
            turn_id="turn",
            user_message="task",
            original_user_message="task",
            _should_review_memory=False,
            _turn_exit_reason="kanban_auto_handoff_requested",
        )

    finalizer_thread = threading.Thread(target=finalize_worker)
    finalizer_thread.start()
    assert entered_commit.wait(timeout=5)
    stop_box = {}
    stop_thread = threading.Thread(
        target=lambda: stop_box.setdefault(
            "result", agent._queue_auto_handoff_control("stop", "/stop")
        )
    )
    stop_thread.start()
    allow_failure.set()
    finalizer_thread.join(timeout=5)
    stop_thread.join(timeout=5)

    assert result_box["result"]["auto_handoff"]["status"] == "failed"
    assert stop_box["result"]["state"] == "routed"
    assert stop_box["result"]["accepted"] is True
    assert len(routed) == 1
    assert routed[0]["phase"] == "commit_failed"
    assert routed[0]["target_task_id"] == "task-123"
    assert routed[0]["kind"] == "stop"


def test_stop_waits_until_veto_self_gate_is_persisted(monkeypatch):
    """No gap exists between choosing veto and creating its durable self gate."""
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "77")
    monkeypatch.setattr(
        handoff, "stage_pending_handoff_control", _stage_without_supervisor
    )
    entered_persist = threading.Event()
    allow_persist = threading.Event()
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda: conn)

    def persist_veto(*_args, **_kwargs):
        entered_persist.set()
        assert allow_persist.wait(timeout=5)
        return {"status": "recorded", "target_task_id": "task-123"}

    monkeypatch.setattr("hermes_cli.kanban_db.persist_handoff_control", persist_veto)
    agent = _LimitAgent(max_iterations=90, budget_remaining=54)
    routed = []
    agent._persist_auto_handoff_control_event = lambda event: (
        routed.append(dict(event)) or True
    )

    def summary_with_redirect(messages, _api_call_count, **_kwargs):
        queued = agent._queue_auto_handoff_control(
            "redirect", "change implementation"
        )
        assert queued["state"] == "queued"
        return "discarded summary"

    agent._handle_max_iterations = summary_with_redirect
    result_box = {}

    def finalize_worker():
        result_box["result"] = finalize_turn(
            agent,
            final_response=None,
            api_call_count=36,
            interrupted=False,
            failed=False,
            messages=[{"role": "user", "content": "task"}],
            conversation_history=[],
            effective_task_id="task",
            turn_id="turn",
            user_message="task",
            original_user_message="task",
            _should_review_memory=False,
            _turn_exit_reason="kanban_auto_handoff_requested",
        )

    finalizer_thread = threading.Thread(target=finalize_worker)
    finalizer_thread.start()
    assert entered_persist.wait(timeout=5)
    stop_box = {}
    stop_thread = threading.Thread(
        target=lambda: stop_box.setdefault(
            "result", agent._queue_auto_handoff_control("stop", "/stop")
        )
    )
    stop_thread.start()
    assert stop_thread.is_alive()
    allow_persist.set()
    finalizer_thread.join(timeout=5)
    stop_thread.join(timeout=5)

    assert not finalizer_thread.is_alive()
    assert not stop_thread.is_alive()
    assert result_box["result"]["auto_handoff"]["persisted"] is True
    assert stop_box["result"]["state"] == "routed"
    assert stop_box["result"]["accepted"] is True
    assert len(routed) == 1
    assert routed[0]["target_task_id"] == "task-123"
    assert routed[0]["kind"] == "stop"


def test_non_handoff_turn_preserves_existing_background_learning(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent()

    finalize_turn(
        agent,
        final_response="partial diagnostic",
        api_call_count=60,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=True,
        _turn_exit_reason="provider_failure",
    )

    agent._spawn_background_review.assert_called_once()
    agent._sync_external_memory_for_turn.assert_called_once()


def test_terminal_verification_failure_is_persisted_as_one_correction(monkeypatch):
    """When verification fails terminally (nudge present but budget exhausted),
    the finalizer drops the synthetic nudge and the assistant candidate
    persists as a single correction. No duplicate assistant appended. (#65919 §7)
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent()
    report = "terminal failure correction"

    result = finalize_turn(
        agent,
        final_response=report,
        api_call_count=60,
        interrupted=False,
        failed=False,
        messages=[
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": report},
            # Synthetic nudge — should be dropped by _drop_verification_continuation_scaffolding.
            {"role": "user", "content": "[System: run tests]", "_verification_stop_synthetic": True},
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

    # The nudge is dropped; the assistant candidate is the tail and matches
    # final_response, so no duplicate is appended.
    roles = [m["role"] for m in result["messages"]]
    assert roles == ["user", "assistant"]
    # The nudge is gone from persisted messages too.
    assert agent.persisted_messages is not None
    persisted_contents = [m.get("content") for m in agent.persisted_messages]
    assert "[System: run tests]" not in persisted_contents
    assert report in persisted_contents


def test_hard_budget_two_tool_redirect_receipts_are_persisted_once(monkeypatch):
    _arm_managed_hard_budget(monkeypatch)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent(max_iterations=90)
    agent._pending_steer = "redirect A\nredirect B"
    agent._pending_steer_receipts = [
        _hard_receipt(
            "hc-redirect-a",
            1,
            "redirect",
            "redirect A",
            delivery_slot="steer",
        ),
        _hard_receipt(
            "hc-redirect-b",
            2,
            "redirect",
            "redirect B",
            delivery_slot="steer",
        ),
    ]
    persist = MagicMock(return_value={"status": "recorded"})
    timeout_record = MagicMock(name="record_managed_timeout")
    monkeypatch.setattr(handoff, "persist_worker_handoff_control", persist)
    monkeypatch.setattr(
        "hermes_cli.kanban_db._record_managed_task_failure_exact",
        timeout_record,
    )

    result = _finalize_managed_hard_budget(agent)

    timeout_record.assert_not_called()
    assert handoff.wait_for_pending_handoff_controls(timeout=5)
    controls = {
        call.args[0]["control_id"]: call.args[0]
        for call in persist.call_args_list
    }
    assert set(controls) == {"hc-redirect-a", "hc-redirect-b"}
    assert controls["hc-redirect-a"]["phase"] == "before_commit"
    assert controls["hc-redirect-a"]["message"] == "redirect A"
    assert controls["hc-redirect-b"]["phase"] == "after_terminal"
    assert controls["hc-redirect-b"]["message"] == "redirect B"
    assert result["turn_exit_reason"] == "interrupted_by_user_direction"


def test_hard_budget_pre_window_and_during_summary_redirects_survive_once(
    monkeypatch,
):
    _arm_managed_hard_budget(monkeypatch)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent(max_iterations=90)
    agent._pending_redirect = "pre-window A"
    agent._interrupt_requested = True
    agent._pending_steer_receipts = [
        _hard_receipt(
            "hc-pre-window",
            1,
            "redirect",
            "pre-window A",
            delivery_slot="redirect",
        )
    ]

    def summarize_with_second_redirect(*_args, **_kwargs):
        queued = agent._queue_auto_handoff_control(
            "redirect",
            "during-summary B",
        )
        assert queued["state"] == "queued"
        return "discarded hard-budget summary"

    agent._handle_max_iterations = summarize_with_second_redirect
    persist = MagicMock(return_value={"status": "recorded"})
    monkeypatch.setattr(handoff, "persist_worker_handoff_control", persist)

    result = _finalize_managed_hard_budget(agent)

    assert handoff.wait_for_pending_handoff_controls(timeout=5)
    controls = {
        call.args[0]["control_id"]: call.args[0]
        for call in persist.call_args_list
    }
    assert controls["hc-pre-window"]["phase"] == "before_commit"
    assert controls["hc-pre-window"]["message"] == "pre-window A"
    follower = next(
        control
        for control_id, control in controls.items()
        if control_id != "hc-pre-window"
    )
    assert follower["phase"] == "after_terminal"
    assert follower["message"] == "during-summary B"
    assert result["turn_exit_reason"] == "interrupted_by_user_direction"


def test_injected_receipt_at_hard_budget_prevents_timeout(monkeypatch):
    _arm_managed_hard_budget(monkeypatch)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent(max_iterations=90)
    agent._pending_steer = None
    agent._pending_steer_receipts = [
        _hard_receipt(
            "hc-injected",
            1,
            "steer",
            "inspect the final migration",
            injected=True,
        )
    ]
    persist = MagicMock(return_value={"status": "recorded"})
    timeout_record = MagicMock(name="record_managed_timeout")
    monkeypatch.setattr(handoff, "persist_worker_handoff_control", persist)
    monkeypatch.setattr(
        "hermes_cli.kanban_db._record_managed_task_failure_exact",
        timeout_record,
    )

    result = _finalize_managed_hard_budget(agent)

    timeout_record.assert_not_called()
    control = persist.call_args.args[0]
    assert control["control_id"] == "hc-injected"
    assert control["message"] == "inspect the final migration"
    assert result["interrupted"] is True
    assert result["failed"] is False


def _worker_control(control_id="hc-retry"):
    return {
        "control_id": control_id,
        "source_task_id": "task-123",
        "target_task_id": "task-123",
        "kind": "redirect",
        "message": "retry this direction",
        "expected_run_id": 77,
        "expected_worker_pid": 1234,
    }


def _stage_without_supervisor(control, *, error=""):
    """Unit-test finalizer shape without leaving an infinite retry thread."""
    return {
        **dict(control),
        "state": "pending_supervisor_retry",
        "error": str(error),
    }


def _control_conn(*, close_error=None):
    class _Conn:
        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(fetchone=lambda: None)

        def close(self):
            if close_error is not None:
                raise close_error

    return _Conn()


def test_worker_control_connect_failure_retries_same_control_id(monkeypatch):
    from hermes_cli import kanban_db as kb

    connect = MagicMock(
        side_effect=[RuntimeError("connect unavailable"), _control_conn()]
    )
    persist = MagicMock(return_value={"status": "recorded"})
    monkeypatch.setattr(kb, "connect", connect)
    monkeypatch.setattr(kb, "persist_handoff_control", persist)
    control = _worker_control("hc-connect-retry")

    result = handoff.persist_worker_handoff_control(control, attempts=2)

    assert result["status"] == "recorded"
    assert result["attempts"] == 2
    assert connect.call_count == 2
    assert persist.call_args.kwargs["control_id"] == "hc-connect-retry"


def test_worker_control_close_failure_replays_to_already_recorded(monkeypatch):
    from hermes_cli import kanban_db as kb

    first = _control_conn(close_error=RuntimeError("close uncertain"))
    second = _control_conn()
    connect = MagicMock(side_effect=[first, second])
    persist = MagicMock(
        side_effect=[
            {"status": "recorded"},
            {"status": "already_recorded"},
        ]
    )
    monkeypatch.setattr(kb, "connect", connect)
    monkeypatch.setattr(kb, "persist_handoff_control", persist)
    control = _worker_control("hc-close-replay")

    result = handoff.persist_worker_handoff_control(control, attempts=2)

    assert result["status"] == "already_recorded"
    assert result["attempts"] == 2
    assert [
        call.kwargs["control_id"] for call in persist.call_args_list
    ] == ["hc-close-replay", "hc-close-replay"]
    assert [call.kwargs["phase"] for call in persist.call_args_list] == [
        "before_commit",
        "before_commit",
    ]


def test_hard_budget_two_persist_failures_return_structured_control(monkeypatch):
    from hermes_cli import kanban_db as kb

    _arm_managed_hard_budget(monkeypatch)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    connect = MagicMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(kb, "connect", connect)
    monkeypatch.setattr(
        handoff, "stage_pending_handoff_control", _stage_without_supervisor
    )
    agent = _LimitAgent(max_iterations=90)
    agent._pending_steer_receipts = [
        _hard_receipt(
            "hc-stop-recovery",
            1,
            "stop",
            "/stop",
            delivery_slot="interrupt",
        )
    ]

    result = _finalize_managed_hard_budget(agent)

    assert connect.call_count == 2
    assert result["failed"] is True
    assert result["turn_exit_reason"] == "kanban_budget_finalize_failed"
    assert result.get("pending_steer") is None
    assert result["pending_handoff_control"] == {
        "control_id": "hc-stop-recovery",
        "source_task_id": "task-123",
        "target_task_id": "task-123",
        "kind": "stop",
        "message": "/stop",
        "phase": "before_commit",
        "expected_run_id": 77,
        "expected_worker_pid": os.getpid(),
        "state": "pending_supervisor_retry",
        "error": "database unavailable",
    }


@pytest.mark.parametrize(
    "terminal_outcome",
    ["recorded", "vetoed", "superseded", "persist_failed"],
)
def test_managed_hard_budget_all_terminal_outcomes_skip_learning_hooks(
    monkeypatch,
    terminal_outcome,
):
    _arm_managed_hard_budget(monkeypatch)
    plugin_hook = MagicMock(return_value=[])
    context_hook = MagicMock(name="context_turn_complete")
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", plugin_hook)
    monkeypatch.setattr(
        "agent.conversation_loop._notify_context_engine_turn_complete",
        context_hook,
    )
    agent = _LimitAgent(max_iterations=90)
    agent._skill_nudge_interval = 1
    agent._iters_since_skill = 1
    agent.valid_tool_names = ["skill_manage"]

    if terminal_outcome in {"vetoed", "persist_failed"}:
        agent._pending_steer_receipts = [
            _hard_receipt(
                "hc-learning-guard",
                1,
                "steer",
                "carry this direction",
                injected=True,
            )
        ]
        monkeypatch.setattr(
            handoff,
            "persist_worker_handoff_control",
            MagicMock(
                return_value=(
                    {"status": "recorded"}
                    if terminal_outcome == "vetoed"
                    else {"status": "failed", "error": "still unavailable"}
                )
            ),
        )
        if terminal_outcome == "persist_failed":
            monkeypatch.setattr(
                handoff,
                "stage_pending_handoff_control",
                _stage_without_supervisor,
            )
    else:
        conn = SimpleNamespace(close=lambda: None)
        monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda: conn)
        monkeypatch.setattr(
            "hermes_cli.kanban_db._record_managed_task_failure_exact",
            MagicMock(return_value={"status": terminal_outcome}),
        )

    result = _finalize_managed_hard_budget(
        agent,
        should_review_memory=True,
    )

    plugin_hook.assert_not_called()
    context_hook.assert_not_called()
    agent._sync_external_memory_for_turn.assert_not_called()
    agent._spawn_background_review.assert_not_called()
    assert agent._suppress_session_end_learning is True
    expected_reason = {
        "recorded": "max_iterations_reached(90/90)",
        "vetoed": "interrupted_by_user_direction",
        "superseded": "kanban_state_superseded",
        "persist_failed": "kanban_budget_finalize_failed",
    }[terminal_outcome]
    assert result["turn_exit_reason"] == expected_reason
