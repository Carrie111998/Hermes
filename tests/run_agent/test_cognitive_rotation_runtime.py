"""Runtime regressions for the opt-in cognitive-rotation guardrail."""

import json
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.tool_guardrails import ToolGuardrailDecision
from run_agent import AIAgent


def _tool_defs(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _tool_call(name: str, call_id: str, arguments: dict | None = None):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments or {}),
        ),
    )


def _make_agent(config: dict) -> AIAgent:
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=_tool_defs(
                "delegate_task",
                "write_file",
                "patch",
                "execute_code",
                "read_file",
            ),
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value=config),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    setattr(agent, "tool_delay", 0)
    setattr(agent, "compression_enabled", False)
    setattr(agent, "save_trajectories", False)
    return agent


def _rotation_config(**overrides) -> dict:
    rotation = {
        "enabled": True,
        "mutation_budget": 20,
        "rotate_after_compaction": True,
        "lock_after_delegation": True,
    }
    rotation.update(overrides)
    return {"agent": {"cognitive_rotation": rotation}}


_RUN_SUBMITTED_CALL = object()
_LEAVE_SUBMITTED_CALL_PENDING = object()


class _ExecutorBoundaryFailure(BaseException):
    """Deterministic non-Exception failure from an executor boundary."""


class _ScriptedExecutor:
    """Executor fake that makes submit/result availability deterministic."""

    def __init__(
        self,
        *submit_actions: object,
        shutdown_error: BaseException | None = None,
    ):
        self._submit_actions = submit_actions
        self._shutdown_error = shutdown_error
        self.submit_count = 0

    def submit(self, function, *args, **kwargs):
        action = self._submit_actions[self.submit_count]
        self.submit_count += 1
        if isinstance(action, BaseException):
            raise action

        future = Future()
        if action is _LEAVE_SUBMITTED_CALL_PENDING:
            return future
        assert action is _RUN_SUBMITTED_CALL
        try:
            result = function(*args, **kwargs)
        except BaseException as error:
            future.set_exception(error)
        else:
            future.set_result(result)
        return future

    def shutdown(self, *_args, **_kwargs):
        if self._shutdown_error is not None:
            raise self._shutdown_error


def _install_completion_callbacks(agent) -> tuple[MagicMock, MagicMock]:
    tool_progress_callback = MagicMock()
    tool_complete_callback = MagicMock()
    setattr(agent, "tool_progress_callback", tool_progress_callback)
    setattr(agent, "tool_complete_callback", tool_complete_callback)
    return tool_progress_callback, tool_complete_callback


def _assert_no_completion_output(
    messages: list[dict],
    tool_progress_callback: MagicMock,
    tool_complete_callback: MagicMock,
) -> None:
    assert messages == []
    tool_complete_callback.assert_not_called()
    assert not any(
        call.args and call.args[0] == "tool.completed"
        for call in tool_progress_callback.call_args_list
    )


def test_successful_delegation_blocks_later_direct_mutation_without_halting_turn():
    """Enabled Hermes must rotate rather than mutate after delegated implementation."""
    agent = _make_agent(_rotation_config())
    messages: list[dict] = []
    delegation = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "delegate_task",
                "delegate-1",
                {"goal": "implement the change"},
            )
        ]
    )

    with patch.object(
        agent,
        "_dispatch_delegate_task",
        return_value=json.dumps({"success": True, "delegation_id": "child-1"}),
    ):
        agent._execute_tool_calls_sequential(
            delegation,
            messages,
            "task-1",
        )

    mutation = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-1",
                {"path": "feature.py", "content": "changed = True\n"},
            )
        ]
    )
    with patch(
        "run_agent.handle_function_call",
        return_value=json.dumps({"success": True}),
    ) as execute:
        agent._execute_tool_calls_sequential(
            mutation,
            messages,
            "task-1",
        )

    execute.assert_not_called()
    blocked = json.loads(messages[-1]["content"])
    assert blocked["error_type"] == "cognitive_rotation_required"
    assert blocked["reason"] == "delegation"
    assert agent._tool_guardrail_halt_decision is None


