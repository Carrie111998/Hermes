"""Gateway agent-history / observed-context helpers extracted from gateway/run.py (#54962).

Byte-identical extraction — no behavior change. The Telegram observed-context
prompt marker and the observed-context / addressed-message header constants
move with their sole consumers.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


_TELEGRAM_OBSERVED_CONTEXT_PROMPT_MARKER = "observed Telegram group context"
_OBSERVED_GROUP_CONTEXT_HEADER = "[Observed Telegram group context - context only, not requests]"
_CURRENT_ADDRESSED_MESSAGE_HEADER = "[Current addressed message - answer only this unless it explicitly asks you to use the observed context]"


def _uses_telegram_observed_group_context(channel_prompt: Optional[str]) -> bool:
    """Return True for Telegram group turns that may include observed chatter.

    Telegram's observe-unmentioned mode persists skipped group chatter so a
    later @mention can see it. Those rows must not replay as ordinary user
    turns: a weak wake word like ``@bot cambio`` should not make the model treat
    old unmentioned chatter as pending work. The Telegram adapter marks these
    turns with a channel prompt; this helper keeps the run-path check explicit
    and unit-testable.
    """

    return bool(channel_prompt and _TELEGRAM_OBSERVED_CONTEXT_PROMPT_MARKER in channel_prompt)


def _select_cached_agent_history(
    persisted_history: List[Dict[str, Any]],
    live_history: Any,
) -> List[Dict[str, Any]]:
    """Prefer a cached agent's live in-memory transcript over a shorter
    persisted one.

    Guards the FTS write-corruption case (#50502): when message writes fail
    silently through corrupt FTS triggers, the next turn reloads a stale/empty
    ``conversation_history`` from disk even though the same cached ``AIAgent``
    still holds the full live ``_session_messages``. Replacing the live
    transcript with that shorter persisted copy causes immediate same-session
    amnesia. When the live transcript is strictly longer, keep it.

    Returns ``persisted_history`` unchanged unless the live copy is a longer
    list, in which case a copy of the live transcript is returned.
    """
    if isinstance(live_history, list) and len(live_history) > len(persisted_history):
        return list(live_history)
    return persisted_history


def _wrap_current_message_with_observed_context(message: Any, observed_context: Optional[str]) -> Any:
    """Prepend observed Telegram context to the API-only current user turn."""

    if not observed_context:
        return message

    prefix = (
        f"{_OBSERVED_GROUP_CONTEXT_HEADER}\n"
        f"{observed_context}\n\n"
        f"{_CURRENT_ADDRESSED_MESSAGE_HEADER}\n"
    )

    if isinstance(message, str):
        return f"{prefix}{message}"

    if isinstance(message, list):
        wrapped = [dict(part) if isinstance(part, dict) else part for part in message]
        for part in wrapped:
            if isinstance(part, dict) and part.get("type") == "text":
                part["text"] = f"{prefix}{part.get('text', '')}"
                return wrapped
        return [{"type": "text", "text": prefix.rstrip()}] + wrapped

    return message
