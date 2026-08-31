"""Tool-result callbacks must receive the same redacted egress payload as the model."""

from types import SimpleNamespace


def test_terminal_post_hook_receives_redacted_result_and_error(monkeypatch):
    from agent.tool_executor import _emit_terminal_post_tool_call

    captured = {}

    def capture_hook(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("model_tools._emit_post_tool_call_hook", capture_hook)
    query_value = "query-" + "opaque-callback-value"
    refresh_value = "refresh-" + "opaque-callback-value"
    raw = (
        '{"refresh_token": "'
        + refresh_value
        + '"} https://example.test/cb?token='
        + query_value
    )
    agent = SimpleNamespace(
        session_id="synthetic-session",
        _current_turn_id="synthetic-turn",
        _current_api_request_id="synthetic-request",
    )

    _emit_terminal_post_tool_call(
        agent,
        function_name="synthetic_tool",
        function_args={},
        result=raw,
        effective_task_id="synthetic-task",
        tool_call_id="synthetic-call",
        error_message=raw,
    )

    assert refresh_value not in captured["result"]
    assert query_value not in captured["result"]
    assert refresh_value not in captured["error_message"]
    assert query_value not in captured["error_message"]