def test_concurrent_mixed_delegation_batch_blocks_direct_mutator_before_execution():
    agent = _make_agent(_rotation_config(mutation_budget=0))
    messages: list[dict] = []
    batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "delegate_task",
                "delegate-mixed",
                {"goal": "implement elsewhere"},
            ),
            _tool_call(
                "write_file",
                "write-mixed",
                {"path": "feature.py", "content": "changed = True\n"},
            ),
        ]
    )
    executed: list[str] = []

    def invoke(name, *_args, **_kwargs):
        executed.append(name)
        return json.dumps({"success": True})

    with patch.object(agent, "_invoke_tool", side_effect=invoke):
        agent._execute_tool_calls_concurrent(batch, messages, "task-1")

    assert executed == ["delegate_task"]
    assert [message["tool_call_id"] for message in messages] == [
        "delegate-mixed",
        "write-mixed",
    ]
    blocked = json.loads(messages[1]["content"])
    assert blocked["error_type"] == "cognitive_rotation_required"
    assert blocked["reason"] == "mixed_delegation_batch"


def test_sequential_mixed_delegation_batch_blocks_direct_mutator():
    agent = _make_agent(_rotation_config(mutation_budget=0))
    messages: list[dict] = []
    batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "delegate_task",
                "delegate-sequential",
                {"goal": "attempt implementation elsewhere"},
            ),
            _tool_call(
                "write_file",
                "write-sequential",
                {"path": "feature.py", "content": "changed = True\n"},
            ),
        ]
    )

    with (
        patch.object(
            agent,
            "_dispatch_delegate_task",
            return_value=json.dumps({"success": False, "error": "child failed"}),
        ),
        patch("run_agent.handle_function_call") as execute_mutation,
    ):
        agent._execute_tool_calls_sequential(batch, messages, "task-1")

    execute_mutation.assert_not_called()
    assert [message["tool_call_id"] for message in messages] == [
        "delegate-sequential",
        "write-sequential",
    ]
    blocked = json.loads(messages[1]["content"])
    assert blocked["error_type"] == "cognitive_rotation_required"
    assert blocked["reason"] == "mixed_delegation_batch"
    assert getattr(agent, "_cognitive_rotation").active_reason == ""
    assert agent._tool_guardrail_halt_decision is None


def test_segmented_batch_preserves_original_delegation_signal():
    agent = _make_agent(_rotation_config(mutation_budget=0))
    messages: list[dict] = []
    batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "delegate_task",
                "delegate-segment",
                {"goal": "attempt implementation elsewhere"},
            ),
            _tool_call(
                "write_file",
                "write-segment",
                {"path": "feature.py", "content": "changed = True\n"},
            ),
            _tool_call(
                "read_file",
                "read-segment",
                {"path": "README.md"},
            ),
        ]
    )
    executed: list[str] = []

    def invoke(name, *_args, **_kwargs):
        executed.append(name)
        return json.dumps({"success": True})

    with (
        patch.object(
            agent,
            "_dispatch_delegate_task",
            return_value=json.dumps({"success": False, "error": "child failed"}),
        ),
        patch.object(agent, "_invoke_tool", side_effect=invoke),
    ):
        agent._execute_tool_calls(batch, messages, "task-1")

    assert "write_file" not in executed
    assert executed == ["read_file"]
    assert [message["tool_call_id"] for message in messages] == [
        "delegate-segment",
        "write-segment",
        "read-segment",
    ]
    blocked = json.loads(messages[1]["content"])
    assert blocked["error_type"] == "cognitive_rotation_required"
    assert blocked["reason"] == "mixed_delegation_batch"
    assert getattr(agent, "_cognitive_rotation").active_reason == ""
    assert agent._tool_guardrail_halt_decision is None


def test_rotation_budget_concurrent_admission_blocks_one_mutator_but_runs_read():
    agent = _make_agent(_rotation_config(mutation_budget=1))
    messages: list[dict] = []
    batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-budget",
                {"path": "one.py", "content": "one = 1\n"},
            ),
            _tool_call(
                "patch",
                "patch-budget",
                {
                    "mode": "replace",
                    "path": "two.py",
                    "old_string": "old",
                    "new_string": "new",
                },
            ),
            _tool_call("read_file", "read-budget", {"path": "README.md"}),
        ]
    )
    executed: list[str] = []

    def invoke(name, *_args, **_kwargs):
        executed.append(name)
        if name == "read_file":
            return json.dumps({"success": True, "content": "read"})
        if name == "write_file":
            return json.dumps({
                "success": True,
                "bytes_written": 8,
                "files_modified": ["/tmp/one.py"],
            })
        return json.dumps({
            "success": True,
            "diff": "--- a/two.py\n+++ b/two.py\n",
            "files_modified": ["/tmp/two.py"],
        })

    with patch.object(agent, "_invoke_tool", side_effect=invoke):
        agent._execute_tool_calls_concurrent(batch, messages, "task-1")

    executed_mutators = [name for name in executed if name in {"write_file", "patch"}]
    assert len(executed_mutators) == 1
    assert "read_file" in executed
    assert [message["tool_call_id"] for message in messages] == [
        "write-budget",
        "patch-budget",
        "read-budget",
    ]
    blocked_id = (
        "patch-budget" if executed_mutators == ["write_file"] else "write-budget"
    )
    blocked_message = next(
        message for message in messages if message["tool_call_id"] == blocked_id
    )
    blocked = json.loads(blocked_message["content"])
    assert blocked["error_type"] == "cognitive_rotation_required"
    assert blocked["reason"] == "mutation_budget"
    assert (
        sum(
            str(message["content"]).count(
                "[Cognitive rotation activated: mutation_budget]"
            )
            for message in messages
        )
        == 1
    )
    controller = getattr(agent, "_cognitive_rotation")
    assert controller.successful_mutations == 1
    assert controller.pending_mutation_reservations == 0


