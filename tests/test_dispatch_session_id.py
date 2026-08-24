"""Tests that handle_function_call forwards session_id into registry.dispatch."""

import json
from unittest.mock import MagicMock, patch


def _make_registry(captured: dict):
    """Return a mock registry whose dispatch records the kwargs it receives."""
    registry = MagicMock()

    def _dispatch(name, args, **kwargs):
        captured.update(kwargs)
        return json.dumps({"result": "ok"})

    registry.dispatch.side_effect = _dispatch
    return registry


class TestSessionIdForwarding:

    def test_standard_path_forwards_session_id(self):
        """registry.dispatch receives session_id on the normal tool path."""
        captured = {}
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call
            handle_function_call(
                "web_search",
                {"query": "test"},
                task_id="t1",
                session_id="sess-abc",
                skip_pre_tool_call_hook=True,
            )
        assert captured.get("session_id") == "sess-abc"

    def test_execute_code_path_forwards_session_id(self):
        """registry.dispatch receives session_id on the execute_code path."""
        captured = {}
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call
            handle_function_call(
                "execute_code",
                {"code": "print(1)"},
                task_id="t1",
                session_id="sess-xyz",
                skip_pre_tool_call_hook=True,
            )
        assert captured.get("session_id") == "sess-xyz"

    def test_session_id_default_is_none(self):
        """When session_id is omitted, dispatch receives None."""
        captured = {}
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call
            handle_function_call(
                "web_search",
                {"query": "test"},
                task_id="t1",
                skip_pre_tool_call_hook=True,
            )
        assert "session_id" in captured
        assert captured["session_id"] is None

    def test_task_id_still_forwarded(self):
        """Existing task_id forwarding is not broken by this change."""
        captured = {}
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call
            handle_function_call(
                "web_search",
                {"query": "test"},
                task_id="task-999",
                session_id="sess-1",
                skip_pre_tool_call_hook=True,
            )
        assert captured.get("task_id") == "task-999"


def test_model_tools_blocks_when_pre_tool_hook_dispatch_raises():
    """A hook failure is a policy failure, never a route around the hook."""
    registry = MagicMock()
    registry.dispatch.side_effect = AssertionError("underlying tool must not run")

    with (
        patch("model_tools.registry", registry),
        patch(
            "hermes_cli.plugins._dispatch_pre_tool_call_hooks",
            side_effect=RuntimeError("sensitive internal error"),
        ),
    ):
        from model_tools import handle_function_call
        result = handle_function_call("web_search", {"query": "test"}, task_id="t1")

    registry.dispatch.assert_not_called()
    assert json.loads(result) == {"error": "Tool blocked: pre-tool policy check failed"}


def test_agent_runtime_blocks_when_pre_tool_hook_dispatch_raises():
    """The agent-owned tool path shares the same fail-closed boundary."""
    from types import SimpleNamespace
    from agent.agent_runtime_helpers import invoke_tool

    agent = SimpleNamespace(
        session_id="s1", _current_turn_id="turn-1", _current_api_request_id="request-1",
        _todo_store=MagicMock(),
    )
    with (
        patch(
            "hermes_cli.plugins._dispatch_pre_tool_call_hooks",
            side_effect=RuntimeError("sensitive internal error"),
        ),
        patch("tools.todo_tool.todo_tool", side_effect=AssertionError("underlying tool must not run")) as todo,
    ):
        result = invoke_tool(agent, "todo", {"todos": []}, "t1")

    todo.assert_not_called()
    assert json.loads(result) == {"error": "Tool blocked: pre-tool policy check failed"}


def test_delegated_child_tool_is_blocked_before_dispatch_when_hook_errors():
    from types import SimpleNamespace
    from agent.agent_runtime_helpers import invoke_tool

    child_dispatch = MagicMock(side_effect=AssertionError("child must not run"))
    agent = SimpleNamespace(
        session_id="s1", _current_turn_id="turn-1", _current_api_request_id="request-1",
        _dispatch_delegate_task=child_dispatch,
    )
    with patch(
        "hermes_cli.plugins._dispatch_pre_tool_call_hooks",
        side_effect=RuntimeError("sensitive internal error"),
    ):
        result = invoke_tool(agent, "delegate_task", {"goal": "child"}, "t1")

    child_dispatch.assert_not_called()
    assert json.loads(result) == {"error": "Tool blocked: pre-tool policy check failed"}
