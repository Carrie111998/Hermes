import hashlib
import json
from types import SimpleNamespace

import pytest

from agent.terminal_tool_result import (
    TerminalToolResultError,
    output_schema_declares_terminal_verbatim,
    parse_terminal_tool_result,
)
from tools.mcp_tool import _mcp_tool_returns_terminal_result
from tools.registry import registry


def _payload(answer: str) -> dict:
    return {
        "delivery_semantics": "terminal_verbatim",
        "answer": answer,
        "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
    }


def test_output_schema_requires_explicit_terminal_contract():
    schema = {
        "type": "object",
        "properties": {
            "delivery_semantics": {
                "type": "string",
                "const": "terminal_verbatim",
            },
            "answer": {"type": "string"},
            "answer_sha256": {"type": "string"},
        },
        "required": ["delivery_semantics", "answer", "answer_sha256"],
    }

    assert output_schema_declares_terminal_verbatim(schema) is True
    assert _mcp_tool_returns_terminal_result(
        SimpleNamespace(outputSchema=schema)
    ) is True
    assert output_schema_declares_terminal_verbatim(
        {
            **schema,
            "properties": {
                **schema["properties"],
                "delivery_semantics": {"type": "string"},
            },
        }
    ) is False


def test_parse_terminal_result_prefers_structured_content():
    expected = _payload("Exact participant answer.")
    raw = json.dumps(
        {
            "result": "model-oriented duplicate",
            "structuredContent": expected,
        }
    )

    parsed = parse_terminal_tool_result(
        raw,
        tool_name="mcp__knowledge__search",
        tool_call_id="call-1",
    )

    assert parsed.answer == expected["answer"]
    assert parsed.answer_sha256 == expected["answer_sha256"]


def test_parse_terminal_result_rejects_digest_mismatch():
    invalid = {
        **_payload("Exact participant answer."),
        "answer_sha256": "0" * 64,
    }

    with pytest.raises(
        TerminalToolResultError,
        match="does not match",
    ):
        parse_terminal_tool_result(
            json.dumps({"result": invalid}),
            tool_name="mcp__knowledge__search",
            tool_call_id="call-1",
        )


def test_registry_retains_terminal_result_capability():
    name = "test_terminal_result_capability"
    registry.register(
        name=name,
        toolset="test-terminal-result",
        schema={
            "name": name,
            "description": "test",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda _args: "{}",
        terminal_result=True,
    )
    try:
        assert registry.is_terminal_result_tool(name) is True
    finally:
        registry.deregister(name)