def test_concurrent_persistence_abort_finalizes_unconsumed_mutation_reservations():
    agent = _make_agent(_rotation_config(mutation_budget=2))
    messages: list[dict] = []
    batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-before-persist-failure",
                {"path": "landed.py", "content": "landed = True\n"},
            ),
            _tool_call(
                "patch",
                "patch-after-persist-failure",
                {
                    "mode": "replace",
                    "path": "failed.py",
                    "old_string": "old",
                    "new_string": "new",
                },
            ),
        ]
    )
    executed: list[str] = []

    def invoke(name, *_args, **_kwargs):
        executed.append(name)
        if name == "write_file":
            return json.dumps({
                "success": True,
                "bytes_written": 14,
                "files_modified": ["/tmp/landed.py"],
            })
        return json.dumps({"success": False, "error": "patch failed"})

    flush_messages_to_session_db = MagicMock(return_value=False)
    tool_complete_callback = MagicMock()
    tool_progress_callback = MagicMock()
    setattr(agent, "tool_complete_callback", tool_complete_callback)
    setattr(agent, "tool_progress_callback", tool_progress_callback)

    with (
        patch.object(
            agent,
            "_flush_messages_to_session_db",
            flush_messages_to_session_db,
        ),
        patch.object(agent, "_invoke_tool", side_effect=invoke),
    ):
        agent._execute_tool_calls_concurrent(batch, messages, "task-1")

    assert sorted(executed) == ["patch", "write_file"]
    assert [message["tool_call_id"] for message in messages] == [
        "write-before-persist-failure"
    ]
    flush_messages_to_session_db.assert_called_once()
    assert getattr(agent, "_incremental_persistence_failed", False) is True
    tool_complete_callback.assert_not_called()
    assert not any(
        call.args and call.args[0] == "tool.completed"
        for call in tool_progress_callback.call_args_list
    )

    controller = getattr(agent, "_cognitive_rotation")
    assert controller.successful_mutations == 1
    assert controller.pending_mutation_reservations == 0
    assert controller.active_reason == ""

    later_admission = controller.before_call("patch")
    assert later_admission.allows_execution
    assert later_admission.reservation_id is not None
    controller.cancel_mutation_reservation(later_admission.reservation_id)
    assert controller.pending_mutation_reservations == 0


def test_concurrent_persistence_abort_counts_unconsumed_successful_mutation():
    agent = _make_agent(_rotation_config(mutation_budget=2))
    messages: list[dict] = []
    batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-before-successful-remainder",
                {"path": "first.py", "content": "first = True\n"},
            ),
            _tool_call(
                "patch",
                "patch-successful-remainder",
                {
                    "mode": "replace",
                    "path": "second.py",
                    "old_string": "old",
                    "new_string": "new",
                },
            ),
        ]
    )

    def invoke(name, *_args, **_kwargs):
        if name == "write_file":
            return json.dumps({
                "success": True,
                "bytes_written": 13,
                "files_modified": ["/tmp/first.py"],
            })
        return json.dumps({
            "success": True,
            "diff": "--- a/second.py\n+++ b/second.py\n",
            "files_modified": ["/tmp/second.py"],
        })

    flush_messages_to_session_db = MagicMock(return_value=False)

    with (
        patch.object(
            agent,
            "_flush_messages_to_session_db",
            flush_messages_to_session_db,
        ),
        patch.object(agent, "_invoke_tool", side_effect=invoke) as execute,
    ):
        agent._execute_tool_calls_concurrent(batch, messages, "task-1")

    assert execute.call_count == 2
    assert [message["tool_call_id"] for message in messages] == [
        "write-before-successful-remainder"
    ]
    controller = getattr(agent, "_cognitive_rotation")
    assert controller.successful_mutations == 2
    assert controller.pending_mutation_reservations == 0
    assert controller.active_reason == "mutation_budget"


