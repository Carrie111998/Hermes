import json

import pytest

from agent.claude_cli_protocol import (
    ClaudeCLIProtocolError,
    build_bootstrap_prompt,
    build_resume_prompt,
    decision_schema_json,
    parse_decision,
    to_chat_completion,
)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Return text",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    }
]


def test_accepts_final_and_converts_to_completed_chat_response():
    decision = parse_decision({"kind": "final", "text": "done"}, tools=TOOLS)

    response = to_chat_completion(decision, model="opus")

    assert response.choices[0].message.content == "done"
    assert response.choices[0].message.tool_calls is None
    assert response.choices[0].finish_reason == "stop"


def test_accepts_parallel_tool_calls_and_serializes_literal_arguments():
    decision = parse_decision(
        {
            "kind": "tool_calls",
            "calls": [
                {"id": "call-a", "name": "echo", "arguments": {"text": "A"}},
                {"id": "call-b", "name": "echo", "arguments": {"text": "B"}},
            ],
        },
        tools=TOOLS,
    )

    response = to_chat_completion(decision, model="opus")
    calls = response.choices[0].message.tool_calls

    assert [(c.id, c.function.name, c.function.arguments) for c in calls] == [
        ("call-a", "echo", '{"text":"A"}'),
        ("call-b", "echo", '{"text":"B"}'),
    ]
    assert response.choices[0].finish_reason == "tool_calls"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"kind": "final", "text": ""},
        {"kind": "final", "text": "x", "calls": []},
        {"kind": "tool_calls", "calls": []},
        {
            "kind": "tool_calls",
            "calls": [
                {"id": "same", "name": "echo", "arguments": {"text": "A"}},
                {"id": "same", "name": "echo", "arguments": {"text": "B"}},
            ],
        },
        {
            "kind": "tool_calls",
            "calls": [{"id": "x", "name": "missing", "arguments": {}}],
        },
        {
            "kind": "tool_calls",
            "calls": [{"id": "x", "name": "echo", "arguments": {}}],
        },
    ],
)
def test_rejects_invalid_decisions(payload):
    with pytest.raises(ClaudeCLIProtocolError):
        parse_decision(payload, tools=TOOLS)


def test_rejects_malformed_json_and_unknown_fields():
    with pytest.raises(ClaudeCLIProtocolError):
        parse_decision("{not-json", tools=TOOLS)
    with pytest.raises(ClaudeCLIProtocolError):
        parse_decision(
            {"kind": "final", "text": "done", "unexpected": True},
            tools=TOOLS,
        )


def test_schema_forbids_unknown_fields():
    schema = json.loads(decision_schema_json())
    assert schema["additionalProperties"] is False
    assert "$schema" not in schema
    assert all(key not in schema for key in ("oneOf", "allOf", "anyOf"))


def test_bootstrap_and_resume_prompts_have_distinct_deterministic_frames():
    messages = [
        {"role": "system", "content": "Hermes system"},
        {"role": "user", "content": "hello"},
    ]

    first = build_bootstrap_prompt(messages=messages, tools=TOOLS)
    resumed = build_resume_prompt(
        messages=[{"role": "tool", "tool_call_id": "call-a", "content": "A"}]
    )

    assert '"frame":"bootstrap"' in first
    assert '"frame":"delta"' in resumed
    assert "Hermes system" in first
    assert "Hermes system" not in resumed
    assert "call-a" in resumed
