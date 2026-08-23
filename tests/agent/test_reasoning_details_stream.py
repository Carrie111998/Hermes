"""Regression: OpenRouter reasoning_details must survive stream reassembly.

task-15963 / P0 H1. Details-only streams used to look empty and trip
conversation_loop empty-retry. Later content must still be kept.
"""
from __future__ import annotations

from types import SimpleNamespace

from agent.auxiliary_client import _ChatStreamAccumulator
from agent.chat_completion_helpers import coerce_stream_reasoning_details
from agent.transports.chat_completions import ChatCompletionsTransport


def _chunk(*, content=None, reasoning=None, reasoning_content=None, reasoning_details=None, finish=None, tool_calls=None):
    extra = {}
    if reasoning_details is not None:
        extra["reasoning_details"] = reasoning_details
    delta = SimpleNamespace(
        content=content,
        reasoning=reasoning,
        reasoning_content=reasoning_content,
        reasoning_details=reasoning_details,
        tool_calls=tool_calls or [],
        model_extra=extra,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish)
    return SimpleNamespace(id="c1", model="openrouter/z-ai/glm-4.7", choices=[choice], usage=None)


def test_coerce_reads_model_extra_when_attr_missing():
    delta = SimpleNamespace(
        content=None,
        model_extra={"reasoning_details": [{"type": "reasoning.summary", "summary": "think"}]},
    )
    got = coerce_stream_reasoning_details(delta)
    assert got == [{"type": "reasoning.summary", "summary": "think"}]


def test_accumulator_details_then_content_keeps_content():
    acc = _ChatStreamAccumulator(model="glm-4.7")
    acc.feed(_chunk(reasoning_details=[{"type": "reasoning.summary", "summary": "think first"}]))
    acc.feed(_chunk(content="OK"))
    acc.feed(_chunk(finish="stop"))
    msg = acc.finish().choices[0].message
    assert msg.content == "OK"
    assert msg.reasoning_details == [{"type": "reasoning.summary", "summary": "think first"}]


def test_accumulator_details_only_is_structured_not_empty():
    acc = _ChatStreamAccumulator(model="glm-4.7")
    acc.feed(_chunk(reasoning_details=[{"type": "reasoning.summary", "summary": "only think"}]))
    acc.feed(_chunk(finish="stop"))
    msg = acc.finish().choices[0].message
    assert msg.content == ""
    assert msg.reasoning is None
    assert msg.reasoning_details == [{"type": "reasoning.summary", "summary": "only think"}]
    nr = ChatCompletionsTransport().normalize_response(
        SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="stop")],
            usage=None,
        )
    )
    assert nr.reasoning_details == [{"type": "reasoning.summary", "summary": "only think"}]
    assert bool(nr.reasoning_details)
    assert not (nr.content or "").strip()


def test_accumulator_reasoning_content_only_still_works():
    acc = _ChatStreamAccumulator(model="glm-4.7")
    acc.feed(_chunk(reasoning_content="structured think only"))
    acc.feed(_chunk(finish="stop"))
    msg = acc.finish().choices[0].message
    assert msg.reasoning == "structured think only"
    assert msg.reasoning_details is None


def test_details_not_promoted_to_content():
    acc = _ChatStreamAccumulator(model="glm-4.7")
    acc.feed(_chunk(reasoning_details=[{"type": "reasoning.summary", "summary": "secret cot"}]))
    acc.feed(_chunk(finish="stop"))
    msg = acc.finish().choices[0].message
    assert "secret cot" not in (msg.content or "")


def test_details_then_tool_call_keeps_tools_and_details():
    acc = _ChatStreamAccumulator(model="glm-4.7")
    acc.feed(_chunk(reasoning_details=[{"type": "reasoning.summary", "summary": "plan tool"}]))
    tc = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="terminal", arguments='{"command":"true"}'),
    )
    acc.feed(_chunk(tool_calls=[tc]))
    acc.feed(_chunk(finish="tool_calls"))
    msg = acc.finish().choices[0].message
    assert msg.reasoning_details == [{"type": "reasoning.summary", "summary": "plan tool"}]
    assert msg.tool_calls is not None
    assert msg.tool_calls[0].id == "call_1"
    assert msg.tool_calls[0].function.name == "terminal"
    assert "plan tool" not in (msg.content or "")


def test_partial_stub_preserves_details():
    from agent.chat_completion_helpers import _build_partial_stream_stub
    stub = _build_partial_stream_stub(
        "assistant",
        None,
        None,
        "glm-4.7",
        None,
        reasoning_details=[{"type": "reasoning.summary", "summary": "mid drop"}],
    )
    assert stub.choices[0].message.reasoning_details == [
        {"type": "reasoning.summary", "summary": "mid drop"}
    ]
    assert not (stub.choices[0].message.content or "")
