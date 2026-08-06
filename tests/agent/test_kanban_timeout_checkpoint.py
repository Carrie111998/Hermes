from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent import conversation_loop
from agent.turn_finalizer import finalize_turn
from tests.agent.test_turn_finalizer_iteration_limit_exit import _LimitAgent


def test_kanban_soft_limit_reserves_summary_capacity(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t-soft")

    assert conversation_loop._kanban_soft_iteration_limit(100) == 90
    assert conversation_loop._kanban_soft_iteration_limit(10) == 9
    assert conversation_loop._kanban_soft_iteration_limit(1) == 1

    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert conversation_loop._kanban_soft_iteration_limit(100) == 100


def test_delegated_child_does_not_reserve_parent_kanban_budget(monkeypatch):
    from agent.delegation_context import delegated_child_context

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t-parent")
    with delegated_child_context("child-session"):
        assert conversation_loop._kanban_soft_iteration_limit(100) == 100


def test_pending_interrupt_wins_over_soft_kanban_checkpoint(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t-parent")
    agent = SimpleNamespace(
        max_iterations=100,
        iteration_budget=SimpleNamespace(remaining=10),
        _budget_grace_call=False,
        _interrupt_requested=True,
    )

    assert conversation_loop._kanban_soft_checkpoint_due(agent, 90) is False


def test_soft_checkpoint_persists_session_before_releasing_claim(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t-soft")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "worker-a")

    agent = _LimitAgent(max_iterations=100, budget_remaining=10)
    agent.session_id = "sess-soft"
    agent.model = "model-a"
    agent.provider = "provider-a"
    agent.reasoning_config = {"effort": "high"}
    order: list[str] = []
    live_messages = [{"role": "user", "content": "task"}]
    summary_input = None

    def handle_max_iterations(messages, _api_call_count):
        nonlocal summary_input
        summary_input = messages
        messages.append({"role": "user", "content": "ephemeral summary request"})
        return "durable handoff summary"

    def persist(messages, _history):
        order.append("persist")
        assert all(m.get("content") != "ephemeral summary request" for m in messages)

    recorded = {}

    def record_timeout(conn, task_id, **kwargs):
        order.append("release")
        recorded.update(task_id=task_id, **kwargs)
        return False

    agent._handle_max_iterations = handle_max_iterations
    agent._persist_session = persist
    monkeypatch.setattr(
        "hermes_cli.kanban_db.capture_worker_checkpoint",
        lambda **kwargs: {**kwargs, "branch": "fix/continuation", "head": "a" * 40},
    )
    monkeypatch.setattr("hermes_cli.kanban_db.record_iteration_timeout", record_timeout)
    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda: SimpleNamespace(close=lambda: None))

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=90,
        interrupted=False,
        failed=False,
        messages=live_messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="kanban_soft_budget_checkpoint",
    )

    assert result["final_response"] == "durable handoff summary"
    assert result["completed"] is False
    assert summary_input is not live_messages
    assert order == ["persist", "release"]
    assert recorded["task_id"] == "t-soft"
    assert recorded["expected_run_id"] == 42
    assert recorded["summary"] == "durable handoff summary"
    assert recorded["budget_used"] == 90
    assert recorded["budget_max"] == 100
    assert recorded["soft_checkpoint"] is True
    assert recorded["checkpoint"]["worker_session_id"] == "sess-soft"


def test_checkpoint_does_not_release_claim_when_session_persistence_fails(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t-soft")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "worker-a")
    agent = _LimitAgent(max_iterations=100, budget_remaining=10)
    agent._persist_session = MagicMock(side_effect=OSError("disk full"))
    release = MagicMock()
    monkeypatch.setattr("hermes_cli.kanban_db.record_iteration_timeout", release)

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=90,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="kanban_soft_budget_checkpoint",
    )

    release.assert_not_called()
    assert any("persist_session" in error for error in result["cleanup_errors"])


def test_delegated_child_does_not_release_parent_claim(monkeypatch, tmp_path):
    from agent.delegation_context import delegated_child_context

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t-parent")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    agent = _LimitAgent(max_iterations=100, budget_remaining=0)
    release = MagicMock()
    monkeypatch.setattr("hermes_cli.kanban_db.record_iteration_timeout", release)

    with delegated_child_context("child-session"):
        finalize_turn(
            agent,
            final_response=None,
            api_call_count=100,
            interrupted=False,
            failed=False,
            messages=[{"role": "user", "content": "delegated work"}],
            conversation_history=[],
            effective_task_id="child-task",
            turn_id="turn",
            user_message="delegated work",
            original_user_message="delegated work",
            _should_review_memory=False,
            _turn_exit_reason="budget_exhausted",
        )

    release.assert_not_called()
