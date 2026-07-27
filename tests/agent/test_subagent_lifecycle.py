"""Contract tests for the public plugin subagent lifecycle API."""

import threading
import time
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

    def interrupt(self, _reason):
        self.interrupted = True


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


def test_launch_wait_result_and_handle_round_trip(lifecycle):
    handle = lifecycle.launch(
        SubagentLaunchRequest(goal="x", allowed_toolsets=("file",))
    )
    assert handle.from_dict(handle.to_dict()) == handle
    assert lifecycle.wait(handle, timeout_seconds=1).state is SubagentState.SUCCEEDED
    first = lifecycle.result(handle)
    assert first.ready and first.summary == "safe summary" and first.result_hash
    assert lifecycle.result(handle) == first


def test_duplicate_correlation_and_permission_validation(lifecycle):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="x", correlation_id="same"))
    with pytest.raises(SubagentLifecycleError, match="Duplicate"):
        lifecycle.launch(SubagentLaunchRequest(goal="x", correlation_id="same"))
    with pytest.raises(SubagentLifecycleError, match="broaden"):
        lifecycle.launch(
            SubagentLaunchRequest(goal="x", allowed_toolsets=("terminal",))
        )
    with pytest.raises(SubagentLifecycleError, match="working_directory"):
        lifecycle.launch(SubagentLaunchRequest(goal="x", working_directory="C:/"))
    lifecycle.wait(handle, timeout_seconds=1)


def test_accepts_plugin_registered_toolset(lifecycle, monkeypatch):
    from tools.registry import registry

    monkeypatch.setattr(
        registry,
        "get_registered_toolset_names",
        lambda: {"plugin-supervisor-control"},
    )
    parent = SimpleNamespace(
        session_id="plugin-parent",
        enabled_toolsets=["plugin-supervisor-control"],
    )
    service = SubagentLifecycleService(lambda: parent)
    handle = service.launch(
        SubagentLaunchRequest(
            goal="x",
            allowed_toolsets=("plugin-supervisor-control",),
            correlation_id="plugin-toolset",
        )
    )
    assert service.wait(handle, timeout_seconds=1).state is SubagentState.SUCCEEDED


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


def test_simultaneous_launches_are_distinct_and_reconnect_is_in_process(lifecycle):
    handles = [lifecycle.launch(SubagentLaunchRequest(goal="x")) for _ in range(10)]
    assert len({h.subagent_id for h in handles}) == 10
    assert lifecycle.reconnect(handles[0]).connected
    for handle in handles:
        lifecycle.wait(handle, timeout_seconds=1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capability", []),
        ("contract_version", True),
        ("subagent_id", None),
        ("parent_session_id", []),
        ("correlation_id", []),
        ("created_at", "yesterday"),
        ("provider", []),
        ("model", []),
        ("role", []),
        ("depth", "one"),
    ],
)
def test_malformed_deserialized_handle_is_unknown(lifecycle, field, value):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="x"))
    malformed = handle.from_dict({**handle.to_dict(), field: value})

    assert lifecycle.status(malformed).state is SubagentState.UNKNOWN
    assert lifecycle.result(malformed).error_classification == "UNKNOWN_HANDLE"
    lifecycle.wait(handle, timeout_seconds=1)


def test_launch_preserves_parent_tool_resolution(monkeypatch):
    import model_tools

    parent = SimpleNamespace(session_id="parent-tools", enabled_toolsets=["file"])
    model_tools._last_resolved_tool_names = ["parent_tool"]

    def build(**_kwargs):
        model_tools._last_resolved_tool_names = ["child_tool"]
        return FakeChild("sa-tools")

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", build)
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "summary": "done",
            "api_calls": 0,
            "duration_seconds": 0,
        },
    )

    service = SubagentLifecycleService(lambda: parent)
    handle = service.launch(SubagentLaunchRequest(goal="x"))

    assert model_tools._last_resolved_tool_names == ["parent_tool"]
    assert handle.subagent_id == "sa-tools"
    service.wait(handle, timeout_seconds=1)


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

    monkeypatch.setattr(
        "tools.delegate_tool._build_child_agent", lambda **_kwargs: child
    )
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