def test_concurrent_submit_failure_settles_completed_and_never_submitted_reservations():
    agent = _make_agent(_rotation_config(mutation_budget=2))
    messages: list[dict] = []
    batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-completed-before-submit-failure",
                {"path": "completed.py", "content": "completed = True\n"},
            ),
            _tool_call(
                "patch",
                "patch-never-submitted",
                {
                    "mode": "replace",
                    "path": "never_submitted.py",
                    "old_string": "old",
                    "new_string": "new",
                },
            ),
        ]
    )
    submission_error = RuntimeError("can't start new thread")
    executor = _ScriptedExecutor(_RUN_SUBMITTED_CALL, submission_error)
    tool_progress_callback, tool_complete_callback = _install_completion_callbacks(
        agent
    )
    landed_result = json.dumps({
        "success": True,
        "bytes_written": 17,
        "files_modified": ["/tmp/completed.py"],
    })

    with (
        patch(
            "tools.daemon_pool.DaemonThreadPoolExecutor",
            return_value=executor,
        ),
        patch.object(agent, "_invoke_tool", return_value=landed_result) as execute,
        pytest.raises(RuntimeError) as exc_info,
    ):
        agent._execute_tool_calls_concurrent(batch, messages, "task-1")

    assert exc_info.value is submission_error
    assert executor.submit_count == 2
    execute.assert_called_once()
    _assert_no_completion_output(
        messages,
        tool_progress_callback,
        tool_complete_callback,
    )
    controller = getattr(agent, "_cognitive_rotation")
    assert controller.successful_mutations == 1
    assert controller.pending_mutation_reservations == 0
    assert controller.active_reason == ""

    later_admission = controller.before_call("patch")
    assert later_admission.allows_execution
    assert later_admission.reservation_id is not None
    controller.cancel_mutation_reservation(later_admission.reservation_id)
    assert controller.pending_mutation_reservations == 0


def test_concurrent_submit_failure_conservatively_counts_submitted_unknown_mutation():
    agent = _make_agent(_rotation_config(mutation_budget=1))
    messages: list[dict] = []
    batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-submitted-unknown",
                {"path": "unknown.py", "content": "unknown = True\n"},
            ),
            _tool_call(
                "read_file",
                "read-submit-failure",
                {"path": "README.md"},
            ),
        ]
    )
    submission_error = RuntimeError("can't start new thread")
    executor = _ScriptedExecutor(
        _LEAVE_SUBMITTED_CALL_PENDING,
        submission_error,
    )
    tool_progress_callback, tool_complete_callback = _install_completion_callbacks(
        agent
    )

    with (
        patch(
            "tools.daemon_pool.DaemonThreadPoolExecutor",
            return_value=executor,
        ),
        patch.object(agent, "_invoke_tool") as execute,
        pytest.raises(RuntimeError) as exc_info,
    ):
        agent._execute_tool_calls_concurrent(batch, messages, "task-1")

    assert exc_info.value is submission_error
    assert executor.submit_count == 2
    execute.assert_not_called()
    _assert_no_completion_output(
        messages,
        tool_progress_callback,
        tool_complete_callback,
    )
    controller = getattr(agent, "_cognitive_rotation")
    assert controller.successful_mutations == 1
    assert controller.pending_mutation_reservations == 0
    assert controller.active_reason == "mutation_budget"

    later_admission = controller.before_call("patch")
    assert not later_admission.allows_execution
    assert later_admission.reason == "mutation_budget"
    assert later_admission.reservation_id is None


def test_concurrent_first_submit_failure_releases_never_submitted_reservations():
    agent = _make_agent(_rotation_config(mutation_budget=2))
    messages: list[dict] = []
    batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-never-submitted",
                {"path": "first.py", "content": "first = True\n"},
            ),
            _tool_call(
                "patch",
                "patch-never-submitted",
                {
                    "mode": "replace",
                    "path": "second.py",
                    "old_string": "old",
                    "new_string": "new",
                },
            ),
        ]
    )
    submission_error = RuntimeError("can't start new thread")
    executor = _ScriptedExecutor(submission_error)
    tool_progress_callback, tool_complete_callback = _install_completion_callbacks(
        agent
    )

    with (
        patch(
            "tools.daemon_pool.DaemonThreadPoolExecutor",
            return_value=executor,
        ),
        patch.object(agent, "_invoke_tool") as execute,
        pytest.raises(RuntimeError) as exc_info,
    ):
        agent._execute_tool_calls_concurrent(batch, messages, "task-1")

    assert exc_info.value is submission_error
    assert executor.submit_count == 1
    execute.assert_not_called()
    _assert_no_completion_output(
        messages,
        tool_progress_callback,
        tool_complete_callback,
    )
    controller = getattr(agent, "_cognitive_rotation")
    assert controller.successful_mutations == 0
    assert controller.pending_mutation_reservations == 0
    assert controller.active_reason == ""

    later_admission = controller.before_call("write_file")
    assert later_admission.allows_execution
    assert later_admission.reservation_id is not None
    controller.cancel_mutation_reservation(later_admission.reservation_id)
    assert controller.pending_mutation_reservations == 0


