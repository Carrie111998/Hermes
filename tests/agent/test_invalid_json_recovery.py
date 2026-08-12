"""Invalid-JSON tool-call recovery is keyed by call index, not tool name.

Two parallel calls to the same tool (one valid JSON, one invalid) used to
collide: the valid sibling inherited the invalid call's error because recovery
matched by ``tool_name``. The recovery map must match by call index so a valid
sibling call is only ever marked "Skipped", never given its sibling's error.
"""

from __future__ import annotations

from agent.conversation_loop import _invalid_json_recovery_content


def test_valid_sibling_with_same_name_is_not_marked_invalid():
    # Two parallel calls to the same tool: index 0 has invalid JSON, index 1
    # is valid. Recovery must key by index, so the valid sibling is "Skipped".
    tool_calls = ["write_file", "write_file"]
    invalid_json_args = [(0, "write_file", "Expecting value: line 1")]

    content = _invalid_json_recovery_content(tool_calls, invalid_json_args)

    assert "Invalid JSON arguments" in content[0]
    assert content[1] == "Skipped: other tool call in this response had invalid JSON."


def test_invalid_call_receives_its_own_error():
    tool_calls = ["read_file", "web_search"]
    invalid_json_args = [(1, "web_search", "Extra data: line 5")]

    content = _invalid_json_recovery_content(tool_calls, invalid_json_args)

    assert content[0].startswith("Skipped:")
    assert "Invalid JSON arguments" in content[1]
    assert "Extra data" in content[1]


def test_multiple_invalid_indices_are_isolated():
    tool_calls = ["a", "b", "c"]
    invalid_json_args = [(0, "a", "err1"), (2, "c", "err2")]

    content = _invalid_json_recovery_content(tool_calls, invalid_json_args)

    assert "err1" in content[0]
    assert content[1].startswith("Skipped:")
    assert "err2" in content[2]