def test_plugin_context_uses_turn_scoped_parent(monkeypatch):
    from hermes_cli.plugins import PluginContext, PluginManifest

    parent = SimpleNamespace(session_id="gateway-parent", enabled_toolsets=["file"])
    monkeypatch.setattr(
        "tools.delegate_tool._build_child_agent",
        lambda **_kwargs: FakeChild("sa-gateway"),
    )
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "summary": "done",
            "api_calls": 0,
            "duration_seconds": 0,
        },
    )
    manager = SimpleNamespace(_cli_ref=None)
    ctx = PluginContext(PluginManifest(name="test", source="test"), manager)

    with bind_subagent_parent(parent):
        handle = ctx.subagent_lifecycle.launch(SubagentLaunchRequest(goal="x"))
        ctx.subagent_lifecycle.wait(handle, timeout_seconds=1)

    assert handle.parent_session_id == "gateway-parent"


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


def test_private_context_is_hidden_available_only_in_child_and_cleared(monkeypatch):
    parent = SimpleNamespace(session_id="parent-private", enabled_toolsets=["file"])
    child = FakeChild("sa-private")
    secret = {"opaque": "do-not-serialize"}
    observed = []

    monkeypatch.setattr(
        "tools.delegate_tool._build_child_agent", lambda **_kwargs: child
    )

    service = SubagentLifecycleService(lambda: parent)

    def run(_index, _goal, running_child, _parent):
        with bind_subagent_parent(running_child):
            observed.append(service.current_private_context())
        return {
            "status": "completed",
            "summary": "done",
            "api_calls": 0,
            "duration_seconds": 0,
        }

    monkeypatch.setattr("tools.delegate_tool._run_single_child", run)
    request = SubagentLaunchRequest(goal="x", private_context=secret)
    assert "do-not-serialize" not in repr(request)

    handle = service.launch(request)
    assert "do-not-serialize" not in repr(handle.to_dict())
    assert service.wait(handle, timeout_seconds=1).state is SubagentState.SUCCEEDED
    assert observed == [secret]
    assert child._plugin_private_context is None
    assert service.current_private_context() is None


def test_publish_progress_is_bounded_rate_limited_and_priority_can_bypass(monkeypatch):
    parent = SimpleNamespace(session_id="parent-progress", enabled_toolsets=["file"])
    child = FakeChild("sa-progress")
    relayed = []
    receipts = []
    child.tool_progress_callback = lambda *args, **kwargs: relayed.append((
        args,
        kwargs,
    ))
    monkeypatch.setattr(
        "tools.delegate_tool._build_child_agent", lambda **_kwargs: child
    )
    ticks = iter((10.0, 11.0, 12.0))
    monkeypatch.setattr("agent.subagent_lifecycle.time.monotonic", lambda: next(ticks))

    service = SubagentLifecycleService(lambda: parent)

    def run(_index, _goal, running_child, _parent):
        with bind_subagent_parent(running_child):
            receipts.append(service.publish_progress("first milestone"))
            receipts.append(service.publish_progress("too soon"))
            receipts.append(service.publish_progress("owner gate", priority=True))
        return {
            "status": "completed",
            "summary": "done",
            "api_calls": 0,
            "duration_seconds": 0,
        }

    monkeypatch.setattr("tools.delegate_tool._run_single_child", run)
    handle = service.launch(SubagentLaunchRequest(goal="x"))
    assert service.wait(handle, timeout_seconds=1).state is SubagentState.SUCCEEDED
    assert receipts == [True, False, True]
    assert [call[0] for call in relayed] == [
        ("subagent.progress", "first milestone"),
        ("subagent.progress", "owner gate"),
    ]


