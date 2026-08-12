"""Focused seam tests for streaming spinner cleanup."""

import ast
import inspect

import pytest

from agent.streaming_control import _stop_spinner


class _Spinner:
    def __init__(self, events):
        self.events = events

    def stop(self, value):
        self.events.append(("spinner", value))


def test_stop_spinner_stops_and_clears_then_calls_callback_by_identity():
    events = []
    callback = lambda value: events.append(("callback", value))
    spinner = _Spinner(events)

    assert _stop_spinner(spinner, callback) is None
    assert events == [("spinner", ""), ("callback", "")]


def test_stop_spinner_calls_callback_without_spinner():
    events = []
    callback = lambda value: events.append(("callback", value))

    assert _stop_spinner(None, callback) is None
    assert events == [("callback", "")]


def test_stop_spinner_does_nothing_when_both_inputs_absent():
    assert _stop_spinner(None, None) is None


def test_stop_spinner_preserves_callback_identity_and_order():
    events = []
    callback = lambda value: events.append(("callback", value))
    spinner = _Spinner(events)

    returned = _stop_spinner(spinner, callback)
    assert returned is None
    assert callback is not None
    assert [kind for kind, _ in events] == ["spinner", "callback"]


def test_stop_spinner_propagates_spinner_exception_and_skips_callback():
    events = []

    class FailingSpinner:
        def stop(self, value):
            events.append(("spinner", value))
            raise RuntimeError("stop failed")

    callback = lambda value: events.append(("callback", value))
    with pytest.raises(RuntimeError, match="stop failed"):
        _stop_spinner(FailingSpinner(), callback)
    assert events == [("spinner", "")]


def test_run_conversation_call_site_keeps_first_delta_adapter_and_performs_update():
    from agent.conversation_loop import run_conversation

    tree = ast.parse(inspect.getsource(run_conversation))
    nested = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_on_first_delta"
    ]
    assert len(nested) == 1
    adapter = nested[0]
    assert any(
        isinstance(node, ast.Nonlocal) and node.names == ["thinking_spinner"]
        for node in ast.walk(adapter)
    )
    assignments = [
        node
        for node in ast.walk(adapter)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "thinking_spinner"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    call = assignments[0].value
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Name)
    assert call.func.id == "_stop_spinner"
    assert [ast.unparse(arg) for arg in call.args] == [
        "thinking_spinner",
        "agent.thinking_callback",
    ]

    streaming_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_interruptible_streaming_api_call"
    ]
    assert len(streaming_calls) == 1
    callback_keywords = {
        kw.arg: ast.unparse(kw.value)
        for kw in streaming_calls[0].keywords
        if kw.arg == "on_first_delta"
    }
    assert callback_keywords == {"on_first_delta": "_on_first_delta"}
