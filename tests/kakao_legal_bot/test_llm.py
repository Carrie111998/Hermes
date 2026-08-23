"""The tool-use loop, against a mocked Anthropic/OpenAI endpoint."""

from __future__ import annotations

import json

import httpx
import pytest

from kakao_legal_bot.app.llm import LlmClient, LlmError, ToolSpec


def text_block(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


def tool_block(name: str, arguments: dict) -> dict:
    return {
        "content": [{"type": "tool_use", "id": "call-1", "name": name, "input": arguments}],
        "stop_reason": "tool_use",
    }


def make_tool(name: str, calls: list, output: str = "도구 결과") -> ToolSpec:
    async def handler(arguments: dict) -> str:
        calls.append(arguments)
        return output

    return ToolSpec(
        name=name,
        description="테스트 도구",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        handler=handler,
    )


def client_for(responses: list[dict], provider: str = "anthropic", record: list | None = None):
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(json.loads(request.content))
        return httpx.Response(200, json=queue.pop(0))

    return LlmClient(
        provider=provider,
        api_key="test-key",
        base_url="https://llm.test",
        model="test-model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_plain_answer_needs_no_tools():
    llm = client_for([text_block("안녕하세요")])
    result = await llm.complete("system", [{"role": "user", "content": "안녕"}])
    await llm.aclose()
    assert result.text == "안녕하세요"
    assert result.tools_used == []


@pytest.mark.asyncio
async def test_tool_result_is_fed_back_and_the_answer_returned():
    calls: list = []
    sent: list = []
    llm = client_for(
        [tool_block("search_law", {"query": "민법"}), text_block("민법 제618조에 따르면…")],
        record=sent,
    )
    result = await llm.complete(
        "system", [{"role": "user", "content": "임대차 조문"}], [make_tool("search_law", calls)]
    )
    await llm.aclose()

    assert calls == [{"query": "민법"}]
    assert result.tools_used == ["search_law"]
    assert result.text == "민법 제618조에 따르면…"

    # Second request must carry the assistant turn and the tool_result.
    follow_up = sent[1]["messages"]
    assert follow_up[-2]["role"] == "assistant"
    assert follow_up[-1]["content"][0]["type"] == "tool_result"
    assert follow_up[-1]["content"][0]["content"] == "도구 결과"


@pytest.mark.asyncio
async def test_a_failing_tool_does_not_kill_the_turn():
    async def explode(arguments: dict) -> str:
        raise RuntimeError("법령 API 다운")

    tool = ToolSpec(
        name="search_law", description="", input_schema={"type": "object"}, handler=explode
    )
    sent: list = []
    llm = client_for(
        [tool_block("search_law", {"query": "민법"}), text_block("조문은 확인하지 못했습니다.")],
        record=sent,
    )
    result = await llm.complete("system", [{"role": "user", "content": "질문"}], [tool])
    await llm.aclose()

    assert "확인하지 못했습니다" in result.text
    assert "법령 API 다운" in sent[1]["messages"][-1]["content"][0]["content"]


@pytest.mark.asyncio
async def test_unknown_tool_name_is_reported_to_the_model():
    sent: list = []
    llm = client_for([tool_block("nope", {}), text_block("답변")], record=sent)
    result = await llm.complete("system", [{"role": "user", "content": "질문"}], [])
    await llm.aclose()
    assert result.text == "답변"
    assert "알 수 없는 도구" in sent[1]["messages"][-1]["content"][0]["content"]


@pytest.mark.asyncio
async def test_tool_rounds_are_capped():
    calls: list = []
    # The model would loop forever; the cap stops it and the last round
    # is sent without tools so the model has to answer in prose.
    responses = [tool_block("search_law", {"query": "민법"})] * 3 + [text_block("최종 답변")]
    sent: list = []
    llm = client_for(responses, record=sent)
    llm.max_tool_rounds = 3
    result = await llm.complete(
        "system", [{"role": "user", "content": "질문"}], [make_tool("search_law", calls)]
    )
    await llm.aclose()

    assert len(calls) == 3
    assert "tools" not in sent[-1]
    assert result.text == "최종 답변"


@pytest.mark.asyncio
async def test_http_error_becomes_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    llm = LlmClient(
        provider="anthropic",
        api_key="k",
        base_url="https://llm.test",
        model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(LlmError, match="429"):
        await llm.complete("system", [{"role": "user", "content": "질문"}])
    await llm.aclose()


@pytest.mark.asyncio
async def test_missing_api_key_is_caught_before_the_request():
    llm = LlmClient(provider="anthropic", api_key="", base_url="https://llm.test", model="m")
    with pytest.raises(LlmError, match="ANTHROPIC_API_KEY"):
        await llm.complete("system", [{"role": "user", "content": "질문"}])
    await llm.aclose()


@pytest.mark.asyncio
async def test_openai_provider_speaks_openai():
    sent: list = []
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "search_law", "arguments": '{"query": "민법"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": [{"message": {"role": "assistant", "content": "답변"}, "finish_reason": "stop"}]},
    ]
    calls: list = []
    llm = client_for(responses, provider="openai", record=sent)
    result = await llm.complete(
        "system", [{"role": "user", "content": "질문"}], [make_tool("search_law", calls)]
    )
    await llm.aclose()

    assert calls == [{"query": "민법"}]
    assert result.text == "답변"
    assert sent[0]["messages"][0]["role"] == "system"
    assert sent[1]["messages"][-1]["role"] == "tool"
