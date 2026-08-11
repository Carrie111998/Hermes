from types import SimpleNamespace
from unittest.mock import patch

from agent.tool_executor import _session_search_scope_context


def test_session_search_scope_context_uses_native_private_gateway_fields():
    agent = SimpleNamespace(
        session_id="session-1",
        platform="telegram",
        _chat_id="chat-1",
        _thread_id="topic-7",
        chat_id="wrong-public-field",
        thread_id="wrong-public-field",
    )

    assert _session_search_scope_context(agent) == {
        "current_session_id": "session-1",
        "current_platform": "telegram",
        "current_chat_id": "chat-1",
        "current_thread_id": "topic-7",
    }


def test_concurrent_runtime_path_propagates_scope_and_detail():
    from agent.agent_runtime_helpers import invoke_tool

    captured = {}

    def fake_session_search(**kwargs):
        captured.update(kwargs)
        return '{"success": true}'

    agent = SimpleNamespace(
        session_id="session-1",
        platform="telegram",
        _chat_id="chat-1",
        _thread_id="topic-7",
        _current_turn_id="turn-1",
        _current_api_request_id="request-1",
        _get_session_db_for_recall=lambda: object(),
    )

    with (
        patch("tools.session_search_tool.session_search", side_effect=fake_session_search),
        patch("hermes_cli.plugins.resolve_pre_tool_block", return_value=None),
        patch("model_tools._emit_post_tool_call_hook"),
    ):
        result = invoke_tool(
            agent,
            "session_search",
            {"query": "needle", "detail": "full", "scope": "current"},
            "task-1",
            tool_call_id="call-1",
        )

    assert result == '{"success": true}'
    assert captured["detail"] == "full"
    assert captured["scope"] == "current"
    assert captured["current_session_id"] == "session-1"
    assert captured["current_platform"] == "telegram"
    assert captured["current_chat_id"] == "chat-1"
    assert captured["current_thread_id"] == "topic-7"
