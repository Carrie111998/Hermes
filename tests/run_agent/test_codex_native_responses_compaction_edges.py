"""Protocol edge cases for native Responses compaction."""

from types import SimpleNamespace

import pytest

from agent.codex_responses_adapter import (
    _chat_messages_to_responses_input,
    _normalize_codex_response,
)
from agent.transports.chat_completions import ChatCompletionsTransport


def test_compacted_output_accepts_typed_user_messages_before_boundary():
    route = {
        "issuer_kind": "openai",
        "endpoint": "https://chatgpt.com/backend-api/codex",
        "model": "gpt-5.6-sol",
    }
    sidecar = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "remember this"}],
            "_issuer_kind": "openai",
            "_compaction_route": route,
        },
        {
            "type": "compaction",
            "encrypted_content": "opaque",
            "_issuer_kind": "openai",
            "_compaction_route": route,
        },
    ]
    items = _chat_messages_to_responses_input(
        [{"role": "assistant", "content": "answer", "codex_output_items": sidecar}],
        current_issuer_kind="openai",
        current_compaction_route=route,
    )
    assert [item["type"] for item in items] == ["message", "compaction"]
    assert items[0]["role"] == "user"
    assert items[0]["content"][0] == {"type": "input_text", "text": "remember this"}


def test_chat_completions_strips_native_compaction_sidecar():
    transport = ChatCompletionsTransport()
    kwargs = transport.build_kwargs(
        model="example",
        messages=[
            {
                "role": "assistant",
                "content": "answer",
                "codex_output_items": [
                    {"type": "compaction", "encrypted_content": "opaque"}
                ],
            }
        ],
        tools=[],
        max_tokens=100,
        reasoning_config={},
        request_overrides=None,
    )
    assert "codex_output_items" not in kwargs["messages"][0]


def test_compaction_only_response_continues_instead_of_finishing_empty():
    route = {
        "issuer_kind": "codex_backend",
        "endpoint": "https://chatgpt.com/backend-api/codex",
        "model": "gpt-5.6-sol",
    }
    response = SimpleNamespace(
        status="completed",
        incomplete_details=None,
        output_text=None,
        output=[
            SimpleNamespace(
                type="compaction",
                encrypted_content="opaque",
                id="cmp_1",
            )
        ],
    )
    message, finish_reason = _normalize_codex_response(
        response,
        issuer_kind="codex_backend",
        compaction_route=route,
    )
    assert finish_reason == "incomplete"
    assert message.content == ""
    assert message.codex_output_items[0]["type"] == "compaction"
    assert message.codex_output_items[0]["_compaction_route"] == route


@pytest.mark.parametrize(
    "current_route",
    [
        {
            "issuer_kind": "codex_backend",
            "endpoint": "https://chatgpt.com/backend-api/codex",
            "model": "gpt-5.6-mini",
        },
        {
            "issuer_kind": "codex_backend",
            "endpoint": "https://api.openai.com/v1",
            "model": "gpt-5.6-sol",
        },
    ],
)
def test_same_issuer_route_switch_drops_opaque_compaction_sidecar(current_route):
    minted_route = {
        "issuer_kind": "codex_backend",
        "endpoint": "https://chatgpt.com/backend-api/codex",
        "model": "gpt-5.6-sol",
    }

    items = _chat_messages_to_responses_input(
        [
            {"role": "user", "content": "canonical history"},
            {
                "role": "assistant",
                "content": "canonical answer",
                "codex_output_items": [
                    {
                        "type": "compaction",
                        "encrypted_content": "opaque",
                        "_issuer_kind": "codex_backend",
                        "_compaction_route": minted_route,
                    }
                ],
            },
        ],
        current_issuer_kind="codex_backend",
        current_compaction_route=current_route,
    )
    assert items == [
        {"role": "user", "content": "canonical history"},
        {"role": "assistant", "content": "canonical answer"},
    ]
