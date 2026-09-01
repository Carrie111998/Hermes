"""Focused contract tests for provider-response normalization."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from agent import conversation_loop, turn_response


class _Transport:
    def __init__(self):
        self.kwargs = None

    def normalize_response(self, response, **kwargs):
        self.kwargs = kwargs
        return response


class _Agent:
    api_mode = "anthropic_messages"
    _is_anthropic_oauth = True
    session_id = "session-1"
    platform = "desktop"
    model = "model-1"
    provider = "provider-1"
    base_url = "https://example.invalid"

    def __init__(self, transport):
        self.transport = transport

    def _get_transport(self):
        return self.transport

    def _api_response_payload_for_hook(self, response, message, *, finish_reason):
        return {"finish_reason": finish_reason, "content": message.content}

    def _usage_summary_for_api_request_hook(self, response):
        return {"output_tokens": 3}


def _normalize(agent, response, messages=None):
    return turn_response.normalize_turn_response(
        agent,
        response,
        [] if messages is None else messages,
        task_id="task-1",
        turn_id="turn-1",
        api_request_id="request-1",
        api_call_count=2,
        api_start_time=10.0,
        api_duration=1.5,
        api_message_count=4,
        moa_references={"count": 0},
    )


def test_normalizes_nonstandard_content_and_preserves_anthropic_flag(monkeypatch):
    transport = _Transport()
    agent = _Agent(transport)
    response = SimpleNamespace(content=["first", {"type": "text", "text": "second"}], finish_reason="stop")
    projections = []
    monkeypatch.setattr(
        turn_response,
        "splice_provider_projection",
        lambda *args: projections.append(args),
    )

    result = _normalize(agent, response, [{"role": "user", "content": "hello"}])

    assert result.assistant_message is response
    assert result.finish_reason == "stop"
    assert response.content == "first\nsecond"
    assert transport.kwargs == {"strip_tool_prefix": True}
    assert len(projections) == 1


def test_post_api_request_reports_normalized_response_without_blocking(monkeypatch):
    transport = _Transport()
    agent = _Agent(transport)
    response = SimpleNamespace(
        content={"text": "answer"},
        finish_reason="tool_calls",
        tool_calls=[object(), object()],
        model="provider-model",
    )
    emitted = []
    import hermes_cli.lifecycle as lifecycle

    monkeypatch.setattr(turn_response, "splice_provider_projection", lambda *args: None)
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: name == "post_api_request")
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda name, **kwargs: emitted.append((name, kwargs)))

    _normalize(agent, response)

    assert response.content == "answer"
    assert len(emitted) == 1
    name, payload = emitted[0]
    assert name == "post_api_request"
    assert payload["ended_at"] == 11.5
    assert payload["assistant_content_chars"] == 6
    assert payload["assistant_tool_call_count"] == 2
    assert payload["response"] == {"finish_reason": "tool_calls", "content": "answer"}


def test_post_api_request_failure_is_observational_only(monkeypatch):
    transport = _Transport()
    agent = _Agent(transport)
    response = SimpleNamespace(content="answer", finish_reason="stop")
    import hermes_cli.lifecycle as lifecycle

    monkeypatch.setattr(turn_response, "splice_provider_projection", lambda *args: None)
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: True)
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError()))

    assert _normalize(agent, response).assistant_message is response


def test_conversation_loop_uses_response_module_as_the_owner():
    source = inspect.getsource(conversation_loop.run_conversation)
    assert "normalized_response = normalize_turn_response(" in source
