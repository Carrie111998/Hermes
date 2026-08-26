"""The Gemini provider — request shape, tool loop, and blocked responses.

Gemini differs from the other two in ways that break silently if they are
wrong: the system prompt is its own field, roles are user/model, a tool
result must be an object and must directly follow the model's own
functionCall, and a blocked prompt arrives as HTTP 200 with no candidate.
"""

from __future__ import annotations

import json

import httpx
import pytest

from kakao_legal_bot.app.config import Settings
from kakao_legal_bot.app.llm import LlmClient, LlmError, ToolSpec


def text_turn(text: str) -> dict:
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}
        ]
    }


def call_turn(name: str, args: dict) -> dict:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}}]},
                "finishReason": "STOP",
            }
        ]
    }


def make_tool(name: str, calls: list, output: str = "도구 결과") -> ToolSpec:
    async def handler(arguments: dict) -> str:
        calls.append(arguments)
        return output

    return ToolSpec(
        name=name,
        description="테스트 도구",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "검색어"}},
            "required": ["query"],
        },
        handler=handler,
    )


def client_for(responses: list[dict], record: list | None = None, urls: list | None = None):
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(json.loads(request.content))
        if urls is not None:
            urls.append((str(request.url), dict(request.headers)))
        return httpx.Response(200, json=queue.pop(0) if queue else responses[-1])

    return LlmClient(
        provider="gemini",
        api_key="test-key",
        base_url="https://gemini.test/v1beta",
        model="gemini-3.7-flash",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# ── request shape ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_endpoint_and_auth_header():
    urls: list = []
    llm = client_for([text_turn("답변")], urls=urls)
    await llm.complete("시스템", [{"role": "user", "content": "질문"}])
    await llm.aclose()

    url, headers = urls[0]
    assert url == "https://gemini.test/v1beta/models/gemini-3.7-flash:generateContent"
    assert headers["x-goog-api-key"] == "test-key"


@pytest.mark.asyncio
async def test_system_prompt_is_its_own_field_not_a_message():
    sent: list = []
    llm = client_for([text_turn("답변")], record=sent)
    await llm.complete("너는 모아다", [{"role": "user", "content": "질문"}])
    await llm.aclose()

    assert sent[0]["systemInstruction"]["parts"][0]["text"] == "너는 모아다"
    assert [c["role"] for c in sent[0]["contents"]] == ["user"]


@pytest.mark.asyncio
async def test_assistant_history_becomes_the_model_role():
    sent: list = []
    llm = client_for([text_turn("답변")], record=sent)
    await llm.complete(
        "시스템",
        [
            {"role": "user", "content": "[지금까지의 대화 기록]\n홍길동: 전세금"},
            {"role": "assistant", "content": "대화 기록을 확인했습니다."},
            {"role": "user", "content": "그래서 어떻게 하죠?"},
        ],
    )
    await llm.aclose()

    contents = sent[0]["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert contents[2]["parts"][0]["text"] == "그래서 어떻게 하죠?"


@pytest.mark.asyncio
async def test_generation_config_carries_the_limits():
    sent: list = []
    llm = client_for([text_turn("답변")], record=sent)
    llm.temperature = 0.2
    await llm.complete("시스템", [{"role": "user", "content": "질문"}], max_tokens=1234)
    await llm.aclose()

    assert sent[0]["generationConfig"] == {"temperature": 0.2, "maxOutputTokens": 1234}


# ── tools ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_tools_are_declared_in_gemini_shape():
    sent: list = []
    llm = client_for([text_turn("답변")], record=sent)
    await llm.complete("시스템", [{"role": "user", "content": "질문"}], [make_tool("search_law", [])])
    await llm.aclose()

    declarations = sent[0]["tools"][0]["functionDeclarations"]
    assert declarations[0]["name"] == "search_law"
    assert declarations[0]["parameters"]["properties"]["query"]["type"] == "string"


@pytest.mark.asyncio
async def test_tool_result_is_an_object_and_follows_the_model_turn():
    calls: list = []
    sent: list = []
    llm = client_for(
        [call_turn("search_law", {"query": "주택임대차보호법"}), text_turn("제618조에 따르면…")],
        record=sent,
    )
    result = await llm.complete(
        "시스템", [{"role": "user", "content": "질문"}], [make_tool("search_law", calls)]
    )
    await llm.aclose()

    assert calls == [{"query": "주택임대차보호법"}]
    assert result.text == "제618조에 따르면…"
    assert result.tools_used == ["search_law"]

    contents = sent[1]["contents"]
    # The model's own functionCall turn, echoed back verbatim…
    assert contents[-2]["role"] == "model"
    assert contents[-2]["parts"][0]["functionCall"]["name"] == "search_law"
    # …immediately followed by the result, as an object.
    assert contents[-1]["role"] == "user"
    response = contents[-1]["parts"][0]["functionResponse"]
    assert response["name"] == "search_law"
    assert response["response"] == {"result": "도구 결과"}


@pytest.mark.asyncio
async def test_a_tool_with_no_parameters_omits_the_field():
    """Gemini rejects an empty `properties` object outright."""
    sent: list = []

    async def handler(arguments: dict) -> str:
        return "ok"

    bare = ToolSpec(
        name="ping", description="", input_schema={"type": "object"}, handler=handler
    )
    llm = client_for([text_turn("답변")], record=sent)
    await llm.complete("시스템", [{"role": "user", "content": "질문"}], [bare])
    await llm.aclose()

    assert "parameters" not in sent[0]["tools"][0]["functionDeclarations"][0]


@pytest.mark.asyncio
async def test_unknown_schema_keys_are_stripped():
    sent: list = []

    async def handler(arguments: dict) -> str:
        return "ok"

    tool = ToolSpec(
        name="t",
        description="",
        input_schema={
            "type": "object",
            "additionalProperties": False,  # not part of Gemini's subset
            "properties": {"n": {"type": "integer", "minimum": 1, "description": "수"}},
        },
        handler=handler,
    )
    llm = client_for([text_turn("답변")], record=sent)
    await llm.complete("시스템", [{"role": "user", "content": "질문"}], [tool])
    await llm.aclose()

    params = sent[0]["tools"][0]["functionDeclarations"][0]["parameters"]
    assert "additionalProperties" not in params
    assert "minimum" not in params["properties"]["n"]
    assert params["properties"]["n"]["description"] == "수"


@pytest.mark.asyncio
async def test_tool_rounds_are_capped_and_the_last_call_drops_tools():
    calls: list = []
    sent: list = []
    llm = client_for(
        [call_turn("search_law", {"query": "민법"})] * 3 + [text_turn("최종 답변")], record=sent
    )
    llm.max_tool_rounds = 3
    result = await llm.complete(
        "시스템", [{"role": "user", "content": "질문"}], [make_tool("search_law", calls)]
    )
    await llm.aclose()

    assert len(calls) == 3
    assert "tools" not in sent[-1]
    assert result.text == "최종 답변"


# ── failure modes ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_blocked_prompt_is_an_error_not_an_empty_answer():
    """No candidate at all, HTTP 200 — must not read as a silent success."""
    llm = client_for([{"promptFeedback": {"blockReason": "SAFETY"}}])
    with pytest.raises(LlmError, match="차단"):
        await llm.complete("시스템", [{"role": "user", "content": "질문"}])
    await llm.aclose()


@pytest.mark.asyncio
async def test_a_safety_stop_with_no_text_is_an_error():
    llm = client_for(
        [{"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "SAFETY"}]}]
    )
    with pytest.raises(LlmError, match="차단"):
        await llm.complete("시스템", [{"role": "user", "content": "질문"}])
    await llm.aclose()


@pytest.mark.asyncio
async def test_http_error_becomes_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="quota exceeded")

    llm = LlmClient(
        provider="gemini",
        api_key="k",
        base_url="https://gemini.test/v1beta",
        model="gemini-3.7-flash",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(LlmError, match="429"):
        await llm.complete("시스템", [{"role": "user", "content": "질문"}])
    await llm.aclose()


@pytest.mark.asyncio
async def test_missing_key_is_caught_before_the_request():
    llm = LlmClient(
        provider="gemini", api_key="", base_url="https://gemini.test/v1beta", model="m"
    )
    with pytest.raises(LlmError, match="GEMINI_API_KEY"):
        await llm.complete("시스템", [{"role": "user", "content": "질문"}])
    await llm.aclose()


# ── settings wiring ──────────────────────────────────────────────────────
def test_gemini_provider_picks_its_own_key_and_default_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    settings = Settings()

    assert settings.llm_model == "gemini-3.7-flash"
    assert settings.llm_credentials == (
        "g-key",
        "https://generativelanguage.googleapis.com/v1beta",
    )
    # Iris config is unrelated and still unset here — only the LLM half matters.
    missing = settings.missing_required()
    assert "GEMINI_API_KEY" not in missing
    assert "LLM_MODEL" not in missing


def test_an_explicit_model_still_wins(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("LLM_MODEL", "gemini-3.6-flash")
    assert Settings().llm_model == "gemini-3.6-flash"


def test_a_missing_gemini_key_is_reported(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert "GEMINI_API_KEY" in Settings().missing_required()


def test_anthropic_stays_the_default(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    settings = Settings()
    assert settings.llm_provider == "anthropic"
    assert settings.llm_model == "claude-sonnet-5"


def test_an_unknown_provider_is_reported(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bard")
    assert any("LLM_PROVIDER" in item for item in Settings().missing_required())


def test_drafts_can_run_on_a_different_model(monkeypatch):
    """Cheap Gemini for chat, something stronger for documents."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_DRAFT_MODEL", "gemini-3.7-pro")
    settings = Settings()
    assert settings.llm_model == "gemini-3.7-flash"
    assert settings.draft_model == "gemini-3.7-pro"
