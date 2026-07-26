"""Validated terminal tool-result contracts.

A terminal tool returns the participant-facing answer itself, so the agent
loop must deliver that answer without asking the model to synthesize it again.
Only tools whose declared output schema opts into this behavior are eligible.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any


_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class TerminalToolResult:
    answer: str
    answer_sha256: str
    tool_name: str
    tool_call_id: str


class TerminalToolResultError(ValueError):
    """A terminal-capable tool returned an invalid terminal envelope."""


def output_schema_declares_terminal_verbatim(output_schema: Any) -> bool:
    """Return whether an MCP output schema explicitly opts into termination."""
    if not isinstance(output_schema, dict) or output_schema.get("type") != "object":
        return False
    properties = output_schema.get("properties")
    required = output_schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        return False
    if not {"delivery_semantics", "answer", "answer_sha256"}.issubset(required):
        return False

    delivery = properties.get("delivery_semantics")
    answer = properties.get("answer")
    digest = properties.get("answer_sha256")
    return (
        isinstance(delivery, dict)
        and delivery.get("const") == "terminal_verbatim"
        and isinstance(answer, dict)
        and answer.get("type") == "string"
        and isinstance(digest, dict)
        and digest.get("type") == "string"
    )


def _structured_payload(raw_result: Any) -> dict[str, Any]:
    if not isinstance(raw_result, str):
        raise TerminalToolResultError("result must be a JSON string")
    try:
        outer = json.loads(raw_result)
    except json.JSONDecodeError as exc:
        raise TerminalToolResultError("result is not valid JSON") from exc
    if not isinstance(outer, dict):
        raise TerminalToolResultError("result must decode to an object")

    structured = outer.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    result = outer.get("result")
    if isinstance(result, dict):
        return result
    raise TerminalToolResultError(
        "result does not contain structuredContent or an object result"
    )


def parse_terminal_tool_result(
    raw_result: Any,
    *,
    tool_name: str,
    tool_call_id: str,
) -> TerminalToolResult:
    """Parse and verify the generic ``terminal_verbatim`` envelope."""
    payload = _structured_payload(raw_result)
    if payload.get("delivery_semantics") != "terminal_verbatim":
        raise TerminalToolResultError(
            "delivery_semantics must be terminal_verbatim"
        )

    answer = payload.get("answer")
    if not isinstance(answer, str) or not answer:
        raise TerminalToolResultError("answer must be a non-empty string")

    answer_sha256 = payload.get("answer_sha256")
    if not isinstance(answer_sha256, str) or not _SHA256_RE.fullmatch(
        answer_sha256
    ):
        raise TerminalToolResultError(
            "answer_sha256 must be a lowercase SHA-256 digest"
        )
    actual_digest = hashlib.sha256(answer.encode("utf-8")).hexdigest()
    if actual_digest != answer_sha256:
        raise TerminalToolResultError("answer_sha256 does not match answer")

    return TerminalToolResult(
        answer=answer,
        answer_sha256=answer_sha256,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
    )


def capture_terminal_tool_result(
    agent: Any,
    *,
    tool_name: str,
    tool_call_id: str,
    raw_result: Any,
) -> None:
    """Capture one validated terminal result on the current agent turn."""
    from tools.registry import registry

    if not registry.is_terminal_result_tool(tool_name):
        return

    try:
        parsed = parse_terminal_tool_result(
            raw_result,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )
    except TerminalToolResultError as exc:
        agent._terminal_tool_result_error = (
            f"Terminal tool {tool_name!r} returned an invalid terminal result: {exc}"
        )
        return

    existing = getattr(agent, "_terminal_tool_result", None)
    if existing is not None:
        agent._terminal_tool_result_error = (
            "A single assistant turn returned more than one terminal tool result"
        )
        return
    agent._terminal_tool_result = parsed