def test_concurrent_executor_constructor_failure_releases_reservations():
    agent = _make_agent(_rotation_config(mutation_budget=2))
    messages: list[dict] = []
    batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-before-constructor-failure",
                {"path": "first.py", "content": "first = True\n"},
            ),
            _tool_call(
                "patch",
                "patch-before-constructor-failure",
                {
                    "mode": "replace",
                    "path": "second.py",
                    "old_string": "old",
                    "new_string": "new",
                },
            ),
        ]
    )
    constructor_error = _ExecutorBoundaryFailure("constructor failed")
    tool_progress_callback, tool_complete_callback = _install_completion_callbacks(
        agent
    )

    with (
        patch(
            "tools.daemon_pool.DaemonThreadPoolExecutor",
            side_effect=constructor_error,
        ),
        patch.object(agent, "_invoke_tool") as execute,
        pytest.raises(_ExecutorBoundaryFailure) as exc_info,
    ):
        agent._execute_tool_calls_concurrent(batch, messages, "task-1")

    assert exc_info.value is constructor_error
    execute.assert_not_called()
    _assert_no_completion_output(
        messages,
        tool_progress_callback,
        tool_complete_callback,
    )
    controller = getattr(agent, "_cognitive_rotation")
    assert controller.successful_mutations == 0
    assert controller.pending_mutation_reservations == 0
    assert controller.active_reason == ""


def test_concurrent_wait_base_exception_counts_submitted_unknown_mutation():
    agent = _make_agent(_rotation_config(mutation_budget=1))
    messages: list[dict] = []
    batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-pending-before-wait-failure",
                {"path": "pending.py", "content": "pending = True\n"},
            )
        ]
    )
    wait_error = _ExecutorBoundaryFailure("wait failed")
    executor = _ScriptedExecutor(_LEAVE_SUBMITTED_CALL_PENDING)
    tool_progress_callback, tool_complete_callback = _install_completion_callbacks(
        agent
    )

    with (
        patch(
            "tools.daemon_pool.DaemonThreadPoolExecutor",
            return_value=executor,
        ),
        patch(
            "agent.tool_executor.concurrent.futures.wait",
            side_effect=wait_error,
        ) as wait_for_futures,
        patch.object(agent, "_invoke_tool") as execute,
        pytest.raises(_ExecutorBoundaryFailure) as exc_info,
    ):
        agent._execute_tool_calls_concurrent(batch, messages, "task-1")

    assert exc_info.value is wait_error
    assert executor.submit_count == 1
    wait_for_futures.assert_called_once()
    execute.assert_not_called()
    _assert_no_completion_output(
        messages,
        tool_progress_callback,
        tool_complete_callback,
    )
    controller = getattr(agent, "_cognitive_rotation")
    assert controller.successful_mutations == 1
    assert controller.pending_mutation_reservations == 0
    assert controller.active_reason == "mutation_budget"


def test_concurrent_shutdown_base_exception_settles_completed_mutation():
    agent = _make_agent(_rotation_config(mutation_budget=1))
    messages: list[dict] = []
    batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-completed-before-shutdown-failure",
                {"path": "completed.py", "content": "completed = True\n"},
            )
        ]
    )
    shutdown_error = _ExecutorBoundaryFailure("shutdown failed")
    executor = _ScriptedExecutor(
        _RUN_SUBMITTED_CALL,
        shutdown_error=shutdown_error,
    )
    tool_progress_callback, tool_complete_callback = _install_completion_callbacks(
        agent
    )
    landed_result = json.dumps({
        "success": True,
        "bytes_written": 17,
        "files_modified": ["/tmp/completed.py"],
    })

    with (
        patch(
            "tools.daemon_pool.DaemonThreadPoolExecutor",
            return_value=executor,
        ),
        patch.object(agent, "_invoke_tool", return_value=landed_result) as execute,
        pytest.raises(_ExecutorBoundaryFailure) as exc_info,
    ):
        agent._execute_tool_calls_concurrent(batch, messages, "task-1")

    assert exc_info.value is shutdown_error
    assert executor.submit_count == 1
    execute.assert_called_once()
    _assert_no_completion_output(
        messages,
        tool_progress_callback,
        tool_complete_callback,
    )
    controller = getattr(agent, "_cognitive_rotation")
    assert controller.successful_mutations == 1
    assert controller.pending_mutation_reservations == 0
    assert controller.active_reason == "mutation_budget"


