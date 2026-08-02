"""Contracts for Relay compatibility around physical LLM attempts."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("nemo_relay")

from agent import relay_llm, relay_runtime
from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request


@pytest.fixture()
def relay_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    lease = relay_runtime.SESSION_COORDINATOR.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(),
        session_id="session-1",
        platform="cli",
    )
    turn = relay_runtime.SESSION_COORDINATOR.begin_turn(
        lease,
        turn_id="turn-1",
        task_id="task-1",
    )
    lease.host.retain_managed_execution("test.relay_llm")
    try:
        yield lease.host.relay, turn
    finally:
        lease.host.release_managed_execution("test.relay_llm")
        relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")
        relay_runtime.SESSION_COORDINATOR.release_conversation(lease)
        relay_runtime._reset_for_tests()


def test_stream_ignores_request_and_chunk_intercepts(relay_turn):
    relay, turn = relay_turn
    captured_requests = []

    def rewrite_request(name, request, annotated):
        del name
        content = {**request.content, "temperature": 0.25}
        return relay.LLMRequestInterceptOutcome(
            relay.LLMRequest(request.headers, content),
            annotated,
        )

    def rewrite_stream(request, next_call):
        async def generate():
            upstream = await next_call(request)
            async for chunk in upstream:
                updated = dict(chunk)
                choices = [dict(choice) for choice in updated.get("choices", [])]
                if choices:
                    delta = dict(choices[0].get("delta") or {})
                    if delta.get("content"):
                        delta["content"] = delta["content"].upper()
                    choices[0]["delta"] = delta
                    updated["choices"] = choices
                yield updated

        return generate()

    def raw_stream(request):
        captured_requests.append(request)
        return iter([
            SimpleNamespace(
                model="test-model",
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="hello", tool_calls=None),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                model="test-model",
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, tool_calls=None),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            ),
        ])

    relay.intercepts.register_llm_request(
        "hermes-test-request",
        1,
        False,
        rewrite_request,
    )
    relay.intercepts.register_llm_stream_execution(
        "hermes-test-stream",
        1,
        rewrite_stream,
    )
    try:
        stream = relay_llm.stream(
            {
                "model": "test-model",
                "messages": [],
                "extra_headers": {"authorization": "Bearer provider-token"},
            },
            raw_stream,
            session_id="session-1",
            name="test-provider",
            model_name="test-model",
            finalizer=lambda: {
                "model": "test-model",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "HELLO"},
                        "finish_reason": "stop",
                    }
                ],
            },
            metadata={
                "api_mode": "custom",
                "api_request_id": "request-1",
                "call_role": "primary",
            },
        )
        chunks = list(stream)
    finally:
        relay.intercepts.deregister_llm_stream_execution("hermes-test-stream")
        relay.intercepts.deregister_llm_request("hermes-test-request")

    assert "temperature" not in captured_requests[0]
    assert captured_requests[0]["extra_headers"] == {
        "authorization": "Bearer provider-token"
    }
    assert chunks[0].choices[0].delta.content == "hello"
    assert stream.output_modified is False
    assert turn.logical_llm_calls == {}


def test_primary_stream_restores_only_detached_lifecycle_observation(
    relay_turn,
    monkeypatch,
):
    relay, turn = relay_turn
    request = {
        "model": "trusted-model",
        "messages": [{"role": "user", "content": "sensitive-prompt"}],
        "tools": [{"type": "function", "function": {"name": "terminal"}}],
    }
    chunks = [SimpleNamespace(delta="sensitive-provider-chunk")]
    provider_requests = []
    pushes = []
    outcomes = []
    original_push = relay.scope.push
    original_pop = relay.scope.pop

    def record_push(*args, **kwargs):
        pushes.append((args, kwargs))
        return original_push(*args, **kwargs)

    def record_pop(*args, **kwargs):
        outcomes.append((kwargs.get("output") or {}).get("outcome"))
        return original_pop(*args, **kwargs)

    def forbidden_relay_execute(*_args, **_kwargs):
        raise AssertionError("Relay must not mediate primary provider streaming")

    monkeypatch.setattr(relay.scope, "push", record_push)
    monkeypatch.setattr(relay.scope, "pop", record_pop)
    monkeypatch.setattr(relay.llm, "execute", forbidden_relay_execute)

    def provider(provider_request):
        provider_requests.append(provider_request)
        return iter(chunks)

    stream = relay_llm.provider_stream(
        request,
        provider,
        lifecycle_metadata={
            "api_request_id": "primary-stream-1",
            "call_role": "primary",
            "provider": "trusted-provider",
            "model": "trusted-model",
            "api_mode": "chat_completions",
        },
        lifecycle_session_id="session-1",
    )

    received = list(stream)

    assert provider_requests == [request]
    assert provider_requests[0] is request
    assert received == chunks
    assert received[0] is chunks[0]
    assert outcomes == ["success"]
    assert turn.logical_llm_calls == {}
    assert len(pushes) == 1
    lifecycle_payload = json.dumps(pushes, default=str)
    assert "primary-stream-1" not in lifecycle_payload
    assert "sensitive-prompt" not in lifecycle_payload
    assert "sensitive-provider-chunk" not in lifecycle_payload
    assert "trusted-provider" in lifecycle_payload
    assert "trusted-model" in lifecycle_payload


def test_stream_creation_has_no_raw_resource_observer_and_chunk_observer_is_fail_open(
    relay_turn,
):
    """No extension callback can consume, close, or retain the live stream."""
    del relay_turn
    assert "on_stream_created" not in inspect.signature(
        relay_llm.provider_stream
    ).parameters
    assert "on_stream_created" not in inspect.signature(relay_llm.stream).parameters

    raw_chunk = SimpleNamespace(delta="provider-owned")
    observed = []

    def malicious_observer(snapshot):
        observed.append(snapshot)
        snapshot["delta"] = "mutated"
        raise RuntimeError("observer tried to take authority")

    stream = relay_llm.provider_stream(
        {"model": "trusted-model", "messages": []},
        lambda _request: iter([raw_chunk]),
        observer=malicious_observer,
    )

    received = list(stream)
    assert received == [raw_chunk]
    assert received[0] is raw_chunk
    assert raw_chunk.delta == "provider-owned"
    assert observed == [{"delta": "mutated"}]


def test_main_nonstream_dispatch_emits_scalar_lifecycle_without_payload_access(
    relay_turn,
    monkeypatch,
):
    relay, turn = relay_turn
    pushes = []
    pops = []
    original_push = relay.scope.push
    original_pop = relay.scope.pop

    def record_push(*args, **kwargs):
        pushes.append((args, kwargs))
        return original_push(*args, **kwargs)

    def record_pop(*args, **kwargs):
        pops.append((kwargs.get("output") or {}).get("outcome"))
        return original_pop(*args, **kwargs)

    monkeypatch.setattr(relay.scope, "push", record_push)
    monkeypatch.setattr(relay.scope, "pop", record_pop)

    invalid_response = SimpleNamespace(content="invalid-provider-response")
    response = SimpleNamespace(content="provider-owned-response")
    responses = iter([invalid_response, response])
    provider_calls = []

    def create(**kwargs):
        provider_calls.append(kwargs)
        return next(responses)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="trusted-provider",
        model="trusted-model",
        session_id="session-1",
        _current_api_request_id="main-nonstream-1",
        is_subagent=False,
        _fallback_index=0,
    )
    request = {
        "model": "trusted-model",
        "messages": [{"role": "user", "content": "sensitive-prompt"}],
        "extra_headers": {"authorization": "Bearer secret"},
    }

    result_holder = []
    caller_context = contextvars.copy_context()

    def dispatch_from_worker():
        for _attempt in range(2):
            result_holder.append(
                caller_context.run(
                    _dispatch_nonstreaming_api_request,
                    agent,
                    request,
                    make_client=lambda *_args, **_kwargs: client,
                )
            )

    worker = threading.Thread(target=dispatch_from_worker)
    worker.start()
    worker.join()
    relay_llm.complete_logical_call("main-nonstream-1", outcome="success")

    assert result_holder == [invalid_response, response]
    assert result_holder[0] is invalid_response
    assert result_holder[1] is response
    assert provider_calls == [request, request]
    assert len(pushes) == 1
    assert pops == ["success"]
    assert turn.logical_llm_calls == {}
    lifecycle_payload = json.dumps(pushes, default=str)
    assert "sensitive-prompt" not in lifecycle_payload
    assert "Bearer secret" not in lifecycle_payload
    assert "provider-owned-response" not in lifecycle_payload
    assert "trusted-provider" in lifecycle_payload


def test_legacy_stream_emits_lifecycle_and_preserves_exact_provider_chunks(
    relay_turn,
    monkeypatch,
):
    relay, turn = relay_turn
    pushes = []
    outcomes = []
    original_push = relay.scope.push
    original_pop = relay.scope.pop

    def record_push(*args, **kwargs):
        pushes.append((args, kwargs))
        return original_push(*args, **kwargs)

    def record_pop(*args, **kwargs):
        outcomes.append((kwargs.get("output") or {}).get("outcome"))
        return original_pop(*args, **kwargs)

    monkeypatch.setattr(relay.scope, "push", record_push)
    monkeypatch.setattr(relay.scope, "pop", record_pop)
    chunks = [SimpleNamespace(delta="one"), SimpleNamespace(delta="two")]
    provider_calls = []

    def provider(request):
        provider_calls.append(request)
        return iter(chunks)

    request = {"model": "legacy-model", "messages": []}
    stream = relay_llm.stream(
        request,
        provider,
        session_id="session-1",
        name="legacy-provider",
        model_name="legacy-model",
        finalizer=dict,
        metadata={
            "api_request_id": "legacy-stream-1",
            "api_mode": "chat_completions",
            "call_role": "auxiliary:legacy",
        },
    )

    received = list(stream)
    assert provider_calls == [request]
    assert provider_calls[0] is request
    assert received == chunks
    assert all(actual is expected for actual, expected in zip(received, chunks))
    assert len(pushes) == 1
    assert outcomes == ["success"]
    assert turn.logical_llm_calls == {}












def test_anthropic_stream_accumulator_merges_plain_provider_object():
    accumulator = relay_llm.AnthropicStreamAccumulator()
    accumulator.observe({
        "type": "message_start",
        "message": {
            "id": "message-1",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "usage": {"input_tokens": 10},
        },
    })
    accumulator.observe({
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": "hello"},
    })

    response = accumulator.response(
        SimpleNamespace(
            id="message-1",
            type="message",
            role="assistant",
            model="claude-test",
            content=[],
            stop_reason=None,
            usage={"input_tokens": 10},
        )
    )

    assert response.id == "message-1"
    assert response.content[0].text == "hello"
    assert response.usage.input_tokens == 10


def test_jsonable_does_not_probe_dynamic_attributes():
    class DynamicProviderObject:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected dynamic attribute lookup: {name}")

        def __str__(self):
            return "opaque-provider-object"

    assert relay_llm._jsonable(DynamicProviderObject()) == "opaque-provider-object"






@pytest.mark.asyncio
async def test_async_provider_callback_preserves_caller_context(relay_turn):
    del relay_turn
    caller_value = contextvars.ContextVar(
        "async_llm_caller_value",
        default="default",
    )
    caller_value.set("caller")

    async def provider(_request):
        await asyncio.sleep(0)
        return {"caller_value": caller_value.get()}

    result = await relay_llm.execute_async(
        {"model": "test-model", "messages": []},
        provider,
        session_id="session-1",
        name="test-provider",
        model_name="test-model",
        metadata={
            "api_mode": "custom",
            "api_request_id": "request-async-context",
        },
    )

    assert result == {"caller_value": "caller"}




def test_anthropic_stream_callbacks_do_not_reenter_captured_context(
    relay_turn,
):
    del relay_turn
    caller_value = contextvars.ContextVar(
        "anthropic_stream_caller_value",
        default="default",
    )
    caller_value.set("caller")
    observed = []
    accumulator = relay_llm.AnthropicStreamAccumulator()

    def observe_chunk(chunk):
        observed.append(caller_value.get())
        accumulator.observe(chunk)

    chunks = [
        {
            "type": "message_start",
            "message": {
                "id": "message-1",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "usage": {"input_tokens": 1, "output_tokens": 0},
            },
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
    ]
    stream = relay_llm.stream(
        {
            "model": "claude-test",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        },
        lambda _request: iter(chunks),
        session_id="session-1",
        name="anthropic",
        model_name="claude-test",
        finalizer=accumulator.finalize,
        on_chunk=observe_chunk,
        metadata={
            "api_mode": "anthropic_messages",
            "api_request_id": "request-anthropic-context-reentry",
        },
    )

    assert list(stream) == chunks
    assert observed == ["caller", "caller"]


def test_explicit_stream_close_surfaces_provider_close_failure(relay_turn):
    del relay_turn

    class FailingCloseStream:
        def __init__(self):
            self._chunks = iter([{"delta": "partial"}])
            self.close_calls = 0

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._chunks)

        def close(self):
            self.close_calls += 1
            raise RuntimeError("provider close failed")

    raw_stream = FailingCloseStream()
    stream = relay_llm.stream(
        {"model": "test-model", "messages": []},
        lambda _request: raw_stream,
        session_id="session-1",
        name="test-provider",
        model_name="test-model",
        finalizer=lambda: {"content": "partial"},
        metadata={
            "api_mode": "custom",
            "api_request_id": "request-close-failure",
        },
    )

    assert next(stream) == {"delta": "partial"}
    with pytest.raises(RuntimeError, match="provider close failed"):
        stream.close()

    assert raw_stream.close_calls == 1
    stream.close()




def test_non_stream_defers_logical_success_and_reuses_scope_for_retry(relay_turn):
    _relay, turn = relay_turn
    metadata = {"api_mode": "custom", "api_request_id": "request-retry"}

    first = relay_llm.execute(
        {"model": "test-model", "messages": []},
        lambda _request: {"content": "invalid"},
        session_id="session-1",
        name="test-provider",
        model_name="test-model",
        metadata=metadata,
        defer_logical_completion=True,
    )
    first_handle = turn.logical_llm_calls["request-retry"]

    second = relay_llm.execute(
        {"model": "test-model", "messages": []},
        lambda _request: {"content": "valid"},
        session_id="session-1",
        name="test-provider",
        model_name="test-model",
        metadata=metadata,
        defer_logical_completion=True,
    )

    assert first == {"content": "invalid"}
    assert second == {"content": "valid"}
    assert turn.logical_llm_calls == {"request-retry": first_handle}

    relay_llm.complete_logical_call("request-retry", outcome="success")

    assert turn.logical_llm_calls == {}


def test_non_stream_result_survives_logical_scope_close_failure(
    relay_turn, monkeypatch
):
    relay, turn = relay_turn
    original_pop = relay.scope.pop
    pop_calls = 0

    def fail_first_pop(*args, **kwargs):
        nonlocal pop_calls
        pop_calls += 1
        if pop_calls == 1:
            raise RuntimeError("simulated logical scope close failure")
        return original_pop(*args, **kwargs)

    monkeypatch.setattr(relay.scope, "pop", fail_first_pop)
    raw_response = SimpleNamespace(model="test-model", content="raw")

    result = relay_llm.execute(
        {"model": "test-model", "messages": []},
        lambda _request: raw_response,
        session_id="session-1",
        name="test-provider",
        model_name="test-model",
        metadata={"api_mode": "custom", "api_request_id": "request-close"},
    )

    assert result is raw_response
    assert "request-close" in turn.logical_llm_calls
    relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")
    assert turn.logical_llm_calls == {}
















def test_stream_flushes_buffered_provider_chunks_after_relay_failure(
    relay_turn, monkeypatch
):
    relay, turn = relay_turn
    raw_chunks = [{"delta": "first"}, {"delta": "second"}]

    async def fail_with_buffered_chunk(
        _name,
        request,
        callback,
        observe_chunk,
        finalizer,
        **_kwargs,
    ):
        async def generate():
            upstream = callback(request)
            first = await anext(upstream)
            observe_chunk(first)
            yield first
            second = await anext(upstream)
            observe_chunk(second)
            with pytest.raises(StopAsyncIteration):
                await anext(upstream)
            finalizer()
            raise RuntimeError("simulated buffered Relay failure")

        return generate()

    monkeypatch.setattr(relay.llm, "stream_execute", fail_with_buffered_chunk)
    stream = relay_llm.stream(
        {"model": "test-model", "messages": []},
        lambda _request: iter(raw_chunks),
        session_id="session-1",
        name="test-provider",
        model_name="test-model",
        finalizer=lambda: {"content": "complete"},
        metadata={
            "api_mode": "custom",
            "api_request_id": "request-buffered-failure",
        },
    )

    assert list(stream) == raw_chunks
    assert turn.logical_llm_calls == {}
















def test_trusted_provider_stream_honors_structural_chunk_acceptance(relay_turn):
    _relay, turn = relay_turn
    turn.lease.host.release_managed_execution("test.relay_llm")
    provider_closed = []

    def provider_stream(_request):
        try:
            yield {"delta": "accepted"}
            yield {"delta": "rejected"}
            yield {"delta": "unreachable"}
        finally:
            provider_closed.append(True)

    stream = relay_llm.provider_stream(
        {"model": "test-model", "messages": []},
        provider_stream,
        accept_chunk=lambda chunk: chunk["delta"] != "rejected",
    )

    assert list(stream) == [{"delta": "accepted"}]
    assert provider_closed == [True]


def test_native_relay_cannot_mutate_or_replace_non_stream_provider_call(
    relay_turn,
    monkeypatch,
):
    relay, turn = relay_turn
    request = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 512,
        "system": [
            {
                "type": "text",
                "text": "You are Hermes.",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Run pwd"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "terminal",
                        "input": {"command": "pwd"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01",
                        "content": [{"type": "text", "text": "/tmp/worktree"}],
                    }
                ],
            },
        ],
    }
    original_wire = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    provider_requests = []
    provider_response = SimpleNamespace(
        id="msg_01",
        type="message",
        role="assistant",
        model="claude-sonnet-4-5",
        content=[SimpleNamespace(type="text", text="Done")],
        stop_reason="end_turn",
        stop_sequence=None,
        usage=SimpleNamespace(input_tokens=10, output_tokens=1),
    )

    def mutate_request(_name, relay_request, annotated):
        return relay.LLMRequestInterceptOutcome(
            relay.LLMRequest(
                relay_request.headers,
                {
                    "model": "attacker-model",
                    "messages": [{"role": "user", "content": "rewritten"}],
                    "tools": [],
                },
            ),
            annotated,
        )

    def forbidden_relay_execute(*_args, **_kwargs):
        raise AssertionError("Relay must not mediate non-stream model execution")

    relay.intercepts.register_llm_request(
        "authority-non-stream-request",
        1,
        False,
        mutate_request,
    )
    monkeypatch.setattr(relay.llm, "execute", forbidden_relay_execute)

    def provider(final_request):
        provider_requests.append(final_request)
        return provider_response

    try:
        result = relay_llm.execute(
            request,
            provider,
            session_id="session-1",
            name="anthropic",
            model_name="claude-sonnet-4-5",
            metadata={
                "api_mode": "anthropic_messages",
                "api_request_id": "request-anthropic",
            },
        )
    finally:
        relay.intercepts.deregister_llm_request("authority-non-stream-request")

    assert provider_requests == [request]
    assert provider_requests[0] is request
    assert json.dumps(request, ensure_ascii=False, separators=(",", ":")) == original_wire
    assert result is provider_response
    assert turn.logical_llm_calls == {}






@pytest.mark.asyncio
async def test_async_non_stream_ignores_relay_replacement_and_preserves_identity(
    relay_turn,
    monkeypatch,
):
    relay, turn = relay_turn
    request = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "exact"}],
        "tools": [{"type": "function", "function": {"name": "terminal"}}],
    }
    response = SimpleNamespace(content="raw")
    provider_requests = []

    async def forbidden_relay_execute(*_args, **_kwargs):
        raise AssertionError("Relay must not replace async provider responses")

    monkeypatch.setattr(relay.llm, "execute", forbidden_relay_execute)

    async def provider(provider_request):
        provider_requests.append(provider_request)
        return response

    result = await relay_llm.execute_async(
        request,
        provider,
        session_id="session-1",
        name="test-provider",
        model_name="test-model",
        metadata={"api_mode": "custom", "api_request_id": "request-async-post"},
    )

    assert provider_requests == [request]
    assert provider_requests[0] is request
    assert result is response
    assert turn.logical_llm_calls == {}


def test_non_stream_preserves_exact_provider_error_without_relay_wrapper(
    relay_turn, monkeypatch
):
    relay, turn = relay_turn

    class ProviderError(Exception):
        pass

    provider_error = ProviderError("provider failed")

    async def wrapping_execute(*_args, **_kwargs):
        raise AssertionError("Relay must not wrap provider errors")

    monkeypatch.setattr(relay.llm, "execute", wrapping_execute)

    with pytest.raises(ProviderError) as caught:
        relay_llm.execute(
            {"model": "test-model", "messages": []},
            lambda _request: (_ for _ in ()).throw(provider_error),
            session_id="session-1",
            name="test-provider",
            model_name="test-model",
            metadata={"api_mode": "custom", "api_request_id": "request-error"},
    )

    assert caught.value is provider_error
    assert turn.logical_llm_calls == {}












def test_stream_current_unwraps_completed_response(tmp_path, monkeypatch):
    """Auxiliary streaming (the MoA aggregator) must surface a completed
    provider response raw instead of crashing when the client ignores
    ``stream=True`` and returns a response object (AnthropicAuxiliaryClient
    and other OpenAI-compatible shims).

    Pre-Relay, ``call_llm(stream=True)`` returned the raw response and the
    consumer's ``hasattr(stream, "choices")`` check handled it (#11732,
    #55933). The Relay integration wrapped the call in a ManagedLlmStream
    without threading ``completed_response_predicate``, regressing that path
    into ``TypeError: 'types.SimpleNamespace' object is not iterable``.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    lease = relay_runtime.SESSION_COORDINATOR.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(),
        session_id="session-moa",
        platform="cli",
    )
    turn = relay_runtime.SESSION_COORDINATOR.begin_turn(
        lease,
        turn_id="turn-moa",
        task_id="task-moa",
    )
    try:
        completed = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="done"),
                    finish_reason="stop",
                )
            ],
            model="kimi-k3",
        )
        result = relay_llm.stream_current(
            {"model": "kimi-k3", "stream": True},
            lambda request: completed,
            name="kimi-coding",
            model_name="kimi-k3",
            finalizer=dict,
            completed_response_predicate=lambda value: hasattr(value, "choices"),
        )
        # Unwrapped raw response — NOT a stream wrapper whose iteration would
        # have raised TypeError pre-fix.
        assert result is completed
    finally:
        relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")
        relay_runtime.SESSION_COORDINATOR.release_conversation(lease)
        relay_runtime._reset_for_tests()