def test_durable_notification_is_origin_routed_idempotent_and_restorable(
    tmp_path, monkeypatch
):
    from tools import async_delegation as ad
    from tools.process_registry import process_registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True
    )
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    receipts = []
    publish_finished = threading.Event()
    publish_errors = []
    original_publish = ad.publish_external_notification

    def observable_publish(**kwargs):
        try:
            return original_publish(**kwargs)
        except Exception as exc:
            publish_errors.append(repr(exc))
            raise

    monkeypatch.setattr(ad, "publish_external_notification", observable_publish)
    current_parent = {
        "value": SimpleNamespace(
            session_id="origin-milestone", enabled_toolsets=["file"]
        )
    }
    service = SubagentLifecycleService(lambda: current_parent["value"])
    monkeypatch.setattr(
        "tools.delegate_tool._build_child_agent",
        lambda **_kwargs: FakeChild("sa-durable-milestone"),
    )

    def run(_index, _goal, running_child, _parent):
        with bind_subagent_parent(running_child):
            current_parent["value"] = SimpleNamespace(
                session_id="unrelated-current-turn", enabled_toolsets=["file"]
            )
            receipts.append(
                service.publish_notification(
                    "safe durable milestone",
                    dedupe_key="native-job:cursor-7",
                    priority=True,
                )
            )
            receipts.append(
                service.publish_notification(
                    "safe durable milestone",
                    dedupe_key="native-job:cursor-7",
                    priority=True,
                )
            )
            current_parent["value"] = SimpleNamespace(
                session_id="origin-milestone", enabled_toolsets=["file"]
            )
            publish_finished.set()
        return {
            "status": "completed",
            "summary": "done",
            "api_calls": 0,
            "duration_seconds": 0,
        }

    monkeypatch.setattr("tools.delegate_tool._run_single_child", run)
    try:
        handle = service.launch(
            SubagentLaunchRequest(goal="private child prompt sentinel")
        )
        assert publish_finished.wait(1)
        assert service.wait(handle, timeout_seconds=1).state is SubagentState.SUCCEEDED
        assert receipts == [True, True], publish_errors
        assert ad.active_count() == 0
        event = process_registry.completion_queue.get_nowait()
        assert event["summary"] == "safe durable milestone"
        assert event["parent_session_id"] == "origin-milestone"
        assert event["session_key"] == "origin-milestone"
        assert process_registry.completion_queue.empty()
        durable = ad.get_durable_delegation(event["delegation_id"])
        assert durable["state"] == "completed"
        assert durable["delivery_state"] == "pending"
        assert "private child prompt sentinel" not in str(durable)
        restored = __import__("queue").Queue()
        assert ad.restore_undelivered_completions(restored) == 1
        assert restored.get_nowait()["delegation_id"] == event["delegation_id"]
        assert restored.empty()
    finally:
        ad._reset_for_tests()


def test_terminal_callback_can_relaunch_on_same_parent_without_active_turn(monkeypatch):
    parent = SimpleNamespace(session_id="parent-relaunch", enabled_toolsets=["file"])
    counter = iter(range(2))
    callback_finished = threading.Event()
    relaunched = []

    monkeypatch.setattr(
        "tools.delegate_tool._build_child_agent",
        lambda **_kwargs: FakeChild(f"sa-relaunch-{next(counter)}"),
    )
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "summary": "generation done",
            "api_calls": 0,
            "duration_seconds": 0,
        },
    )

    service = SubagentLifecycleService(lambda: parent)

    def on_terminal(handle, snapshot, result):
        assert snapshot.state is SubagentState.SUCCEEDED
        assert result.ready
        replacement = service.relaunch(
            handle,
            SubagentLaunchRequest(
                goal="replacement",
                correlation_id="generation-2",
                private_context={"generation": 2},
            ),
        )
        relaunched.append(replacement)
        callback_finished.set()

    first = service.launch(
        SubagentLaunchRequest(
            goal="first",
            correlation_id="generation-1",
            on_terminal=on_terminal,
        )
    )
    assert service.wait(first, timeout_seconds=1).state is SubagentState.SUCCEEDED
    assert callback_finished.wait(1)
    assert len(relaunched) == 1
    second = relaunched[0]
    assert second.subagent_id != first.subagent_id
    assert second.parent_session_id == first.parent_session_id
    assert service.wait(second, timeout_seconds=1).state is SubagentState.SUCCEEDED


