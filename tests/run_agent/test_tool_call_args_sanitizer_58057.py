"""
Regression tests for issue #58057.
"""

import copy
import json

from run_agent import AIAgent


def _tool_call(call_id="call_1", name="read_file", arguments='{"path":"/tmp/foo"}'):
    function = {"name": name}
    if arguments is not None:
        function["arguments"] = arguments
    return {
        "id": call_id,
        "type": "function",
        "function": function,
    }


def _assistant_message(*tool_calls):
    return {
        "role": "assistant",
        "content": "tooling",
        "tool_calls": list(tool_calls),
    }


def _tool_response(call_id, content="ok"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def test_array_of_strings_triggers_corruption_marker():
    """A JSON array of strings (not objects) triggers the corruption path.

    Per #58057, when a model emits arguments as a JSON array
    (not a dict), strict OpenAI-compatible endpoints reject with
    HTTP 400 InvalidParameter. The sanitizer must treat this as a
    corruption event, not let it pass. Single-element lists of dicts
    are unwrapped (see test_single_element_list_unwrapped_to_object).
    """
    msg = _assistant_message(
        _tool_call(call_id="call_1", arguments='["a", "b", "c"]')
    )
    messages = [msg]

    repaired = AIAgent._sanitize_tool_call_arguments(messages)

    assert repaired == 1
    args = messages[0]["tool_calls"][0]["function"]["arguments"]
    assert args == "{}", f"expected '{{}}', got {args!r}"


def test_array_arguments_with_multiple_objects_triggers_corruption():
    """A multi-element array is treated as corruption (cannot pick which)."""
    msg = _assistant_message(
        _tool_call(
            call_id="call_1",
            arguments='[{"a": 1}, {"b": 2}]',
        )
    )
    messages = [msg]

    repaired = AIAgent._sanitize_tool_call_arguments(messages)

    assert repaired == 1
    args = messages[0]["tool_calls"][0]["function"]["arguments"]
    assert args == "{}", f"expected '{{}}', got {args!r}"


def test_single_element_list_unwrapped_to_object():
    """A single-element list of dicts is unwrapped to its object.

    Per the bug report, models sometimes emit [{"mode": "..."}]
    when they meant {"mode": "..."}. The sanitizer unwraps this.
    """
    msg = _assistant_message(
        _tool_call(
            call_id="call_1",
            arguments='[{"mode": "replace", "path": "config.yaml"}]',
        )
    )
    messages = [msg]

    repaired = AIAgent._sanitize_tool_call_arguments(messages)

    # Unwrap does NOT count as a "repair" (the model meant an object).
    assert repaired == 0
    args = messages[0]["tool_calls"][0]["function"]["arguments"]
    parsed = json.loads(args)
    assert parsed == {"mode": "replace", "path": "config.yaml"}


def test_string_in_array_triggers_corruption():
    """An array of strings is treated as corruption."""
    msg = _assistant_message(
        _tool_call(call_id="call_1", arguments='["a", "b"]')
    )
    messages = [msg]

    repaired = AIAgent._sanitize_tool_call_arguments(messages)

    assert repaired == 1
    args = messages[0]["tool_calls"][0]["function"]["arguments"]
    assert args == "{}", f"expected '{{}}', got {args!r}"


def test_number_arguments_triggers_corruption():
    """A bare number parses as valid JSON but is not an object."""
    msg = _assistant_message(
        _tool_call(call_id="call_1", arguments="42")
    )
    messages = [msg]

    repaired = AIAgent._sanitize_tool_call_arguments(messages)

    assert repaired == 1
    args = messages[0]["tool_calls"][0]["function"]["arguments"]
    assert args == "{}", f"expected '{{}}', got {args!r}"


def test_null_arguments_triggers_corruption():
    """A bare null parses as valid JSON but is not an object."""
    msg = _assistant_message(
        _tool_call(call_id="call_1", arguments="null")
    )
    messages = [msg]

    repaired = AIAgent._sanitize_tool_call_arguments(messages)

    assert repaired == 1
    args = messages[0]["tool_calls"][0]["function"]["arguments"]
    assert args == "{}", f"expected '{{}}', got {args!r}"


def test_valid_object_arguments_preserved():
    """A valid JSON object is preserved verbatim."""
    original_args = '{"path": "/tmp/foo", "mode": "read"}'
    msg = _assistant_message(
        _tool_call(call_id="call_1", arguments=original_args)
    )
    messages = [msg]

    repaired = AIAgent._sanitize_tool_call_arguments(messages)

    assert repaired == 0
    args = messages[0]["tool_calls"][0]["function"]["arguments"]
    assert args == original_args


def test_invalid_json_still_triggers_corruption():
    """Backward-compat: invalid JSON still triggers the corruption path."""
    msg = _assistant_message(
        _tool_call(call_id="call_1", arguments='{"bad":')
    )
    messages = [msg]

    repaired = AIAgent._sanitize_tool_call_arguments(messages)

    assert repaired == 1
    args = messages[0]["tool_calls"][0]["function"]["arguments"]
    assert args == "{}"
