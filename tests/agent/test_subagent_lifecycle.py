"""Contract tests for the public plugin subagent lifecycle API."""

import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.subagent_lifecycle import (
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
        self.closed = False

    def interrupt(self, _reason):
        self.interrupted = True
        self.interrupt_kind = "soft"

    def hard_interrupt(self, reason, *, tool_reason=None):
        self.interrupted = True
        self.interrupt_kind = "hard"
        self.interrupt_message = reason
        self.tool_reason = tool_reason

    def close(self):
        self.closed = True


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


def test_retry_recovers_visible_child_while_first_launch_response_waits(
    lifecycle, monkeypatch
):
    """A lost launch acknowledgement must not strand or duplicate the child.

    ``Executor.submit`` may have accepted and started the worker before its
    caller receives the returned Future.  Model that exact boundary by
    starting the child and then withholding the submit response.  The first
    caller consequently times out waiting for its response even though the
    child is live and globally listable; a correlation-id retry must recover
    that child's handle immediately.
    """
    from agent import subagent_lifecycle as lifecycle_module
    from tools import delegate_tool

    child_started = threading.Event()
    child_release = threading.Event()
    submit_response_release = threading.Event()
    build_calls = []

    def build_child(**_kwargs):
        child = FakeChild(f"sa-{len(build_calls)}")
        build_calls.append(child)
        return child

    def run_visible_child(_index, goal, child, parent):
        subagent_id = child._subagent_id
        delegate_tool._register_subagent(
            {
                "subagent_id": subagent_id,
                "parent_id": None,
                "depth": 0,
                "goal": goal,
                "model": child.model,
                "started_at": time.time(),
                "status": "running",
                "tool_count": 0,
                "agent": child,
                "owner_agent_session_id": parent.session_id,
            }
        )
        child_started.set()
        try:
            assert child_release.wait(timeout=5), "test did not release child"
            return {
                "status": "completed",
                "summary": "safe summary",
                "api_calls": 1,
                "duration_seconds": 0.01,
            }
        finally:
            delegate_tool._unregister_subagent(subagent_id, agent=child)

    class SubmitResponseGate:
        def submit(self, callback, *args):
            future = Future()

            def run():
                try:
                    future.set_result(callback(*args))
                except BaseException as exc:  # mirror Future worker capture
                    future.set_exception(exc)

            threading.Thread(target=run, daemon=True).start()
            assert submit_response_release.wait(timeout=5), (
                "test did not release the simulated submit response"
            )
            return future

    monkeypatch.setattr(
        delegate_tool, "_build_child_preserving_parent_tools", build_child
    )
    monkeypatch.setattr(delegate_tool, "_run_child_lifecycle", run_visible_child)
    monkeypatch.setattr(lifecycle_module, "_EXECUTOR", SubmitResponseGate())

    request = SubagentLaunchRequest(
        goal="recover a lost launch response",
        correlation_id="lost-launch-ack",
    )
    first_outcome = {}
    first_returned = threading.Event()

    def first_launch():
        try:
            first_outcome["handle"] = lifecycle.launch(request)
        except BaseException as exc:
            first_outcome["error"] = exc
        finally:
            first_returned.set()

    first_thread = threading.Thread(target=first_launch, daemon=True)
    first_thread.start()
    try:
        assert child_started.wait(timeout=2), "child was not started"
        assert not first_returned.is_set(), (
            "first launch unexpectedly received its submit response"
        )
        listed_ids = {
            item["subagent_id"] for item in delegate_tool.list_active_subagents()
        }
        assert "sa-0" in listed_ids

        retry_handle = lifecycle.launch(request)

        assert retry_handle.subagent_id == "sa-0"
        assert not first_returned.is_set()
        assert len(build_calls) == 1

        submit_response_release.set()
        assert first_returned.wait(timeout=2)
        assert first_outcome == {"handle": retry_handle}
        assert lifecycle.status(retry_handle).state is SubagentState.RUNNING
        assert "sa-0" in {
            item["subagent_id"] for item in delegate_tool.list_active_subagents()
        }

        child_release.set()
        assert lifecycle.wait(retry_handle, timeout_seconds=2).state is (
            SubagentState.SUCCEEDED
        )
    finally:
        submit_response_release.set()
        child_release.set()
        first_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert len(build_calls) == 1


def test_launch_returns_handle_while_child_continues_in_background(
    lifecycle, monkeypatch
):
    from tools import delegate_tool

    child_started = threading.Event()
    child_release = threading.Event()

    def gated_run(_index, _goal, _child, _parent):
        child_started.set()
        assert child_release.wait(timeout=5), "test did not release child"
        return {
            "status": "completed",
            "summary": "finished later",
            "api_calls": 1,
            "duration_seconds": 0.01,
        }

    monkeypatch.setattr(delegate_tool, "_run_child_lifecycle", gated_run)
    handle = lifecycle.launch(
        SubagentLaunchRequest(goal="continue later", correlation_id="background-id")
    )
    try:
        assert handle.subagent_id == "sa-0"
        assert child_started.wait(timeout=2)
        assert lifecycle.status(handle).state is SubagentState.RUNNING
        assert not lifecycle.result(handle).ready
    finally:
        child_release.set()

    assert lifecycle.wait(handle, timeout_seconds=2).state is SubagentState.SUCCEEDED
    assert lifecycle.result(handle).summary == "finished later"


def test_same_correlation_is_idempotent_but_payload_change_is_rejected(lifecycle):
    request = SubagentLaunchRequest(goal="same work", correlation_id="retry-key")

    first = lifecycle.launch(request)
    retry = lifecycle.launch(request)

    assert retry == first
    with pytest.raises(SubagentLifecycleError, match="different request"):
        lifecycle.launch(
            SubagentLaunchRequest(
                goal="different work",
                correlation_id="retry-key",
            )
        )


def test_overlapping_same_correlation_builds_and_runs_only_one_child(monkeypatch):
    from tools import delegate_tool

    parent = SimpleNamespace(session_id="parent-overlap", enabled_toolsets=["file"])
    retry_resolved_parent = threading.Event()
    first_build_started = threading.Event()
    duplicate_build_started = threading.Event()
    build_calls = []
    run_calls = []

    def resolve_parent():
        if threading.current_thread().name == "lifecycle-retry":
            retry_resolved_parent.set()
        return parent

    def build_child(**_kwargs):
        index = len(build_calls)
        build_calls.append(index)
        if index == 0:
            first_build_started.set()
            assert retry_resolved_parent.wait(timeout=2), "retry never entered launch"
            # A broken check-then-build implementation lets the retry reach a
            # second build here. The idempotent admission lock keeps it parked
            # until this first build publishes its record instead.
            duplicate_build_started.wait(timeout=2)
        else:
            duplicate_build_started.set()
        return FakeChild(f"sa-overlap-{index}")

    def run_child(_index, _goal, _child, _parent):
        run_calls.append(True)
        return {
            "status": "completed",
            "summary": "ran once",
            "api_calls": 1,
            "duration_seconds": 0.01,
        }

    monkeypatch.setattr(
        delegate_tool, "_build_child_preserving_parent_tools", build_child
    )
    monkeypatch.setattr(delegate_tool, "_run_child_lifecycle", run_child)
    service = SubagentLifecycleService(resolve_parent)
    request = SubagentLaunchRequest(
        goal="one logical launch",
        correlation_id="overlapping-retry-key",
    )
    outcomes = []

    def launch():
        try:
            outcomes.append(service.launch(request))
        except BaseException as exc:
            outcomes.append(exc)

    first = threading.Thread(target=launch, daemon=True, name="lifecycle-first")
    retry = threading.Thread(target=launch, daemon=True, name="lifecycle-retry")
    first.start()
    assert first_build_started.wait(timeout=2), "first launch never reached build"
    retry.start()
    first.join(timeout=2)
    retry.join(timeout=2)

    assert not first.is_alive() and not retry.is_alive()
    assert not duplicate_build_started.is_set()
    assert build_calls == [0]
    assert len(outcomes) == 2
    assert all(not isinstance(outcome, BaseException) for outcome in outcomes)
    assert outcomes[0] == outcomes[1]
    assert service.wait(outcomes[0], timeout_seconds=2).state is (
        SubagentState.SUCCEEDED
    )
    assert run_calls == [True]


def test_failed_submit_rolls_back_correlation_and_allows_retry(monkeypatch):
    from agent import subagent_lifecycle as lifecycle_module
    from tools import delegate_tool

    parent = SimpleNamespace(
        session_id="parent-submit-failure",
        enabled_toolsets=["file"],
        _active_children=[],
        _active_children_lock=None,
    )
    service = SubagentLifecycleService(lambda: parent)
    built_children = []

    def build_child(**_kwargs):
        child = FakeChild(f"sa-submit-{len(built_children)}")
        built_children.append(child)
        parent._active_children.append(child)
        return child

    def run_child(_index, _goal, child, _parent):
        try:
            return {
                "status": "completed",
                "summary": "retry ran",
                "api_calls": 1,
                "duration_seconds": 0.01,
            }
        finally:
            parent._active_children.remove(child)
            child.close()

    real_executor = lifecycle_module._EXECUTOR

    class RejectOnceExecutor:
        def __init__(self):
            self.calls = 0

        def submit(self, callback, *args):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("executor rejected launch")
            return real_executor.submit(callback, *args)

    executor = RejectOnceExecutor()
    monkeypatch.setattr(
        delegate_tool, "_build_child_preserving_parent_tools", build_child
    )
    monkeypatch.setattr(delegate_tool, "_run_child_lifecycle", run_child)
    monkeypatch.setattr(lifecycle_module, "_EXECUTOR", executor)
    request = SubagentLaunchRequest(
        goal="retry after definite failure",
        correlation_id="submit-retry-key",
    )

    with pytest.raises(SubagentLifecycleError, match="Failed to schedule"):
        service.launch(request)

    assert built_children[0].closed
    assert built_children[0] not in parent._active_children

    handle = service.launch(request)
    assert handle.subagent_id == "sa-submit-1"
    assert service.wait(handle, timeout_seconds=2).state is SubagentState.SUCCEEDED
    assert len(built_children) == 2
    assert executor.calls == 2








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
