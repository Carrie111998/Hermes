from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from agent.conversation_loop import _issue_provider_attempt_from_agent
from agent.provider_attempt import (
    ProviderAttemptProvenance,
    _issue_retry_provider_attempt,
)


def _begin(**overrides):
    values = {
        "session_id": "session-1",
        "provider": "primary",
        "model": "model-primary",
        "_fallback_activated": False,
        "_fallback_reason": None,
        "effective_task_id": "task-1",
        "turn_id": "turn-1",
        "api_request_id": "turn-1:api:1",
        "attempt_index": 0,
        "retry_count": 0,
    }
    values.update(overrides)
    activate_fallback = bool(values.pop("_fallback_activated"))
    fallback_reason = values.pop("_fallback_reason")
    agent = SimpleNamespace(
        session_id=values.pop("session_id"),
        provider=values.pop("provider"),
        model=values.pop("model"),
        _fallback_activated=False,
        _fallback_generation=0,
        _fallback_reason=None,
    )
    if activate_fallback:
        from agent.chat_completion_helpers import (
            FailoverReason,
            _record_fallback_activation,
        )

        _record_fallback_activation(
            agent,
            getattr(FailoverReason, fallback_reason or "", None),
        )
    return _issue_provider_attempt_from_agent(agent, **values)


def test_same_logical_request_gets_distinct_physical_attempt_ids():
    first = _begin(attempt_index=0, retry_count=0)
    second = _begin(attempt_index=1, retry_count=1)

    assert first.api_request_id == second.api_request_id
    assert first.provider_attempt_id != second.provider_attempt_id
    assert first.attempt_index == 0
    assert second.attempt_index == 1
    assert first.retry_count == 0
    assert second.retry_count == 1


def test_fallback_state_is_snapshotted_from_hermes_agent_state():
    fallback = _begin(
        provider="fallback",
        model="model-fallback",
        _fallback_activated=True,
        _fallback_reason="rate_limit",
        attempt_index=1,
    )

    assert fallback.fallback_used is True
    assert fallback.provider == "fallback"
    assert fallback.request_model == "model-fallback"
    assert fallback.fallback_generation == 1
    assert fallback.fallback_reason == "rate_limit"


def test_missing_producer_fallback_state_fails_closed():
    agent = SimpleNamespace(
        session_id="session-1",
        provider="primary",
        model="model-primary",
    )

    with pytest.raises(RuntimeError, match="fallback state is unavailable"):
        _issue_provider_attempt_from_agent(
            agent,
            effective_task_id="task-1",
            turn_id="turn-1",
            api_request_id="turn-1:api:1",
            attempt_index=0,
            retry_count=0,
        )


def test_response_and_tool_call_are_bound_to_the_same_attempt():
    attempt = _begin(
        provider="fallback",
        model="model-fallback",
        _fallback_activated=True,
        _fallback_reason="upstream_timeout",
    )
    completed = attempt.complete(
        response_model="model-fallback-served",
        outcome="success",
        ended_at=2.0,
    )
    tool = completed.bind_tool_call("tool-call-A")

    assert tool.provider_attempt_id == completed.provider_attempt_id
    assert tool.tool_call_id == "tool-call-A"
    assert tool.response_model == "model-fallback-served"
    assert tool.provider == "fallback"


def test_unrelated_attempt_cannot_rebind_an_existing_tool_call():
    attempt_a = _begin(attempt_index=0)
    attempt_b = _begin(attempt_index=1, retry_count=1)
    tool_a = attempt_a.complete(
        response_model="model-primary",
        outcome="success",
        ended_at=2.0,
    ).bind_tool_call("tool-call-A")

    assert tool_a.provider_attempt_id != attempt_b.provider_attempt_id
    assert tool_a.tool_call_id == "tool-call-A"


def test_attempt_record_is_immutable():
    attempt = _begin()

    with pytest.raises(FrozenInstanceError):
        attempt.provider = "forged"


def test_arbitrary_caller_cannot_construct_a_core_issued_record():
    with pytest.raises(TypeError):
        ProviderAttemptProvenance(
            runtime_instance_id="forged-runtime",
            session_id="session-1",
            task_id="task-1",
            turn_id="turn-1",
            api_request_id="request-1",
            provider_attempt_id="forged-attempt",
            attempt_index=0,
            retry_count=0,
            provider="primary",
            request_model="model-primary",
            response_model=None,
            fallback_used=False,
            fallback_generation=0,
            fallback_reason=None,
            outcome="success",
            started_at=1.0,
            ended_at=2.0,
        )