def test_rotation_budget_failed_reservation_releases_capacity_for_later_pass():
    agent = _make_agent(_rotation_config(mutation_budget=1))
    messages: list[dict] = []
    failed_batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-failed",
                {"path": "failed.py", "content": "broken"},
            )
        ]
    )

    with patch.object(
        agent,
        "_invoke_tool",
        return_value=json.dumps({"success": False, "error": "write failed"}),
    ):
        agent._execute_tool_calls_concurrent(failed_batch, messages, "task-1")

    controller = getattr(agent, "_cognitive_rotation")
    assert controller.successful_mutations == 0
    assert controller.pending_mutation_reservations == 0

    later_batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "patch",
                "patch-later",
                {
                    "mode": "replace",
                    "path": "later.py",
                    "old_string": "old",
                    "new_string": "new",
                },
            )
        ]
    )
    with patch.object(
        agent,
        "_invoke_tool",
        return_value=json.dumps({
            "success": True,
            "diff": "--- a/later.py\n+++ b/later.py\n",
            "files_modified": ["/tmp/later.py"],
        }),
    ) as execute:
        agent._execute_tool_calls_concurrent(later_batch, messages, "task-1")

    execute.assert_called_once()
    assert controller.successful_mutations == 1
    assert controller.pending_mutation_reservations == 0
    assert "[Cognitive rotation activated: mutation_budget]" in messages[-1]["content"]


def test_rotation_budget_segmented_execution_cannot_exceed_remaining_capacity():
    agent = _make_agent(_rotation_config(mutation_budget=2))
    controller = getattr(agent, "_cognitive_rotation")
    assert controller.observe_tool_result("write_file", failed=False) is None
    messages: list[dict] = []
    batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-segment-budget",
                {"path": "segment.py", "content": "changed = True\n"},
            ),
            _tool_call(
                "read_file",
                "read-segment-budget",
                {"path": "README.md"},
            ),
            _tool_call(
                "execute_code",
                "execute-segment-budget",
                {"code": "print('must not run')"},
            ),
        ]
    )
    executed: list[str] = []

    def invoke(name, *_args, **_kwargs):
        executed.append(name)
        if name == "write_file":
            return json.dumps({
                "success": True,
                "bytes_written": 15,
                "files_modified": ["/tmp/segment.py"],
            })
        return json.dumps({"success": True, "content": "read"})

    with (
        patch.object(agent, "_invoke_tool", side_effect=invoke),
        patch("run_agent.handle_function_call", side_effect=invoke),
    ):
        agent._execute_tool_calls(batch, messages, "task-1")

    assert executed.count("write_file") == 1
    assert executed.count("read_file") == 1
    assert "execute_code" not in executed
    assert [message["tool_call_id"] for message in messages] == [
        "write-segment-budget",
        "read-segment-budget",
        "execute-segment-budget",
    ]
    blocked = json.loads(messages[-1]["content"])
    assert blocked["error_type"] == "cognitive_rotation_required"
    assert blocked["reason"] == "mutation_budget"
    assert controller.successful_mutations == 2
    assert controller.pending_mutation_reservations == 0


def test_rotation_budget_downstream_guardrail_block_releases_reservation():
    agent = _make_agent(_rotation_config(mutation_budget=1))
    messages: list[dict] = []
    blocked_batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-guardrail-blocked",
                {"path": "blocked.py", "content": "blocked = True\n"},
            )
        ]
    )
    ordinary_block = ToolGuardrailDecision(
        action="block",
        code="test_block",
        message="blocked downstream",
        tool_name="write_file",
    )

    with patch.object(
        getattr(agent, "_tool_guardrails"),
        "before_call",
        return_value=ordinary_block,
    ):
        agent._execute_tool_calls_sequential(blocked_batch, messages, "task-1")

    controller = getattr(agent, "_cognitive_rotation")
    assert controller.successful_mutations == 0
    assert controller.pending_mutation_reservations == 0

    later_batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-after-guardrail",
                {"path": "allowed.py", "content": "allowed = True\n"},
            )
        ]
    )
    with patch(
        "run_agent.handle_function_call",
        return_value=json.dumps({
            "success": True,
            "bytes_written": 15,
            "files_modified": ["/tmp/allowed.py"],
        }),
    ) as execute:
        agent._execute_tool_calls_sequential(later_batch, messages, "task-1")

    execute.assert_called_once()
    assert controller.successful_mutations == 1
    assert controller.pending_mutation_reservations == 0


