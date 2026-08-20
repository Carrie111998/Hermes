"""Regression tests for invalid/None terminal command handling."""

import json

from tools.terminal_tool import _transform_sudo_command, terminal_tool


def test_transform_sudo_command_none_returns_cleanly():
    transformed, sudo_stdin = _transform_sudo_command(None)

    assert transformed is None
    assert sudo_stdin is None


def test_terminal_tool_none_command_returns_clean_error():
    result = json.loads(terminal_tool(None))  # type: ignore[arg-type]

    assert result["exit_code"] == -1
    assert result["status"] == "error"
    assert result["error_code"] == "terminal_command_required"
    assert result["retryable"] is False
    assert result["repair_hint"]["required_field"] == "command"
    assert result["repair_hint"]["example"] == {"command": "pwd"}
    assert "do not repeat" in result["error"].lower()
    assert "nonetype" in result["error"].lower()


def test_terminal_tool_empty_command_returns_same_non_retryable_schema_error():
    result = json.loads(terminal_tool("   "))

    assert result["error_code"] == "terminal_command_required"
    assert result["retryable"] is False
    assert "empty string" in result["error"].lower()
