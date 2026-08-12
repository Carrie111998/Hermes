"""Host-owned provenance for built-in memory mutations.

The model can request the built-in memory tool, but the tool call itself is not
evidence that the user authorized a durable external-memory mutation. This
module classifies only the clean direct user turn that Hermes already owns at
turn setup, and deliberately fails closed for ambiguous language.
"""

from __future__ import annotations

import re
from typing import Any

from agent.skill_commands import extract_user_instruction_from_skill_message


EXPLICIT_REMEMBER = "explicit_remember"
EXPLICIT_UPDATE = "explicit_update"
EXPLICIT_FORGET = "explicit_forget"
NONE = "none"

_EXPLICIT_INTENTS = frozenset({EXPLICIT_REMEMBER, EXPLICIT_UPDATE, EXPLICIT_FORGET})
_LEAD = r"(?:please\s+|kindly\s+|could\s+you\s+(?:please\s+)?|would\s+you\s+(?:please\s+)?)?"
_NEGATED_REMEMBER = re.compile(r"^\s*(?:please\s+)?(?:do not|don't|never)\s+remember\b", re.IGNORECASE)
_REMEMBER = re.compile(
    rf"^\s*{_LEAD}(?:remember|don't\s+forget)\b",
    re.IGNORECASE,
)
_SAVE = re.compile(
    rf"^\s*{_LEAD}(?:save|store|keep)\b.*\b(?:memory|future\s+sessions?)\b",
    re.IGNORECASE | re.DOTALL,
)
_UPDATE = re.compile(
    rf"^\s*{_LEAD}(?:update|correct|change|replace)\b.*\b(?:remember|memory|profile|what\s+you\s+know)\b",
    re.IGNORECASE | re.DOTALL,
)
_FORGET = re.compile(
    rf"^\s*{_LEAD}(?:forget|remove|delete)\b",
    re.IGNORECASE,
)


def _text_only(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {"text", "input_text", "output_text"}:
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(parts)
    return ""


def classify_user_memory_intent(content: Any, *, synthetic: bool = False) -> str:
    """Return the trusted intent expressed by a clean direct user turn.

    The classifier is intentionally prefix-based. Explanations, quotations,
    external-note summaries, and negated remember requests do not begin with
    an actionable direct-memory imperative and therefore remain ``none``.
    """
    if synthetic:
        return NONE
    text = _text_only(content)
    extracted = extract_user_instruction_from_skill_message(text)
    if extracted is None:
        return NONE
    text = " ".join(extracted.strip().split())
    if not text or _NEGATED_REMEMBER.match(text):
        return NONE
    if _UPDATE.match(text):
        return EXPLICIT_UPDATE
    if _FORGET.match(text):
        return EXPLICIT_FORGET
    if _REMEMBER.match(text) or _SAVE.match(text):
        return EXPLICIT_REMEMBER
    return NONE


def is_host_confirmed_user_memory(
    intent: str,
    *,
    write_origin: str,
    execution_context: str,
    synthetic: bool,
) -> bool:
    """Return whether host metadata may grant user authority.

    Normal foreground memory tool calls retain ``assistant_tool`` as their
    mechanical origin. All other origins/contexts fail closed, including the
    legacy ``write_origin=user`` value and background review.
    """
    return (
        intent in _EXPLICIT_INTENTS
        and write_origin == "assistant_tool"
        and execution_context == "foreground"
        and not synthetic
    )


__all__ = [
    "EXPLICIT_FORGET",
    "EXPLICIT_REMEMBER",
    "EXPLICIT_UPDATE",
    "NONE",
    "classify_user_memory_intent",
    "is_host_confirmed_user_memory",
]