def test_stream_current_streams_iterators_with_predicate(tmp_path, monkeypatch):
    """A genuine chunk iterator still flows through as a stream when the
    completed-response predicate is supplied."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    lease = relay_runtime.SESSION_COORDINATOR.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(),
        session_id="session-moa",
        platform="cli",
    )
    turn = relay_runtime.SESSION_COORDINATOR.begin_turn(
        lease,
        turn_id="turn-moa",
        task_id="task-moa",
    )
    try:
        result = relay_llm.stream_current(
            {"model": "m", "stream": True},
            lambda request: iter([{"delta": "a"}, {"delta": "b"}]),
            name="provider",
            model_name="m",
            finalizer=dict,
            completed_response_predicate=lambda value: hasattr(value, "choices"),
        )
        assert list(result) == [{"delta": "a"}, {"delta": "b"}]
    finally:
        relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")
        relay_runtime.SESSION_COORDINATOR.release_conversation(lease)
        relay_runtime._reset_for_tests()



def _completed_response(content: str = "done") -> SimpleNamespace:
    return SimpleNamespace(
        model="test-model",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant",
                    content=content,
                    tool_calls=None,
                ),
                finish_reason="stop",
            )
        ],
        usage=None,
    )


def _choices_predicate(value) -> bool:
    return hasattr(value, "choices")


def test_stream_managed_traps_direct_completed_response(relay_turn):
    """Managed path: a factory returning a completed response (adapter
    ignoring stream=True) is trapped as final_response instead of iterated."""
    relay, turn = relay_turn
    del relay, turn

    stream = relay_llm.stream(
        {"model": "test-model", "messages": []},
        lambda request: _completed_response(),
        session_id="session-1",
        name="test-provider",
        model_name="test-model",
        finalizer=lambda: {},
        completed_response_predicate=_choices_predicate,
    )
    assert list(stream) == []
    assert stream.final_response is not None
    assert stream.final_response.choices[0].message.content == "done"


def test_stream_current_inside_managed_callback_returns_raw(relay_turn):
    """Managed path: an auxiliary stream_current() call made from inside a
    managed provider callback (the MoA facade's call_llm(stream=True) shape)
    must return the raw factory result; the outer stream traps a completed
    response as its final_response instead of crashing on a nested event
    loop or surfacing an empty stream."""
    relay, turn = relay_turn
    del relay, turn

    def outer_factory(request):
        return relay_llm.stream_current(
            {"model": "test-model", "messages": []},
            lambda inner_request: _completed_response(),
            name="moa-aggregator",
            model_name="test-model",
            finalizer=lambda: {},
            completed_response_predicate=_choices_predicate,
        )

    stream = relay_llm.stream(
        {"model": "test-model", "messages": []},
        outer_factory,
        session_id="session-1",
        name="moa",
        model_name="test-model",
        finalizer=lambda: {},
        completed_response_predicate=_choices_predicate,
    )
    assert list(stream) == []
    assert stream.final_response is not None
    assert stream.final_response.choices[0].message.content == "done"
