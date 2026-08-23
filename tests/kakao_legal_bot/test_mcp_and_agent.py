"""The MCP wrapper and the prompt the agent actually sends."""

from __future__ import annotations

import json

import httpx
import pytest

from kakao_legal_bot.app.agent import LegalAgent, build_messages, load_persona
from kakao_legal_bot.app.db import Message
from kakao_legal_bot.app.lawapi.client import LawApiClient
from kakao_legal_bot.app.llm import LlmClient
from kakao_legal_bot.mcp_law_server import handle

PREC_JSON = {
    "PrecSearch": {
        "prec": [
            {
                "판례일련번호": "228541",
                "사건명": "임대차보증금반환",
                "사건번호": "2018다255648",
                "선고일자": "20190314",
                "법원명": "대법원",
            }
        ]
    }
}


def law_client(payload: dict) -> LawApiClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps(payload, ensure_ascii=False))

    return LawApiClient(
        oc="oc", service_key="key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


# ── MCP ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_initialize_advertises_tools():
    client = law_client(PREC_JSON)
    response = await handle(client, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    await client.aclose()
    assert response["result"]["serverInfo"]["name"] == "korean-law"
    assert "tools" in response["result"]["capabilities"]


@pytest.mark.asyncio
async def test_tools_list_is_well_formed():
    client = law_client(PREC_JSON)
    response = await handle(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    await client.aclose()
    tools = response["result"]["tools"]
    assert {tool["name"] for tool in tools} >= {"search_law", "search_precedent", "get_precedent"}
    assert all("inputSchema" in tool for tool in tools)


@pytest.mark.asyncio
async def test_tools_call_returns_normalised_json():
    client = law_client(PREC_JSON)
    response = await handle(
        client,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "search_precedent", "arguments": {"query": "임대차"}},
        },
    )
    await client.aclose()
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload[0]["number"] == "2018다255648"
    assert "대법원" in payload[0]["citation"]


@pytest.mark.asyncio
async def test_notifications_get_no_reply():
    client = law_client(PREC_JSON)
    assert await handle(client, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    await client.aclose()


@pytest.mark.asyncio
async def test_unknown_method_is_a_jsonrpc_error():
    client = law_client(PREC_JSON)
    response = await handle(client, {"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
    await client.aclose()
    assert response["error"]["code"] == -32601


# ── prompt assembly ──────────────────────────────────────────────────────
def test_history_is_labelled_by_speaker():
    history = [
        Message(role="user", sender="홍길동", text="전세금을 못 받았어요", created_at=0.0),
        Message(role="bot", sender="모아", text="언제 계약이 끝났나요?", created_at=0.0),
        Message(role="lawyer", sender="김변호사", text="제가 확인해보겠습니다", created_at=0.0),
    ]
    messages = build_messages(history, "그래서 어떻게 하죠?", "모아")

    transcript = messages[0]["content"]
    assert "홍길동: 전세금을 못 받았어요" in transcript
    assert "모아: 언제 계약이 끝났나요?" in transcript
    assert "변호사: 제가 확인해보겠습니다" in transcript
    assert messages[-1]["content"] == "그래서 어떻게 하죠?"


def test_empty_history_sends_only_the_question():
    assert build_messages([], "질문", "모아") == [{"role": "user", "content": "질문"}]


def test_persona_falls_back_when_the_file_is_missing(tmp_path):
    persona = load_persona(tmp_path / "nope.md")
    assert "변호사" in persona


@pytest.mark.asyncio
async def test_agent_system_prompt_carries_persona_and_runtime_facts(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "답변"}], "stop_reason": "end_turn"}
        )

    llm = LlmClient(
        provider="anthropic",
        api_key="k",
        base_url="https://llm.test",
        model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    agent = LegalAgent(settings, llm)
    prompt = agent.system_prompt()
    assert "모아" in prompt
    assert "김변호사" in prompt
    assert str(settings.kakao_max_chars) in prompt

    result = await agent.answer("질문", [])
    await llm.aclose()
    assert result.text == "답변"
    assert result.error == ""


@pytest.mark.asyncio
async def test_agent_reports_an_llm_failure_instead_of_raising(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    llm = LlmClient(
        provider="anthropic",
        api_key="k",
        base_url="https://llm.test",
        model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await LegalAgent(settings, llm).answer("질문", [])
    await llm.aclose()
    assert result.text == ""
    assert "500" in result.error
