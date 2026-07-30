"""Strict decision protocol for the subprocess-backed Claude CLI provider."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


class ClaudeCLIProtocolError(ValueError):
    """Claude returned a decision Hermes cannot safely execute."""


DECISION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"enum": ["final", "tool_calls"]},
        "text": {"type": "string", "minLength": 1},
        "calls": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "name", "arguments"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "name": {"type": "string", "minLength": 1},
                    "arguments": {"type": "object"},
                },
            },
        },
    },
    "required": ["kind"],
    "oneOf": [
        {
            "properties": {"kind": {"const": "final"}},
            "required": ["text"],
            "not": {"required": ["calls"]},
        },
        {
            "properties": {"kind": {"const": "tool_calls"}},
            "required": ["calls"],
            "not": {"required": ["text"]},
        },
    ],
}


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def decision_schema_json() -> str:
    """Return the schema passed to Claude's ``--json-schema`` flag."""

    # Claude Code converts this schema to a custom tool input schema. That
    # surface rejects the Draft 2020-12 URI and top-level combinators. Keep
    # those stricter constraints for Hermes's local validator, which remains
    # authoritative before any decision reaches the tool loop.
    cli_schema = {
        key: value
        for key, value in DECISION_SCHEMA.items()
        if key not in {"$schema", "oneOf", "allOf", "anyOf"}
    }
    return _compact_json(cli_schema)


def _tool_schemas(tools: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, Mapping) else None
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        parameters = function.get("parameters")
        if isinstance(name, str) and name and isinstance(parameters, Mapping):
            schemas[name] = dict(parameters)
    return schemas


def parse_decision(
    payload: str | Mapping[str, Any],
    *,
    tools: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Parse and fully validate one Claude decision envelope."""

    if isinstance(payload, str):
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ClaudeCLIProtocolError("Claude returned malformed decision JSON") from exc
    elif isinstance(payload, Mapping):
        value = dict(payload)
    else:
        raise ClaudeCLIProtocolError("Claude decision must be a JSON object")

    try:
        Draft202012Validator(DECISION_SCHEMA).validate(value)
    except ValidationError as exc:
        raise ClaudeCLIProtocolError(
            f"Claude decision violates the envelope schema: {exc.message}"
        ) from exc

    if value["kind"] == "final":
        return value

    schemas = _tool_schemas(tools)
    seen_ids: set[str] = set()
    for call in value["calls"]:
        call_id = call["id"]
        if call_id in seen_ids:
            raise ClaudeCLIProtocolError(f"Duplicate Claude tool call id: {call_id}")
        seen_ids.add(call_id)

        name = call["name"]
        schema = schemas.get(name)
        if schema is None:
            raise ClaudeCLIProtocolError(f"Claude requested unavailable tool: {name}")
        try:
            Draft202012Validator(schema).validate(call["arguments"])
        except ValidationError as exc:
            raise ClaudeCLIProtocolError(
                f"Claude arguments for tool {name!r} are invalid: {exc.message}"
            ) from exc
    return value


def _protocol_instruction() -> str:
    return (
        "You are the decision model inside Hermes. Hermes owns all tools and "
        "side effects. Return exactly one object matching the supplied JSON "
        "schema. Use kind=final for user-visible text or kind=tool_calls to "
        "request only tools listed in this frame. Never claim a tool ran."
    )


def build_bootstrap_prompt(
    *,
    messages: Iterable[Mapping[str, Any]],
    tools: Iterable[Mapping[str, Any]],
) -> str:
    """Build the deterministic first request for a provider-native session."""

    return _compact_json(
        {
            "frame": "bootstrap",
            "instruction": _protocol_instruction(),
            "messages": list(messages),
            "tools": list(tools or []),
        }
    )


def build_resume_prompt(*, messages: Iterable[Mapping[str, Any]]) -> str:
    """Build a semantic delta for a resumed provider-native session."""

    return _compact_json(
        {
            "frame": "delta",
            "instruction": _protocol_instruction(),
            "messages": list(messages),
        }
    )


def to_chat_completion(
    decision: Mapping[str, Any],
    *,
    model: str,
    model_reported: str | None = None,
) -> SimpleNamespace:
    """Translate a validated decision to Hermes's completed response shape."""

    tool_calls = None
    content = None
    finish_reason = "stop"
    if decision["kind"] == "final":
        content = decision["text"]
    else:
        finish_reason = "tool_calls"
        tool_calls = [
            SimpleNamespace(
                id=call["id"],
                type="function",
                function=SimpleNamespace(
                    name=call["name"],
                    arguments=_compact_json(call["arguments"]),
                ),
            )
            for call in decision["calls"]
        ]

    return SimpleNamespace(
        id=None,
        choices=[
            SimpleNamespace(
                index=0,
                message=SimpleNamespace(
                    role="assistant",
                    content=content,
                    tool_calls=tool_calls,
                    reasoning_content=None,
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
        model=model_reported or model,
        provider_reported_model=model_reported,
    )
