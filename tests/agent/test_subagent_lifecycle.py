"""Contract tests for the public plugin subagent lifecycle API."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import subagent_lifecycle as lifecycle_module
from agent.subagent_lifecycle import (
    SubagentHandle,
    SubagentInterruptionCause,
    SubagentInterruptionEvidence,
    SubagentInterruptionStage,
    SubagentLaunchRequest,
    SubagentLifecycleError,
    SubagentLifecycleService,
    SubagentState,
    bind_subagent_parent,
    get_active_subagent_parent,
)


class FakeChild:
    def __init__(self, ident="sa-test"):
        self._subagent_id = ident
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self.provider = "test"
        self.model = "test-model"
        self.interrupted = False
        self.interrupt_kind = None
        self.interrupt_message = None
        self.tool_reason = None

    def interrupt(self, _reason):
        self.interrupted = True
        self.interrupt_kind = "soft"

    def hard_interrupt(self, reason, *, tool_reason=None):
        self.interrupted = True
        self.interrupt_kind = "hard"
        self.interrupt_message = reason
        self.tool_reason = tool_reason


@pytest.fixture
def lifecycle(monkeypatch):
    parent = SimpleNamespace(session_id="parent-1", enabled_toolsets=["file"])
    counter = iter(range(1000))

    def build(**_kwargs):
        return FakeChild(f"sa-{next(counter)}")

    def run(_index, _goal, child, _parent):
        for _ in range(20):
            if child.interrupted:
                return {
                    "status": "interrupted",
                    "summary": None,
                    "api_calls": 0,
                    "duration_seconds": 0,
                }
            time.sleep(0.002)
        return {
            "status": "completed",
            "summary": "safe summary",
            "api_calls": 1,
            "duration_seconds": 0.01,
        }

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", build)
    monkeypatch.setattr("tools.delegate_tool._run_single_child", run)
    return SubagentLifecycleService(lambda: parent)






def test_cancel_is_cooperative_and_forged_handle_is_unknown(lifecycle):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="x"))
    assert lifecycle.cancel(handle, reason="test").accepted
    terminal = lifecycle.wait(handle, timeout_seconds=1)
    assert terminal.state is SubagentState.CANCELLED
    forged = handle.__class__(**{**handle.to_dict(), "capability": "forged"})
    assert lifecycle.status(forged).state is SubagentState.UNKNOWN
    assert lifecycle.result(forged).error_classification == "UNKNOWN_HANDLE"
    other_parent = SimpleNamespace(session_id="different-parent")
    other_service = SubagentLifecycleService(lambda: other_parent)
    assert other_service.status(handle).state is SubagentState.UNKNOWN


def test_cancel_uses_explicit_hard_interrupt(lifecycle):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="x"))
    record = lifecycle._record(handle)
    assert record is not None and record.agent is not None

    assert lifecycle.cancel(handle, reason="explicit user cancel").accepted

    assert record.agent.interrupt_kind == "hard"
    assert "explicit user cancel" in record.agent.interrupt_message
    assert record.agent.tool_reason == "subagent cancellation requested"
    lifecycle.wait(handle, timeout_seconds=1)








def test_public_lifecycle_runs_host_aggregation(monkeypatch):
    memory = Mock()
    parent = SimpleNamespace(
        session_id="parent-aggregate",
        enabled_toolsets=["file"],
        _memory_manager=memory,
        _current_turn_id="turn-1",
        session_estimated_cost_usd=1.0,
        session_cost_source="none",
        session_cost_status="unknown",
    )
    child = FakeChild("sa-aggregate")
    child.session_id = "child-session"
    hook = Mock()

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", lambda **_kwargs: child)
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda *_args, **_kwargs: {
            "task_index": 0,
            "status": "completed",
            "summary": "aggregated",
            "api_calls": 1,
            "duration_seconds": 0.25,
            "_child_role": "leaf",
            "_child_cost_usd": 2.5,
        },
    )
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", hook)

    service = SubagentLifecycleService(lambda: parent)
    handle = service.launch(SubagentLaunchRequest(goal="aggregate me"))
    assert service.wait(handle, timeout_seconds=1).state is SubagentState.SUCCEEDED

    memory.on_delegation.assert_called_once_with(
        task="aggregate me", result="aggregated", child_session_id="child-session"
    )
    hook.assert_called_once_with(
        "subagent_stop",
        parent_session_id="parent-aggregate",
        parent_turn_id="turn-1",
        child_session_id="child-session",
        child_role="leaf",
        child_summary="aggregated",
        child_status="completed",
        # Redacted tool history rides the shared finalization pipeline
        # (#62011/#72403); empty here because the fabricated result carries
        # no tool_trace.
        tool_call_history=[],
        duration_ms=250,
    )
    assert parent.session_estimated_cost_usd == 3.5
    assert parent.session_cost_source == "subagent"
    assert parent.session_cost_status == "estimated"




def test_agent_turn_binds_and_clears_lifecycle_parent(monkeypatch):
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    observed = []

    def run_conversation(parent, *_args, **_kwargs):
        observed.append(get_active_subagent_parent())
        return {"final_response": "ok"}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", run_conversation)

    assert agent.run_conversation("hello") == {"final_response": "ok"}
    assert observed == [agent]
    assert get_active_subagent_parent() is None


def test_cancellation_exposes_typed_first_and_later_evidence(lifecycle):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="evidence"))

    cancellation = lifecycle.cancel(handle, reason="user stopped this")
    assert cancellation.accepted
    status = lifecycle.status(handle)
    assert status.interruption is not None
    assert status.interruption.cause is SubagentInterruptionCause.USER_CANCEL
    assert status.interruption.stage is SubagentInterruptionStage.REQUESTED
    assert status.interruption.detail == "user stopped this"
    assert status.later_interruptions
    assert all(
        isinstance(evidence, SubagentInterruptionEvidence)
        for evidence in status.later_interruptions
    )

    terminal = lifecycle.wait(handle, timeout_seconds=1)
    assert terminal.state is SubagentState.CANCELLED
    result = lifecycle.result(handle)
    assert result.interruption == status.interruption
    assert result.later_interruptions == terminal.later_interruptions


def test_terminal_finalization_is_generation_aware_and_idempotent(lifecycle):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="finalize"))
    record = lifecycle._record(handle)
    assert record is not None
    lifecycle.wait(handle, timeout_seconds=1)
    original = lifecycle.result(handle)

    late = lifecycle._finalize_record(
        record,
        original,
        generation=handle.generation + 1,
        callback_id="late-generation",
    )
    duplicate = lifecycle._finalize_record(
        record,
        original,
        generation=handle.generation,
        callback_id="run:duplicate",
    )
    assert late == original
    assert duplicate == original
    assert lifecycle.result(handle) == original
    status = lifecycle.status(handle)
    assert status.first_cause is None
    assert status.later_causes
    assert all(
        cause is SubagentInterruptionCause.LATE_CALLBACK
        for cause in status.later_causes
    )


def test_later_interruption_evidence_is_bounded(lifecycle):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="bounded"))
    record = lifecycle._record(handle)
    assert record is not None
    lifecycle.wait(handle, timeout_seconds=1)

    with lifecycle_module._REGISTRY.lock:
        for index in range(11):
            lifecycle._record_interruption_locked(
                record,
                SubagentInterruptionCause.TIMEOUT,
                SubagentInterruptionStage.OBSERVED,
                source="test",
                detail=str(index),
                authoritative=index == 0,
            )

    status = lifecycle.status(handle)
    assert status.first_cause is SubagentInterruptionCause.TIMEOUT
    assert len(status.later_interruptions) == 8
    assert status.dropped_interruptions == 2


def test_conflicting_duplicate_callback_cannot_replace_terminal_result(lifecycle):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="callback"))
    record = lifecycle._record(handle)
    assert record is not None
    lifecycle.wait(handle, timeout_seconds=1)
    original = lifecycle.result(handle)
    conflicting = type(original)(
        **{
            **original.__dict__,
            "summary": "late conflicting callback",
        }
    )

    lifecycle._finalize_record(
        record,
        original,
        generation=handle.generation,
        callback_id="same-callback",
    )
    lifecycle._finalize_record(
        record,
        conflicting,
        generation=handle.generation,
        callback_id="same-callback",
    )
    assert lifecycle.result(handle) == original


def test_handle_serialization_round_trips_generation(lifecycle):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="round-trip"))
    assert handle.generation == 1
    restored = SubagentHandle.from_dict(handle.to_dict())
    assert restored == handle
    assert restored.generation == 1
    lifecycle.wait(handle, timeout_seconds=1)


def test_legacy_handle_without_generation_is_accepted(lifecycle):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="legacy"))
    payload = handle.to_dict()
    payload.pop("generation")
    legacy = SubagentHandle.from_dict(payload)
    assert legacy.generation == 0
    assert lifecycle.status(legacy).state is not SubagentState.UNKNOWN
    lifecycle.wait(handle, timeout_seconds=1)


def test_stale_generation_handle_is_rejected(lifecycle):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="stale-generation"))
    stale = handle.__class__(**{**handle.to_dict(), "generation": handle.generation + 1})
    assert lifecycle.status(stale).state is SubagentState.UNKNOWN
    lifecycle.wait(handle, timeout_seconds=1)
