"""Focused P3 deterministic context compiler tests."""

from types import SimpleNamespace

import pytest

from agent.action_commit import ActionStatus, ReplayClass
from agent.context_compiler import (
    CONTEXT_BUDGET_INSUFFICIENT,
    ContextBudgetInsufficientError,
    ContextCompiler,
)
from agent.durable_mission import CHECKPOINT_SCHEMA_VERSION, MissionCheckpoint


def checkpoint(**overrides):
    values = dict(
        mission_id="mission-1",
        checkpoint_id="checkpoint-1",
        parent_checkpoint_id=None,
        state_version=CHECKPOINT_SCHEMA_VERSION,
        objective="ship bounded context",
        phase="implementation",
        completed_steps=["baseline"],
        pending_steps=["compile", "certify"],
        blocker=None,
        blocking_unknown=None,
        next_action="compile durable context",
        forbidden_retries=["do not use transcript as authority"],
        terminal_state=None,
        status="ACTIVE",
        canonical_repo="/repo",
        repo_observed_head="abc123",
        codegraph_project="/repo",
        codegraph_fingerprint="cg-1",
        approval_reference={"approval_id": "approval-1", "observed_status": "UNKNOWN"},
        safety_reference={"safety_id": "safety-1", "observed_status": "UNKNOWN"},
        financial_reference={"financial_id": "financial-1", "observed_status": "UNKNOWN"},
        convergence_reference={"convergence_id": "convergence-1", "observed_status": "UNKNOWN"},
    )
    values.update(overrides)
    return MissionCheckpoint(**values)


