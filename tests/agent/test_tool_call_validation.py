"""Focused contracts for model-emitted tool-call protocol validation."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from agent import conversation_loop
from agent.tool_call_validation import (
    normalize_and_validate_tool_arguments,
    validate_tool_call_names,
)


def _call(name, arguments, call_id="call"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class _Agent:
    valid_tool_names = {"known", "renamed"}
    log_prefix = ""

    def __init__(self):
        self.unique_calls = None

    def _uniquify_tool_call_ids(self, calls):
        self.unique_calls = list(calls)

    def _repair_tool_call(self, name):
        return "renamed" if name == "rename-me" else None


def test_name_validation_deduplicates_repairs_and_identifies_mixed_batch():
    agent = _Agent()
    calls = [_call("rename-me", "{}"), _call("unknown", "{}"), _call("known", "{}")]

    result = validate_tool_call_names(agent, calls)

    assert agent.unique_calls == calls
    assert calls[0].function.name == "renamed"
    assert result.invalid_names == ["unknown"]
    assert result.mixed_invalid_batch is True


def test_argument_validation_coerces_common_shapes_and_ignores_mixed_invalid_call():
    calls = [
        _call("known", {"value": 1}),
        _call("known", "   "),
        _call("unknown", "{broken"),
    ]

    result = normalize_and_validate_tool_arguments(
        calls,
        valid_tool_names={"known"},
        mixed_invalid_batch=True,
    )

    assert calls[0].function.arguments == '{"value": 1}'
    assert calls[1].function.arguments == "{}"
    assert result.invalid_arguments == []
    assert result.truncated is False


def test_argument_validation_detects_truncated_json_for_valid_tool():
    result = normalize_and_validate_tool_arguments(
        [_call("known", '{"value":')],
        valid_tool_names={"known"},
        mixed_invalid_batch=False,
    )

    assert len(result.invalid_arguments) == 1
    assert result.invalid_arguments[0][0] == "known"
    assert result.truncated is True


def test_conversation_loop_uses_protocol_validation_module_as_owner():
    source = inspect.getsource(conversation_loop.run_conversation)
    assert "name_validation = validate_tool_call_names(" in source
    assert "argument_validation = normalize_and_validate_tool_arguments(" in source
