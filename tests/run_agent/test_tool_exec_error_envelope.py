"""Executor-level tool errors use the same JSON envelope as tool handlers.

Timeout / exception / thread-failure paths used to emit free-text
``"Error executing tool 'X': ..."`` while tool handlers return ``tool_error()``'s
JSON envelope, so the model could not tell whether a side effect had happened
or whether retry was safe. ``_tool_exec_error`` unifies them.
"""

from __future__ import annotations

import json

from agent.tool_executor import _tool_exec_error


def test_tool_exec_error_is_json_envelope():
    data = json.loads(_tool_exec_error("boom"))
    assert data["error"] == "boom"
    assert data["effect_disposition"] == "unknown"
    assert data["retryable"] is False


def test_tool_exec_error_custom_disposition():
    data = json.loads(
        _tool_exec_error(
            "not started", effect_disposition="not_started", retryable=True
        )
    )
    assert data["effect_disposition"] == "not_started"
    assert data["retryable"] is True


def test_tool_exec_error_detected_as_failure():
    # The same detection path used for handler errors must recognize the
    # executor envelope as a failure (JSON {"error": ...}).
    from agent.display import _detect_tool_failure

    is_failure, suffix = _detect_tool_failure(
        "web_search", _tool_exec_error("Error executing tool 'web_search': boom")
    )
    assert is_failure is True
    assert "boom" in suffix