def test_manual_completion_delivery_is_durable_routed_and_exactly_once(
    tmp_path, monkeypatch
):
    from tools import async_delegation as ad
    from tools.process_registry import process_registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    try:
        current_parent = {
            "value": SimpleNamespace(
                session_id="origin-delivery", enabled_toolsets=["file"]
            )
        }
        service = SubagentLifecycleService(lambda: current_parent["value"])
        delivery = service.register_completion_delivery(
            goal="deliver one native terminal",
            context="bounded public context",
            role="leaf",
            model="test-model",
            toolsets=("file",),
        )
        assert ad.active_count() == 1

        # A different conversation/turn can become active before the producer ends;
        # the terminal must retain the origin captured at registration time.
        current_parent["value"] = SimpleNamespace(
            session_id="unrelated-current-turn", enabled_toolsets=["file"]
        )
        assert (
            service.publish_completion(
                delivery,
                status="completed",
                summary="native terminal result",
                api_calls=2,
                duration_seconds=1.25,
            )
            is True
        )
        assert (
            service.publish_completion(
                delivery,
                status="completed",
                summary="duplicate must not enqueue",
            )
            is False
        )
        assert ad.active_count() == 0

        event = process_registry.completion_queue.get_nowait()
        assert event["summary"] == "native terminal result"
        assert event["parent_session_id"] == "origin-delivery"
        assert event["session_key"] == "origin-delivery"
        assert process_registry.completion_queue.empty()

        durable = ad.get_durable_delegation(delivery.delegation_id)
        assert durable["state"] == "completed"
        assert durable["delivery_state"] == "pending"

        restored = __import__("queue").Queue()
        assert ad.restore_undelivered_completions(restored) == 1
        assert restored.get_nowait()["delegation_id"] == delivery.delegation_id
        assert restored.empty()
    finally:
        ad._reset_for_tests()


def test_manual_completion_delivery_discard_removes_only_running_registration(
    tmp_path, monkeypatch
):
    from tools import async_delegation as ad
    from tools.process_registry import process_registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    try:
        parent = SimpleNamespace(session_id="origin-discard", enabled_toolsets=["file"])
        service = SubagentLifecycleService(lambda: parent)
        handle = service.register_completion_delivery(goal="external work")
        assert ad.active_count() == 1
        assert service.discard_completion_delivery(handle) is True
        assert service.discard_completion_delivery(handle) is False
        assert ad.active_count() == 0
        assert ad.get_durable_delegation(handle.delegation_id) is None
        assert process_registry.completion_queue.empty()
    finally:
        ad._reset_for_tests()


def test_manual_completion_delivery_fails_closed_without_a_routable_session(
    tmp_path, monkeypatch
):
    from gateway import session_context
    from tools import async_delegation as ad

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setattr(session_context, "async_delivery_supported", lambda: False)
    monkeypatch.setattr(ad, "_current_origin_session_id", lambda: "")
    ad._reset_for_tests()
    try:
        parent = SimpleNamespace(session_id="finite-worker", enabled_toolsets=["file"])
        service = SubagentLifecycleService(lambda: parent)
        with pytest.raises(
            SubagentLifecycleError,
            match="cannot receive a detached durable completion",
        ):
            service.register_completion_delivery(goal="unroutable external work")
        assert ad.active_count() == 0
    finally:
        ad._reset_for_tests()
