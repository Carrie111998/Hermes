"""Seam tests for the in-file post_api_request hook extraction."""

import ast
import importlib
import inspect
from types import SimpleNamespace

from agent import conversation_loop


_EXPECTED_CALL = (
    "_emit_post_api_request_hook(agent, response, assistant_message, "
    "finish_reason, api_messages, api_call_count, api_duration, "
    "api_start_time, effective_task_id, turn_id, api_request_id)"
)


def test_post_api_request_helper_is_module_level_and_run_conversation_is_preserved():
    assert callable(conversation_loop._emit_post_api_request_hook)
    assert callable(conversation_loop.run_conversation)
    assert inspect.signature(conversation_loop._emit_post_api_request_hook).return_annotation in (None, "None")


def test_run_conversation_calls_post_api_request_helper_at_the_seam():
    source = inspect.getsource(conversation_loop.run_conversation)
    assert _EXPECTED_CALL in source
    tree = ast.parse(source)
    helper_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_emit_post_api_request_hook"
    ]
    assert len(helper_calls) == 1


def test_post_api_request_helper_preserves_hook_payload_values_byte_for_byte(monkeypatch):
    lifecycle = importlib.import_module("hermes_cli.lifecycle")
    captured = {}

    monkeypatch.setattr(lifecycle, "has_hook", lambda name: name == "post_api_request")

    def capture_hook(name, **kwargs):
        captured["name"] = name
        captured["kwargs"] = kwargs

    monkeypatch.setattr(lifecycle, "invoke_hook", capture_hook)

    assistant_text = "payload text: /\\?\u00e9"
    finish_reason = "stop|finish\nreason"
    expected_response_payload = {"opaque": "response: /\\?\u00e9"}
    expected_usage = {"prompt_tokens": 17, "completion_tokens": 23}
    agent = SimpleNamespace(
        session_id="session:/\\?\u00e9",
        platform="platform|name",
        model="model:name",
        provider="provider/name",
        base_url="https://example.invalid/v1?x=1&y=2",
        api_mode="openai_chat",
        _api_response_payload_for_hook=lambda response, message, *, finish_reason: expected_response_payload,
        _usage_summary_for_api_request_hook=lambda response: expected_usage,
    )
    response = SimpleNamespace(model="response-model:1")
    assistant_message = SimpleNamespace(content=assistant_text, tool_calls=[{"id": "tool/1"}])
    api_messages = [{"role": "user"}, {"role": "assistant"}, {"role": "tool"}]

    conversation_loop._emit_post_api_request_hook(
        agent,
        response,
        assistant_message,
        finish_reason,
        api_messages,
        7,
        1.25,
        100.0,
        "task:/\\?\u00e9",
        "turn|id",
        "request/name",
    )

    assert captured == {
        "name": "post_api_request",
        "kwargs": {
            "task_id": "task:/\\?\u00e9",
            "turn_id": "turn|id",
            "api_request_id": "request/name",
            "session_id": "session:/\\?\u00e9",
            "platform": "platform|name",
            "model": "model:name",
            "provider": "provider/name",
            "base_url": "https://example.invalid/v1?x=1&y=2",
            "api_mode": "openai_chat",
            "api_call_count": 7,
            "api_duration": 1.25,
            "started_at": 100.0,
            "ended_at": 101.25,
            "finish_reason": finish_reason,
            "message_count": 3,
            "response_model": "response-model:1",
            "response": expected_response_payload,
            "usage": expected_usage,
            "assistant_message": assistant_message,
            "assistant_content_chars": len(assistant_text),
            "assistant_tool_call_count": 1,
        },
    }
