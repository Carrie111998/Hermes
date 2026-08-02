"""Adversarial contracts for model-authoritative streaming.

Relay/plugin compatibility surfaces may observe lifecycle snapshots elsewhere,
but they never sit between Hermes and a streaming model provider.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import relay_llm, relay_runtime


ATTACKS = (
    "request_mutation",
    "response_replacement",
    "duplicate_provider_callback",
    "observer_exception",
    "delayed_callback",
    "chunk_rewrite",
    "early_stop",
    "provider_bypass",
)


@pytest.mark.parametrize("attack", ATTACKS)
def test_relay_streaming_attacks_never_enter_provider_boundary(
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    """Even a retained managed-execution runtime has no streaming authority."""
    relay_resolution_calls: list[str] = []
    relay_attack_calls: list[str] = []

    class MaliciousRelayLlm:
        async def stream_execute(self, *_args, **_kwargs):
            relay_attack_calls.append(attack)
            raise AssertionError(f"Relay streaming authority invoked: {attack}")

    fake_runtime = SimpleNamespace(
        relay=SimpleNamespace(llm=MaliciousRelayLlm()),
        managed_execution_enabled=lambda: True,
    )

    def resolve_execution_context(session_id: str):
        relay_resolution_calls.append(session_id)
        return fake_runtime, object(), object()

    monkeypatch.setattr(
        relay_runtime,
        "resolve_execution_context",
        resolve_execution_context,
    )
    request = {
        "model": "trusted-model",
        "messages": [{"role": "user", "content": "exact input"}],
        "reasoning": {"effort": "high"},
    }
    request_wire = json.dumps(request, sort_keys=True, separators=(",", ":"))
    provider_calls: list[str] = []
    provider_chunks = [b"first\x00chunk", b"second\xffchunk"]

    def provider(final_request):
        provider_calls.append(
            json.dumps(final_request, sort_keys=True, separators=(",", ":"))
        )
        return iter(provider_chunks)

    stream = relay_llm.stream(
        request,
        provider,
        session_id="session-authority",
        name="trusted-provider",
        model_name="trusted-model",
        finalizer=lambda: pytest.fail(f"Relay finalizer invoked: {attack}"),
        chunk_adapter=lambda _chunk: pytest.fail(
            f"Relay chunk adapter invoked: {attack}"
        ),
        metadata={"attack": attack},
    )

    received = list(stream)

    assert relay_resolution_calls == []
    assert relay_attack_calls == []
    assert provider_calls == [request_wire]
    assert received == provider_chunks
    assert all(actual is expected for actual, expected in zip(received, provider_chunks))
    assert stream.output_modified is False


def test_stream_observer_mutation_and_exception_are_detached_and_fail_open() -> None:
    provider_chunk = {
        "choices": [{"delta": {"content": "provider-authored"}}],
        "finish_reason": "stop",
    }
    observed: list[dict] = []

    def malicious_observer(snapshot: dict) -> None:
        observed.append(snapshot)
        snapshot["choices"][0]["delta"]["content"] = "rewritten"
        raise RuntimeError("observer failed after mutation")

    stream = relay_llm.stream(
        {"model": "trusted-model", "messages": []},
        lambda _request: iter([provider_chunk]),
        session_id="session-authority",
        name="trusted-provider",
        model_name="trusted-model",
        finalizer=dict,
        on_chunk=malicious_observer,
    )

    received = list(stream)

    assert received == [provider_chunk]
    assert received[0] is provider_chunk
    assert provider_chunk["choices"][0]["delta"]["content"] == "provider-authored"
    assert observed[0]["choices"][0]["delta"]["content"] == "rewritten"


def test_trusted_provider_chunk_parser_failure_is_visible() -> None:
    provider_chunk = {"delta": "exact"}

    def broken_trusted_parser(_chunk) -> None:
        raise ValueError("cannot represent provider event")

    stream = relay_llm.provider_stream(
        {"model": "trusted-model", "messages": []},
        lambda _request: iter([provider_chunk]),
        on_provider_chunk=broken_trusted_parser,
    )

    with pytest.raises(ValueError, match="cannot represent provider event"):
        next(stream)


def test_terminal_observer_cannot_replace_provider_stream_failure() -> None:
    class ProviderError(Exception):
        pass

    provider_error = ProviderError("exact provider failure")
    provider_chunk = {"delta": "exact"}
    outcomes: list[str] = []

    def provider(_request):
        yield provider_chunk
        raise provider_error

    def broken_terminal_observer(outcome: str) -> None:
        outcomes.append(outcome)
        raise RuntimeError("notification failed")

    stream = relay_llm.provider_stream(
        {"model": "trusted-model", "messages": []},
        provider,
        on_terminal=broken_terminal_observer,
    )

    assert next(stream) is provider_chunk
    with pytest.raises(ProviderError) as caught:
        next(stream)

    assert caught.value is provider_error
    assert outcomes == ["failed"]
    stream.close()
    assert outcomes == ["failed"]


def test_terminal_observer_cannot_replace_provider_stream_open_failure() -> None:
    class ProviderOpenError(Exception):
        pass

    provider_error = ProviderOpenError("exact provider open failure")
    outcomes: list[str] = []

    def provider(_request):
        raise provider_error

    def broken_terminal_observer(outcome: str) -> None:
        outcomes.append(outcome)
        raise RuntimeError("notification failed")

    with pytest.raises(ProviderOpenError) as caught:
        relay_llm.provider_stream(
            {"model": "trusted-model", "messages": []},
            provider,
            on_terminal=broken_terminal_observer,
        )

    assert caught.value is provider_error
    assert outcomes == ["failed"]


def test_relay_compatibility_cannot_supply_early_stop_predicate() -> None:
    chunks = [{"delta": "first"}, {"delta": "second"}]
    accept_calls: list[dict] = []

    stream = relay_llm.stream(
        {"model": "trusted-model", "messages": []},
        lambda _request: iter(chunks),
        session_id="session-authority",
        name="relay-compat",
        model_name="trusted-model",
        finalizer=dict,
        accept_chunk=lambda chunk: accept_calls.append(chunk) or False,
    )

    assert list(stream) == chunks
    assert accept_calls == []


def _make_agent(*, api_mode: str):
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="trusted-model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.api_mode = api_mode
    agent._interrupt_requested = False
    return agent


def test_anthropic_final_response_is_exact_provider_message() -> None:
    agent = _make_agent(api_mode="anthropic_messages")
    agent._current_api_request_id = "anthropic-primary-1"
    usage = SimpleNamespace(
        input_tokens=17,
        output_tokens=9,
        cache_read_input_tokens=5,
    )
    final_message = SimpleNamespace(
        id="msg_exact",
        model="claude-exact",
        role="assistant",
        content=[
            SimpleNamespace(type="thinking", thinking="trusted reasoning"),
            SimpleNamespace(type="text", text="trusted text"),
            SimpleNamespace(
                type="tool_use",
                id="toolu_exact",
                name="terminal",
                input={"command": "pwd"},
            ),
        ],
        stop_reason="tool_use",
        stop_sequence=None,
        usage=usage,
    )
    events = [
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(
                type="thinking_delta", thinking="trusted reasoning"
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=1,
            delta=SimpleNamespace(type="text_delta", text="trusted text"),
        ),
        SimpleNamespace(
            type="content_block_start",
            index=2,
            content_block=SimpleNamespace(
                type="tool_use", id="toolu_exact", name="terminal", input={}
            ),
        ),
    ]
    raw_stream = MagicMock()
    raw_stream.__iter__ = MagicMock(return_value=iter(events))
    raw_stream.get_final_message.return_value = final_message
    manager = MagicMock()
    manager.__enter__.return_value = raw_stream
    manager.__exit__.return_value = False
    request_client = MagicMock()
    request_client.messages.stream.return_value = manager
    agent._create_request_anthropic_client = lambda *args, **kwargs: request_client

    with patch(
        "agent.relay_llm.provider_stream",
        wraps=relay_llm.provider_stream,
    ) as provider_stream_call:
        response = agent._interruptible_streaming_api_call(
            {"model": "claude-exact", "messages": []}
        )

    request_client.messages.stream.assert_called_once()
    assert provider_stream_call.call_args.kwargs["lifecycle_metadata"] == {
        "api_request_id": "anthropic-primary-1",
        "call_role": "primary",
        "provider": "anthropic",
        "model": agent.model,
        "api_mode": "anthropic_messages",
    }
    assert response is final_message
    assert response.content[0].thinking == "trusted reasoning"
    assert response.content[1].text == "trusted text"
    assert response.content[2].id == "toolu_exact"
    assert response.content[2].name == "terminal"
    assert response.content[2].input == {"command": "pwd"}
    assert response.usage is usage
    assert response.stop_reason == "tool_use"


def test_bedrock_final_response_preserves_reasoning_tool_usage_and_finish() -> None:
    pytest.importorskip("botocore", reason="botocore required for Bedrock parity")
    agent = _make_agent(api_mode="bedrock_converse")
    agent._current_api_request_id = "bedrock-primary-1"
    events = [
        {"messageStart": {"role": "assistant"}},
        {
            "contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {},
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {
                    "reasoningContent": {"text": "trusted reasoning"}
                },
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"text": "trusted text"},
            }
        },
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {
            "contentBlockStart": {
                "contentBlockIndex": 1,
                "start": {
                    "toolUse": {
                        "toolUseId": "call_exact",
                        "name": "terminal",
                    }
                },
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 1,
                "delta": {"toolUse": {"input": '{"command":"pwd"}'}},
            }
        },
        {"contentBlockStop": {"contentBlockIndex": 1}},
        {"messageStop": {"stopReason": "tool_use"}},
        {
            "metadata": {
                "usage": {
                    "inputTokens": 17,
                    "outputTokens": 9,
                    "cacheReadInputTokens": 5,
                    "cacheWriteInputTokens": 3,
                }
            }
        },
    ]
    client = MagicMock()
    client.converse_stream.return_value = {"stream": iter(events)}

    with (
        patch(
            "agent.bedrock_adapter._get_bedrock_runtime_client",
            return_value=client,
        ),
        patch(
            "agent.relay_llm.provider_stream",
            wraps=relay_llm.provider_stream,
        ) as provider_stream_call,
    ):
        response = agent._interruptible_streaming_api_call(
            {"modelId": "bedrock-exact", "messages": []}
        )

    client.converse_stream.assert_called_once()
    assert provider_stream_call.call_args.kwargs["lifecycle_metadata"] == {
        "api_request_id": "bedrock-primary-1",
        "call_role": "primary",
        "provider": "bedrock",
        "model": agent.model,
        "api_mode": "custom",
    }
    message = response.choices[0].message
    assert message.reasoning_content == "trusted reasoning"
    assert message.content == "trusted text"
    assert len(message.tool_calls) == 1
    assert message.tool_calls[0].id == "call_exact"
    assert message.tool_calls[0].function.name == "terminal"
    assert json.loads(message.tool_calls[0].function.arguments) == {"command": "pwd"}
    assert response.choices[0].finish_reason == "tool_calls"
    assert response.usage.prompt_tokens == 25
    assert response.usage.completion_tokens == 9
    assert response.usage.total_tokens == 34


def test_codex_final_response_preserves_items_reasoning_usage_and_status() -> None:
    agent = _make_agent(api_mode="codex_responses")
    agent._current_api_request_id = "codex-primary-1"
    reasoning_deltas: list[str] = []
    agent.reasoning_callback = reasoning_deltas.append
    reasoning_item = SimpleNamespace(
        type="reasoning",
        id="reason_exact",
        summary=[SimpleNamespace(type="summary_text", text="trusted reasoning")],
        encrypted_content="sealed",
        status="completed",
    )
    tool_item = SimpleNamespace(
        type="function_call",
        id="fc_exact",
        call_id="call_exact",
        name="terminal",
        arguments='{"command":"pwd"}',
        status="completed",
    )
    usage = SimpleNamespace(input_tokens=17, output_tokens=9, total_tokens=26)
    events = [
        SimpleNamespace(
            type="response.reasoning_summary_text.delta",
            delta="trusted reasoning",
        ),
        SimpleNamespace(type="response.output_item.done", item=reasoning_item),
        SimpleNamespace(
            type="response.output_item.added",
            item=SimpleNamespace(type="function_call"),
        ),
        SimpleNamespace(type="response.output_item.done", item=tool_item),
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                id="resp_exact",
                status="completed",
                output=None,
                usage=usage,
            ),
        ),
    ]

    class ExactStream:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            return iter(events)

        def close(self):
            self.closed = True

    raw_stream = ExactStream()
    request_client = MagicMock()
    request_client.responses.create.return_value = raw_stream

    with patch(
        "agent.relay_llm.provider_stream",
        wraps=relay_llm.provider_stream,
    ) as provider_stream_call:
        response = agent._run_codex_stream(
            {"model": "codex-exact", "instructions": "exact", "input": []},
            client=request_client,
        )

    request_client.responses.create.assert_called_once()
    assert provider_stream_call.call_args.kwargs["lifecycle_metadata"] == {
        "api_request_id": "codex-primary-1",
        "call_role": "primary",
        "provider": "codex",
        "model": "codex-exact",
        "api_mode": "codex_responses",
    }
    assert raw_stream.closed is True
    assert response.output == [reasoning_item, tool_item]
    assert response.output[0] is reasoning_item
    assert response.output[1] is tool_item
    assert reasoning_deltas == ["trusted reasoning"]
    assert response.usage is usage
    assert response.status == "completed"
    assert response.id == "resp_exact"
    assert response.model == "codex-exact"


def test_completed_provider_response_cannot_be_replaced_by_relay_finalizer() -> None:
    provider_calls = 0
    finalizer_calls = 0
    completed = SimpleNamespace(
        output_text="byte-identical final",
        output=[SimpleNamespace(type="message")],
    )

    def provider(_request):
        nonlocal provider_calls
        provider_calls += 1
        return completed

    def malicious_finalizer():
        nonlocal finalizer_calls
        finalizer_calls += 1
        return SimpleNamespace(output_text="replaced")

    stream = relay_llm.stream(
        {"model": "trusted-model", "input": "exact"},
        provider,
        session_id="session-authority",
        name="trusted-provider",
        model_name="trusted-model",
        finalizer=malicious_finalizer,
        completed_response_predicate=lambda value: hasattr(value, "output"),
    )

    assert list(stream) == []
    assert provider_calls == 1
    assert finalizer_calls == 0
    assert stream.final_response is completed
    assert stream.final_response.output_text.encode() == b"byte-identical final"


def test_stream_current_preserves_completed_response_without_finalizer_authority() -> None:
    provider_calls = 0
    finalizer_calls = 0
    completed = SimpleNamespace(
        model="aux-exact",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="exact auxiliary final"),
                finish_reason="stop",
            )
        ],
    )

    def provider(_request):
        nonlocal provider_calls
        provider_calls += 1
        return completed

    def forbidden_finalizer():
        nonlocal finalizer_calls
        finalizer_calls += 1
        return SimpleNamespace(choices=[])

    result = relay_llm.stream_current(
        {"model": "aux-exact", "messages": [], "stream": True},
        provider,
        name="aux-provider",
        model_name="aux-exact",
        finalizer=forbidden_finalizer,
        completed_response_predicate=lambda value: hasattr(value, "choices"),
    )

    assert provider_calls == 1
    assert finalizer_calls == 0
    assert result is completed
    assert result.choices[0].message.content == "exact auxiliary final"


def test_stream_current_preserves_iterator_without_finalizer_assembly() -> None:
    provider_calls = 0
    finalizer_calls = 0
    chunks = [b"aux-first", b"aux-second"]

    raw_stream = iter(chunks)

    def provider(_request):
        nonlocal provider_calls
        provider_calls += 1
        return raw_stream

    def forbidden_finalizer():
        nonlocal finalizer_calls
        finalizer_calls += 1
        return {"replaced": True}

    stream = relay_llm.stream_current(
        {"model": "aux-exact", "messages": [], "stream": True},
        provider,
        name="aux-provider",
        model_name="aux-exact",
        finalizer=forbidden_finalizer,
        completed_response_predicate=lambda value: hasattr(value, "choices"),
    )

    assert stream is raw_stream
    received = list(stream)
    assert provider_calls == 1
    assert finalizer_calls == 0
    assert received == chunks
    assert all(actual is expected for actual, expected in zip(received, chunks))
