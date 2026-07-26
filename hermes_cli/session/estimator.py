"""Fast deterministic input-token estimates for rotation decisions."""

from __future__ import annotations

from typing import Any


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def estimate_next_turn_input_tokens(
    system_prompt: str,
    conversation_history: list,
    pending_user_message: Any,
) -> int:
    total = estimate_tokens(str(system_prompt or ""))
    for message in conversation_history or []:
        content = (
            message.get("content", "")
            if isinstance(message, dict)
            else str(message)
        )
        total += estimate_tokens(str(content or ""))
        total += 10
    total += estimate_tokens(str(pending_user_message or ""))
    return total


__all__ = ["estimate_next_turn_input_tokens", "estimate_tokens"]