def test_post_tool_hook_receives_detached_core_projection(monkeypatch):
    import hermes_cli.lifecycle as lifecycle
    from model_tools import _emit_post_tool_call_hook

    attempt = _begin().complete(
        response_model="model-primary-served",
        outcome="success",
        ended_at=2.0,
    )
    captured = {}
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: name == "post_tool_call")
    monkeypatch.setattr(
        lifecycle,
        "invoke_hook",
        lambda event, **kwargs: captured.update({"event": event, **kwargs}),
    )

    _emit_post_tool_call_hook(
        function_name="decision-submit",
        function_args={"decision": "HOLD"},
        result="ok",
        task_id="task-1",
        session_id="session-1",
        turn_id="turn-1",
        api_request_id="turn-1:api:1",
        tool_call_id="tool-call-A",
        provider_attempt=attempt,
    )

    assert captured["provider_attempt_id"] == attempt.provider_attempt_id
    assert "provider_attempt" not in captured
    assert captured["tool_call_provenance"]["tool_call_id"] == "tool-call-A"
    assert (
        captured["provider_attempt_observer"]["provider_attempt_id"]
        == attempt.provider_attempt_id
    )
    captured["provider_attempt_observer"]["provider"] = "mutated-observer-copy"
    assert attempt.provider == "primary"


def test_public_tool_dispatch_does_not_accept_provenance_kwargs():
    from model_tools import handle_function_call

    with pytest.raises(TypeError):
        handle_function_call(
            "decision-submit",
            {},
            task_id="task-1",
            turn_id="turn-1",
            api_request_id="request-1",
            provider_attempt_id="forged-attempt",
            fallback_used=True,
        )


def test_internal_retry_requires_a_fresh_provider_attempt():
    issued = []

    def issue():
        attempt = object()
        issued.append(attempt)
        return attempt

    assert _issue_retry_provider_attempt(issue, retry_index=0) is None
    first_retry = _issue_retry_provider_attempt(issue, retry_index=1)
    second_retry = _issue_retry_provider_attempt(issue, retry_index=2)

    assert first_retry is issued[0]
    assert second_retry is issued[1]
    assert first_retry is not second_retry

    with pytest.raises(RuntimeError, match="without an attempt issuer"):
        _issue_retry_provider_attempt(None, retry_index=1)


def test_codex_internal_stream_retry_gets_a_new_provider_attempt(monkeypatch):
    import httpx

    from agent import codex_runtime, relay_llm

    class FakeStream:
        final_response = None

        def __iter__(self):
            return iter(())

        def close(self):
            return None

    initial_attempt = SimpleNamespace(provider_attempt_id="attempt-A")
    agent = SimpleNamespace(
        _interrupt_requested=False,
        _codex_streamed_text_parts=[],
        _current_provider_attempt=initial_attempt,
        _current_api_request_id="request-1",
        session_id="session-1",
        provider="openai-codex",
        model="gpt-5.6-luna",
        is_subagent=False,
        _fallback_index=0,
        interim_assistant_callback=None,
        show_commentary=True,
        _touch_activity=lambda *_args: None,
        _fire_stream_delta=lambda *_args: None,
        _fire_reasoning_delta=lambda *_args: None,
        _fire_streamed_codex_commentary=lambda *_args: None,
        _client_log_context=lambda: "test-codex",
    )
    issued = []
    metadata = []
    relay_calls = []

    def issue():
        attempt = SimpleNamespace(
            provider_attempt_id=f"attempt-{chr(ord('B') + len(issued))}"
        )
        issued.append(attempt)
        agent._current_provider_attempt = attempt
        return attempt

    def fake_stream(_request, _factory, **kwargs):
        relay_calls.append(1)
        metadata.append(kwargs["metadata"])
        if len(relay_calls) == 1:
            raise httpx.ConnectError("simulated physical stream failure")
        return FakeStream()

    monkeypatch.setattr(relay_llm, "stream", fake_stream)
    monkeypatch.setattr(
        codex_runtime,
        "_consume_codex_event_stream",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="completed",
            incomplete_details=None,
            error=None,
        ),
    )

    result = codex_runtime.run_codex_stream(
        agent,
        {"model": "gpt-5.6-luna"},
        client=object(),
        issue_provider_attempt=issue,
    )

    assert result.status == "completed"
    assert len(relay_calls) == 2
    assert len(issued) == 1
    assert metadata[0]["provider_attempt_id"] == "attempt-A"
    assert metadata[1]["provider_attempt_id"] == "attempt-B"
    assert metadata[0]["provider_attempt_id"] != metadata[1]["provider_attempt_id"]