def test_rotation_budget_timeout_releases_reservation_for_later_pass():
    agent = _make_agent(_rotation_config(mutation_budget=1))
    messages: list[dict] = []
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    timed_batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-timeout",
                {"path": "timeout.py", "content": "timeout = True\n"},
            )
        ]
    )

    def wait_forever(*_args, **_kwargs):
        started.set()
        release.wait(timeout=5)
        finished.set()
        return json.dumps({
            "success": True,
            "bytes_written": 15,
            "files_modified": ["/tmp/timeout.py"],
        })

    try:
        with (
            patch.object(agent, "_invoke_tool", side_effect=wait_forever),
            patch(
                "agent.tool_executor._resolve_concurrent_tool_timeout",
                return_value=0.5,
            ),
        ):
            agent._execute_tool_calls_concurrent(timed_batch, messages, "task-1")
    finally:
        release.set()

    assert started.is_set()
    assert finished.wait(timeout=2)
    controller = getattr(agent, "_cognitive_rotation")
    assert controller.successful_mutations == 0
    assert controller.pending_mutation_reservations == 0

    later_batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "patch",
                "patch-after-timeout",
                {
                    "mode": "replace",
                    "path": "after.py",
                    "old_string": "old",
                    "new_string": "new",
                },
            )
        ]
    )
    with patch.object(
        agent,
        "_invoke_tool",
        return_value=json.dumps({
            "success": True,
            "diff": "--- a/after.py\n+++ b/after.py\n",
            "files_modified": ["/tmp/after.py"],
        }),
    ):
        agent._execute_tool_calls_concurrent(later_batch, messages, "task-1")

    assert controller.successful_mutations == 1
    assert controller.pending_mutation_reservations == 0


def test_rotation_budget_keyboard_interrupt_releases_sequential_reservation():
    agent = _make_agent(_rotation_config(mutation_budget=1))
    messages: list[dict] = []
    batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-interrupted",
                {"path": "interrupt.py", "content": "interrupted = True\n"},
            )
        ]
    )

    with (
        patch("run_agent.handle_function_call", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        agent._execute_tool_calls_sequential(batch, messages, "task-1")

    controller = getattr(agent, "_cognitive_rotation")
    assert controller.successful_mutations == 0
    assert controller.pending_mutation_reservations == 0


def test_raw_mutation_result_reaches_sequential_verifier_before_notice_decoration():
    agent = _make_agent(_rotation_config(mutation_budget=1))
    setattr(agent, "_turn_failed_file_mutations", {})
    setattr(agent, "_turn_file_mutation_paths", set())
    messages: list[dict] = []
    batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "write_file",
                "write-verifier-sequential",
                {"path": "sequential.py", "content": "landed = True\n"},
            )
        ]
    )
    raw_result = json.dumps({
        "success": True,
        "bytes_written": 14,
        "files_modified": ["/tmp/project/sequential.py"],
    })

    with patch("run_agent.handle_function_call", return_value=raw_result):
        agent._execute_tool_calls_sequential(batch, messages, "task-1")

    assert getattr(agent, "_turn_file_mutation_paths") == {"/tmp/project/sequential.py"}
    assert (
        messages[0]["content"].count("[Cognitive rotation activated: mutation_budget]")
        == 1
    )


def test_raw_mutation_result_reaches_concurrent_verifier_before_notice_decoration():
    agent = _make_agent(_rotation_config(mutation_budget=1))
    setattr(agent, "_turn_failed_file_mutations", {})
    setattr(agent, "_turn_file_mutation_paths", set())
    messages: list[dict] = []
    batch = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "patch",
                "patch-verifier-concurrent",
                {
                    "mode": "replace",
                    "path": "concurrent.py",
                    "old_string": "old",
                    "new_string": "new",
                },
            )
        ]
    )
    raw_result = json.dumps({
        "success": True,
        "diff": "--- a/concurrent.py\n+++ b/concurrent.py\n",
        "files_modified": ["/tmp/project/concurrent.py"],
    })

    with patch.object(agent, "_invoke_tool", return_value=raw_result):
        agent._execute_tool_calls_concurrent(batch, messages, "task-1")

    assert getattr(agent, "_turn_file_mutation_paths") == {"/tmp/project/concurrent.py"}
    assert (
        messages[0]["content"].count("[Cognitive rotation activated: mutation_budget]")
        == 1
    )