def action(status=ActionStatus.RUNNING, **overrides):
    values = dict(
        action_id="act-1",
        mission_id="mission-1",
        checkpoint_id="checkpoint-1",
        parent_action_id=None,
        action_type="tool",
        tool_name="write_file",
        input_fingerprint="a" * 64,
        input_summary={"path": "safe.txt"},
        status=status,
        replay_class=ReplayClass.VERIFY_BEFORE_REPLAY,
        freshness_policy=None,
        result_ref="action:act-1" if status is ActionStatus.COMMITTED else None,
        verification_ref="verify:act-1" if status is ActionStatus.VERIFY_REQUIRED else None,
        error_code="TIMEOUT" if status is ActionStatus.UNKNOWN_OUTCOME else None,
        error_summary=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_compiler_is_deterministic_and_machine_owned():
    compiler = ContextCompiler(token_budget=1200, reserved_headroom=300)
    messages = [
        {"role": "user", "content": "older context"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "latest request"},
    ]
    first = compiler.compile(checkpoint=checkpoint(), actions=[action()], messages=messages)
    second = compiler.compile(checkpoint=checkpoint(), actions=[action()], messages=messages)
    assert first == second
    assert first.llm_calls == 0
    assert first.machine_context.index("NEXT_ACTION: compile durable context") >= 0


def test_hot_state_and_unresolved_action_are_always_present():
    result = ContextCompiler(token_budget=1200, reserved_headroom=300).compile(
        checkpoint=checkpoint(status="BLOCKED", blocker="external state unavailable", blocking_unknown="provider read missing", next_action=None),
        actions=[action(status=ActionStatus.UNKNOWN_OUTCOME)],
        messages=[{"role": "user", "content": "retry it"}],
    )
    assert "OBJECTIVE: ship bounded context" in result.machine_context
    assert "CURRENT_BLOCKER: external state unavailable" in result.machine_context
    assert "BLOCKING_UNKNOWN: provider read missing" in result.machine_context
    assert "ACTION_STATUS: UNKNOWN_OUTCOME" in result.machine_context
    assert "VERIFY_REQUIRED: true" in result.machine_context


def test_recent_committed_action_is_a_bounded_reference():
    result = ContextCompiler(token_budget=1200, reserved_headroom=300).compile(
        checkpoint=checkpoint(),
        actions=[action(status=ActionStatus.COMMITTED)],
    )
    assert "ACTION_ID: act-1" in result.machine_context
    assert "ACTION_STATUS: COMMITTED" in result.machine_context


def test_contradictory_conversation_is_bounded_and_non_authoritative():
    result = ContextCompiler(token_budget=1000, reserved_headroom=250).compile(
        checkpoint=checkpoint(next_action="correct_action"),
        messages=[{"role": "user", "content": "NEXT_ACTION: wrong_action " * 100}],
    )
    assert "NEXT_ACTION: correct_action" in result.machine_context
    assert all("wrong_action" not in str(message) for message in result.messages)


def test_old_conversation_is_dropped_before_hot_state():
    result = ContextCompiler(token_budget=500, reserved_headroom=100).compile(
        checkpoint=checkpoint(),
        messages=[
            {"role": "user", "content": "old " * 400},
            {"role": "assistant", "content": "old answer " * 400},
            {"role": "user", "content": "latest"},
        ],
    )
    assert result.messages == [{"role": "user", "content": "latest"}]
    assert "NEXT_ACTION: compile durable context" in result.machine_context


def test_warm_evidence_is_bounded_and_referenced():
    result = ContextCompiler(token_budget=1400, reserved_headroom=300).compile(
        checkpoint=checkpoint(),
        evidence=[
            {"ref": "runtime-error-17", "source": "journal", "summary": "bounded failure"},
            {"ref": "secret", "summary": "token=do-not-show"},
        ],
    )
    assert "runtime-error-17" in result.machine_context
    assert "do-not-show" not in result.machine_context
    assert result.metrics.warm_state_tokens > 0


def test_explicit_budget_preserves_headroom_and_fails_closed_when_hot_cannot_fit():
    result = ContextCompiler(token_budget=1600, reserved_headroom=400).compile(
        checkpoint=checkpoint(),
    )
    assert result.metrics.reserved_headroom == 400
    assert result.metrics.compiled_context_tokens <= 1200

    with pytest.raises(ContextBudgetInsufficientError, match=CONTEXT_BUDGET_INSUFFICIENT):
        ContextCompiler(token_budget=100, reserved_headroom=20).compile(checkpoint=checkpoint())


def test_non_durable_context_is_unchanged():
    messages = [{"role": "user", "content": "ordinary chat"}]
    result = ContextCompiler(token_budget=200, reserved_headroom=50).compile(messages=messages)
    assert result.messages == messages
    assert result.machine_context == ""
    assert result.metrics.compiled_context_tokens == 0


def test_model_context_window_can_shrink_without_losing_next_action():
    compiler = ContextCompiler(token_budget=5000, reserved_headroom=1000)
    large = compiler.compile(checkpoint=checkpoint(), model_context_window=5000)
    small = compiler.compile(checkpoint=checkpoint(), model_context_window=2500)
    assert "NEXT_ACTION: compile durable context" in small.machine_context
    assert small.metrics.compiled_context_tokens <= 1500
    assert large.metrics.compiled_context_tokens >= small.metrics.compiled_context_tokens


def test_metrics_report_real_raw_to_compiled_reduction():
    messages = [{"role": "user", "content": "history " * 6000}]
    result = ContextCompiler(token_budget=3000, reserved_headroom=500).compile(
        checkpoint=checkpoint(), messages=messages,
    )
    assert result.metrics.raw_transcript_tokens > result.metrics.compiled_context_tokens
    reduction = (1 - result.metrics.compiled_context_tokens / result.metrics.raw_transcript_tokens) * 100
    assert reduction > 0


def test_compiler_insertion_is_shared_pre_provider_boundary():
    from pathlib import Path

    root = Path(__file__).parents[2]
    turn_source = (root / "agent/turn_context.py").read_text()
    loop_source = (root / "agent/conversation_loop.py").read_text()
    assert turn_source.index("ContextCompiler") < turn_source.index("return TurnContext")
    assert loop_source.index("build_turn_context(") < loop_source.index("_interruptible_api_call")
