"""Canonical model-visible tool-result egress sanitation.

The executor and message-construction paths share this bounded owner while
``agent.tool_executor`` is fractured under #79975. Legacy private imports remain
available through compatibility aliases in ``agent.tool_dispatch_helpers``.
"""

from __future__ import annotations

from typing import Any

from agent.redact import redact_sensitive_text


def is_multimodal_tool_result(value: Any) -> bool:
    """Return whether ``value`` is a supported multimodal result envelope."""
    return (
        isinstance(value, dict)
        and value.get("_multimodal") is True
        and isinstance(value.get("content"), list)
    )


def redact_tool_result_for_egress(value: Any) -> Any:
    """Redact every model-visible text carrier without altering media bytes.

    Plain strings receive the configured redaction policy plus strict URL
    userinfo/query masking. Text parts are copied before mutation; non-text
    image/base64 parts are retained by identity. ``redact_sensitive_text`` owns
    the global opt-out, so this function deliberately does not force redaction.
    """

    def redact_text(text: str) -> str:
        return redact_sensitive_text(text, redact_url_credentials=True)

    def redact_parts(parts: list[Any]) -> list[Any]:
        redacted_parts: list[Any] = []
        for part in parts:
            if isinstance(part, str):
                redacted_parts.append(redact_text(part))
                continue
            if not isinstance(part, dict) or part.get("type") not in {
                "text",
                "input_text",
                "output_text",
            }:
                redacted_parts.append(part)
                continue
            redacted_part = dict(part)
            for field in ("text", "content"):
                field_value = redacted_part.get(field)
                if isinstance(field_value, str):
                    redacted_part[field] = redact_text(field_value)
            redacted_parts.append(redacted_part)
        return redacted_parts

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return redact_parts(value)
    if is_multimodal_tool_result(value):
        redacted_value = dict(value)
        redacted_value["content"] = redact_parts(value.get("content") or [])
        if isinstance(value.get("text_summary"), str):
            redacted_value["text_summary"] = redact_text(value["text_summary"])
        return redacted_value
    return value