@pytest.mark.parametrize("mode", ["sequential", "concurrent", "segmented"])
def test_effect_disposition_is_none_for_cognitive_blocks_in_every_mode(mode):
    agent = _make_agent(_rotation_config(mutation_budget=0))
    controller = getattr(agent, "_cognitive_rotation")
    assert controller.observe_tool_result("delegate_task", failed=False) is not None
    calls = [
        _tool_call(
            "write_file",
            f"write-blocked-{mode}",
            {"path": f"{mode}.py", "content": "blocked = True\n"},
        ),
        _tool_call(
            "read_file",
            f"read-allowed-{mode}",
            {"path": "README.md"},
        ),
    ]
    if mode == "segmented":
        calls.append(
            _tool_call(
                "execute_code",
                "execute-blocked-segmented",
                {"code": "print('must not run')"},
            )
        )
    batch = SimpleNamespace(tool_calls=calls)
    messages: list[dict] = []
    executed: list[str] = []

    def invoke(name, *_args, **_kwargs):
        executed.append(name)
        return json.dumps({"success": True, "content": "read"})

    with (
        patch.object(agent, "_invoke_tool", side_effect=invoke),
        patch("run_agent.handle_function_call", side_effect=invoke),
    ):
        if mode == "sequential":
            agent._execute_tool_calls_sequential(batch, messages, "task-1")
        elif mode == "concurrent":
            agent._execute_tool_calls_concurrent(batch, messages, "task-1")
        else:
            from agent.tool_executor import execute_tool_calls_segmented

            execute_tool_calls_segmented(
                agent,
                batch,
                messages,
                "task-1",
                segments=[("parallel", calls[:2]), ("sequential", calls[2:])],
            )

    assert executed == ["read_file"]
    assert [message["tool_call_id"] for message in messages] == [
        call.id for call in calls
    ]
    for call, message in zip(calls, messages):
        if call.function.name == "read_file":
            continue
        blocked = json.loads(message["content"])
        assert blocked["error_type"] == "cognitive_rotation_required"
        assert blocked["reason"] == "delegation"
        assert message["effect_disposition"] == "none"


@pytest.mark.parametrize("mode", ["sequential", "concurrent", "segmented"])
def test_effect_disposition_remains_effect_capable_for_executed_mutators(mode):
    agent = _make_agent(_rotation_config(mutation_budget=0))
    calls = [
        _tool_call(
            "write_file",
            f"write-executed-{mode}",
            {"path": f"{mode}.py", "content": "executed = True\n"},
        ),
        _tool_call(
            "read_file",
            f"read-executed-{mode}",
            {"path": "README.md"},
        ),
    ]
    if mode == "segmented":
        calls.append(
            _tool_call(
                "execute_code",
                "execute-executed-segmented",
                {"code": "print('executed')"},
            )
        )
    batch = SimpleNamespace(tool_calls=calls)
    messages: list[dict] = []

    def invoke(name, *_args, **_kwargs):
        if name == "write_file":
            return json.dumps({
                "success": True,
                "bytes_written": 16,
                "files_modified": [f"/tmp/{mode}.py"],
            })
        if name == "execute_code":
            return json.dumps({"success": True, "output": "executed"})
        return json.dumps({"success": True, "content": "read"})

    with (
        patch.object(agent, "_invoke_tool", side_effect=invoke),
        patch("run_agent.handle_function_call", side_effect=invoke),
    ):
        if mode == "sequential":
            agent._execute_tool_calls_sequential(batch, messages, "task-1")
        elif mode == "concurrent":
            agent._execute_tool_calls_concurrent(batch, messages, "task-1")
        else:
            from agent.tool_executor import execute_tool_calls_segmented

            execute_tool_calls_segmented(
                agent,
                batch,
                messages,
                "task-1",
                segments=[("parallel", calls[:2]), ("sequential", calls[2:])],
            )

    assert [message["tool_call_id"] for message in messages] == [
        call.id for call in calls
    ]
    for call, message in zip(calls, messages):
        if call.function.name in {"write_file", "execute_code"}:
            assert message.get("effect_disposition") is None
