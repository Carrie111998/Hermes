"""
Reaction Router — dispatch Telegram message reactions to registered callbacks.

When the agent sends a message that expects a reaction-based response (e.g.
"React ✅ to approve"), it registers a callback keyed by ``(chat_id, message_id)``.
The router sits between the raw ``_handle_message_reaction`` handler and the
generic ``MessageEvent`` dispatch, so known patterns are handled inline and the
rest fall through to normal agent processing.

Three categories wired:

1. **Operational Control** — ✅/❌ resolves approvals. 🔄 triggers retry.
   ↩️ triggers undo.

2. **Menu & Selection** — Agent presents emoji-tagged options, registers a
   listener, user reacts once to pick, callback fires with the selection.

3. **Feedback** — Silent per-reaction quality tracking. Positive vs negative
   reaction counts per response type, used for self-calibration.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Callback registry
# ---------------------------------------------------------------------------
# Key: (chat_id: str, message_id: int)
# Value: dict with keys: callback, timeout_at, description, category

_ReactionCallback = Callable[
    [str, int, str, Dict[str, Any]], Coroutine[Any, Any, None]
]
"""async callback(chat_id, message_id, emoji, metadata) -> None"""

_listeners: Dict[Tuple[str, int], Dict[str, Any]] = {}
_timeout_sec = 300  # default 5 minutes

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def register(
    chat_id: str,
    message_id: int,
    callback: _ReactionCallback,
    *,
    timeout: int = 300,
    description: str = "",
    category: str = "generic",
) -> None:
    """Register a reaction callback for ``(chat_id, message_id)``.

    When a reaction arrives for this chat+message, the router calls
    ``callback(chat_id, message_id, emoji, metadata)``.  The callback
    is invoked exactly once (on the first matching reaction), then
    auto-deregistered.

    ``timeout`` seconds after registration the entry expires and is
    removed without firing.
    """
    key = (chat_id, message_id)
    _listeners[key] = {
        "callback": callback,
        "timeout_at": time.monotonic() + timeout,
        "description": description or f"{category}@{chat_id}:{message_id}",
        "category": category,
    }
    _gc_expired()
    logger.debug(
        "[ReactionRouter] Registered listener %s (cat=%s, timeout=%ds)",
        key, category, timeout,
    )


def unregister(chat_id: str, message_id: int) -> bool:
    """Remove a reaction callback.  Returns True if one existed."""
    key = (chat_id, message_id)
    entry = _listeners.pop(key, None)
    if entry:
        logger.debug("[ReactionRouter] Unregistered listener %s (%s)", key, entry["description"])
        return True
    return False


async def dispatch(chat_id: str, message_id: int, emoji: str, metadata: Dict[str, Any]) -> bool:
    """Try to dispatch an incoming reaction.

    Returns True if a registered callback handled it (message consumed).
    Returns False if no match — caller should fall through to generic event.
    """
    key = (chat_id, message_id)
    entry = _listeners.pop(key, None)
    if not entry:
        # Also try with message_id as str (type mismatch safety)
        return False

    if time.monotonic() > entry["timeout_at"]:
        logger.debug("[ReactionRouter] Listener %s expired, dropping reaction %s", key, emoji)
        return True  # consumed but silently dropped

    try:
        await entry["callback"](chat_id, message_id, emoji, metadata)
        logger.debug("[ReactionRouter] Dispatched %s → %s", key, emoji)
    except Exception:
        logger.exception(
            "[ReactionRouter] Callback error for %s (%s)", key, entry["description"]
        )
    return True


def active_listeners() -> list[dict]:
    """Return a snapshot of current listeners for diagnostics."""
    _gc_expired()
    now = time.monotonic()
    result = []
    for (cid, mid), entry in list(_listeners.items()):
        remaining = max(0, entry["timeout_at"] - now)
        result.append({
            "chat_id": cid,
            "message_id": mid,
            "description": entry["description"],
            "category": entry["category"],
            "remaining_sec": round(remaining, 1),
        })
    return result


def _gc_expired() -> None:
    now = time.monotonic()
    expired = [(cid, mid) for (cid, mid), e in list(_listeners.items()) if now > e["timeout_at"]]
    for key in expired:
        entry = _listeners.pop(key, None)
        if entry:
            logger.debug("[ReactionRouter] GC'd expired listener %s (%s)", key, entry["description"])


# ---------------------------------------------------------------------------
# Built-in reaction matchers
# ---------------------------------------------------------------------------

_APPROVAL_EMOJIS = {"✅", "👍", "✔️", "🟢", "❌", "👎", "🔴", "🛑", "⛔"}
_RETRY_EMOJIS = {"🔄", "🔁", "♻️"}
_UNDO_EMOJIS = {"↩️", "⏪", "🔙"}
_POSITIVE_FEEDBACK = {"👍", "🔥", "✨", "💯", "🫡", "👏", "🎯", "⭐", "💪"}
_NEGATIVE_FEEDBACK = {"👎", "😕", "🤷", "💤", "🥱"}

# Number emoji mapping
_NUMBER_EMOJIS = {
    "1️⃣": 1, "2️⃣": 2, "3️⃣": 3, "4️⃣": 4, "5️⃣": 5,
    "6️⃣": 6, "7️⃣": 7, "8️⃣": 8, "9️⃣": 9, "🔟": 10,
}


def classify_emoji(emoji: str) -> str:
    """Classify a reaction emoji into a semantic category.

    Returns one of: ``approve``, ``deny``, ``retry``, ``undo``,
    ``number``, ``positive``, ``negative``, ``nav_up``, ``nav_down``,
    ``save``, ``more``, ``unknown``.
    """
    if emoji in _APPROVAL_EMOJIS:
        if emoji in {"❌", "👎", "🔴", "🛑", "⛔"}:
            return "deny"
        return "approve"
    if emoji in _RETRY_EMOJIS:
        return "retry"
    if emoji in _UNDO_EMOJIS:
        return "undo"
    if emoji in _NUMBER_EMOJIS:
        return "number"
    if emoji in _POSITIVE_FEEDBACK:
        return "positive"
    if emoji in _NEGATIVE_FEEDBACK:
        return "negative"
    if emoji in {"🔽", "⬇️", "👇", "📥"}:
        return "nav_down"
    if emoji in {"🔼", "⬆️", "👆", "📤"}:
        return "nav_up"
    if emoji in {"📌", "📍", "🔖"}:
        return "save"
    if emoji in {"❓", "🤔", "💭"}:
        return "more"
    return "unknown"


def emoji_to_number(emoji: str) -> Optional[int]:
    """Convert a number emoji (1️⃣-9️⃣, 🔟) to its integer value."""
    return _NUMBER_EMOJIS.get(emoji)


# ---------------------------------------------------------------------------
# Feedback tracker (lightweight)
# ---------------------------------------------------------------------------

class ReactionFeedbackTracker:
    """Tracks reaction patterns per session for quality self-calibration.

    Usage::

        tracker.record(session_key, message_type, emoji, is_positive=True)
        stats = tracker.summary(session_key)
    """

    def __init__(self):
        self._data: Dict[str, Dict[str, Any]] = {}

    def record(
        self,
        session_key: str,
        message_type: str,
        emoji: str,
        classification: str,
    ) -> None:
        if session_key not in self._data:
            self._data[session_key] = {"positive": 0, "negative": 0, "by_type": {}}
        entry = self._data[session_key]

        if classification in ("approve", "positive"):
            entry["positive"] += 1
            entry.setdefault("by_type", {}).setdefault(message_type, {"positive": 0, "negative": 0})["positive"] += 1
        elif classification in ("deny", "negative"):
            entry["negative"] += 1
            entry.setdefault("by_type", {}).setdefault(message_type, {"positive": 0, "negative": 0})["negative"] += 1

    def summary(self, session_key: str) -> Optional[Dict[str, Any]]:
        data = self._data.get(session_key)
        if not data:
            return None
        total = data["positive"] + data["negative"]
        if total == 0:
            ratio = None
        else:
            ratio = round(data["positive"] / total, 3)
        return {
            "positive": data["positive"],
            "negative": data["negative"],
            "ratio": ratio,
            "by_type": data.get("by_type", {}),
        }


# Singleton
_feeback = ReactionFeedbackTracker()

def get_feedback_tracker() -> ReactionFeedbackTracker:
    return _feeback
