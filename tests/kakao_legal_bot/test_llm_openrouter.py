"""OpenRouter — the OpenAI wire format with two differences that matter.

It reads `max_tokens` and ignores `max_completion_tokens`; sending the wrong
one leaves the output length unbounded, which is a bill rather than an error.
"""

from __future__ import annotations

import json

import httpx
import pytest

from kakao_legal_bot.app.config import Settings
from kakao_legal_bot.app.llm import LlmClient, ToolSpec


def message_turn(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}


def call_turn(name: str, arguments: dict) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "c1", "function": {"name": name, "arguments": json.dumps(arguments)}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def client_for(responses: list[dict], record: list | None = None, headers: list | None = None):
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(json.loads(request.content))
        if headers is not None:
            headers.append((str(request.url), dict(request.headers)))
        return httpx.Response(200, json=queue.pop(0) if queue else responses[-1])

    return LlmClient(
        provider="openrouter",
        api_key="or-key",
        base_url="https://openrouter.test/api/v1",
        model="google/gemini-3.7-flash",
        extra_headers={"X-Title": "moa-legal-bot"},
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_output_length_is_actually_capped():
    """max_completion_tokens is silently ignored by OpenRouter — use max_tokens."""
    sent: list = []
    llm = client_for([message_turn("답변")], record=sent)
    await llm.complete("시스템", [{"role": "user", "content": "질문"}], max_tokens=1500)
    await llm.aclose()

    assert sent[0]["max_tokens"] == 1500
    assert "max_completion_tokens" not in sent[0]


@pytest.mark.asyncio
async def test_plain_openai_still_uses_max_completion_tokens():
    sent: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=message_turn("답변"))

    llm = LlmClient(
        provider="openai",
        api_key="k",
        base_url="https://api.openai.test/v1",
        model="gpt",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await llm.complete("시스템", [{"role": "user", "content": "질문"}], max_tokens=1500)
    await llm.aclose()

    assert sent[0]["max_completion_tokens"] == 1500
    assert "max_tokens" not in sent[0]


@pytest.mark.asyncio
async def test_endpoint_auth_and_attribution_headers():
    seen: list = []
    llm = client_for([message_turn("답변")], headers=seen)
    await llm.complete("시스템", [{"role": "user", "content": "질문"}])
    await llm.aclose()

    url, headers = seen[0]
    assert url == "https://openrouter.test/api/v1/chat/completions"
    assert headers["authorization"] == "Bearer or-key"
    assert headers["x-title"] == "moa-legal-bot"


@pytest.mark.asyncio
async def test_the_tool_loop_works_through_openrouter():
    calls: list = []
    sent: list = []

    async def handler(arguments: dict) -> str:
        calls.append(arguments)
        return "주택임대차보호법 …"

    tool = ToolSpec(
        name="search_law",
        description="법령 검색",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        handler=handler,
    )
    llm = client_for(
        [call_turn("search_law", {"query": "주택임대차"}), message_turn("제618조에 따르면…")],
        record=sent,
    )
    result = await llm.complete("시스템", [{"role": "user", "content": "질문"}], [tool])
    await llm.aclose()

    assert calls == [{"query": "주택임대차"}]
    assert result.text == "제618조에 따르면…"
    assert sent[1]["messages"][-1]["role"] == "tool"


def test_settings_wire_openrouter_end_to_end(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    settings = Settings()

    assert settings.llm_credentials == ("or-key", "https://openrouter.ai/api/v1")
    assert settings.llm_model == "google/gemini-3.7-flash"
    assert "OPENROUTER_API_KEY" not in settings.missing_required()


def test_a_missing_openrouter_key_is_reported(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert "OPENROUTER_API_KEY" in Settings().missing_required()


def test_a_pinned_slug_wins(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("LLM_MODEL", "google/gemini-3.7-flash-20260813")
    assert Settings().llm_model == "google/gemini-3.7-flash-20260813"
