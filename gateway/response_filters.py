"""Mechanical validation for model-authored delivery outcomes.

The gateway never interprets response prose.  The primary model may author a
turn-bound ``delivery_outcome`` through the ``todo`` tool; this boundary only
validates the exact receipt and executes deliver/suppress.  Unknown, malformed,
stale, or failed results always remain deliverable.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Mapping

from agent.delivery_outcome import (
    DELIVERY_ACTIONS,
    MAX_DELIVERY_REASON_CHARS,
)


_OUTCOME_KEYS = frozenset({"action", "reason", "turn_id"})

# Canonical model-emitted control token for intentional silence.
SILENT_REPLY_TOKEN = "NO_REPLY"

# Keep the exact whole-response marker set small and explicit. Blank output
# remains an error/empty-response path rather than intentional silence.
LIVE_GATEWAY_SILENT_MARKERS = frozenset({
    "[SILENT]",
    "SILENT",
    "NO_REPLY",
    "NO REPLY",
})


def _canonical_silence_candidate(text: str) -> str:
    return " ".join(text.strip().upper().split())


def _strip_edge_silence_punctuation(text: str) -> str:
    """Strip stray edge punctuation without erasing marker structure."""
    start = 0
    end = len(text)
    while (
        start < end
        and text[start] not in "[]"
        and unicodedata.category(text[start]).startswith("P")
    ):
        start += 1
    while (
        end > start
        and text[end - 1] not in "[]"
        and unicodedata.category(text[end - 1]).startswith("P")
    ):
        end -= 1
    return text[start:end].strip()


def _canonical_silence_candidates(text: str) -> tuple[str, ...]:
    exact = _canonical_silence_candidate(text)
    stripped = _strip_edge_silence_punctuation(text.strip())
    if stripped == text.strip():
        return (exact,)
    return (exact, _canonical_silence_candidate(stripped))


def is_intentional_silence_response(response: Any) -> bool:
    """Return True only when ``response`` is exactly a silence marker."""
    if not isinstance(response, str):
        return False
    stripped = response.strip()
    if not stripped or len(stripped) > 64:
        return False
    return any(
        candidate in LIVE_GATEWAY_SILENT_MARKERS
        for candidate in _canonical_silence_candidates(stripped)
    )


def validated_delivery_outcome(
    agent_result: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    """Return an exact, same-turn outcome or ``None`` on any uncertainty."""

    if not isinstance(agent_result, Mapping):
        return None
    turn_id = agent_result.get("turn_id")
    outcome = agent_result.get("delivery_outcome")
    if type(turn_id) is not str or not turn_id:
        return None
    if not isinstance(outcome, Mapping):
        return None
    if frozenset(outcome.keys()) != _OUTCOME_KEYS:
        return None

    action = outcome.get("action")
    reason = outcome.get("reason")
    outcome_turn_id = outcome.get("turn_id")
    if type(action) is not str or action not in DELIVERY_ACTIONS:
        return None
    if type(reason) is not str or not reason.strip():
        return None
    if len(reason) > MAX_DELIVERY_REASON_CHARS:
        return None
    if type(outcome_turn_id) is not str or outcome_turn_id != turn_id:
        return None
    return {
        "action": action,
        "reason": reason,
        "turn_id": outcome_turn_id,
    }


def should_suppress_delivery(agent_result: Mapping[str, Any] | None) -> bool:
    """Execute only an exact suppress choice from a known-successful turn."""

    if not isinstance(agent_result, Mapping):
        return False
    # Fail open for delivery: failure, missing status, or a malformed status
    # must never hide the diagnostic response from the user.
    if agent_result.get("failed") is not False:
        return False
    outcome = validated_delivery_outcome(agent_result)
    return bool(outcome and outcome["action"] == "suppress")


def is_autonomous_silence_response(response: Any) -> bool:
    """Loose silence matcher for autonomous lanes (cron, webhook).

    Autonomous lanes instruct the agent to emit ``[SILENT]`` when a tick
    produced nothing worth a human's attention, and models reliably bracket
    the marker with a short note explaining why they stayed quiet.  Unlike
    :func:`is_intentional_silence_response` (the interactive-chat rule, which
    demands the response be EXACTLY a marker), this suppresses when a marker
    is the whole response, sits on its own first or last line, or the
    bracketed sentinel opens the response (the documented
    ``[SILENT] No changes detected`` pattern).  A token buried mid-sentence
    in a genuine report is still delivered.

    Shares :data:`LIVE_GATEWAY_SILENT_MARKERS` so the interactive and
    autonomous marker sets can never drift apart.
    """
    if not isinstance(response, str):
        return False
    stripped = response.strip()
    if not stripped:
        return False

    def _is_token(line: str) -> bool:
        return _canonical_silence_candidate(line) in LIVE_GATEWAY_SILENT_MARKERS

    # Whole response is exactly a token.
    if _is_token(stripped):
        return True
    # Marker on its own first or last line (leading/trailing note on a
    # separate line — e.g. "2 deals filtered\n\n[SILENT]").
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if lines and (_is_token(lines[0]) or _is_token(lines[-1])):
        return True
    # Bracketed sentinel used as a same-line prefix — the documented pattern
    # "[SILENT] No changes detected".  Restricted to the bracketed form so a
    # bare word like "Silent retry succeeded" is NOT swallowed.
    if stripped.upper().startswith("[SILENT]"):
        return True
    return False


def is_intentional_silence_agent_result(agent_result: dict | None, response: Any) -> bool:
    """Silence markers suppress delivery only for successful agent turns."""
    if not isinstance(agent_result, dict):
        return False
    if agent_result.get("failed"):
        return False
    return is_intentional_silence_response(response)


def is_partial_silence_marker(text: Any) -> bool:
    """Return True while ``text`` could still resolve to a silence marker.

    The streaming path accumulates the reply delta-by-delta and must decide,
    before the whole response is known, whether to show what it has so far.
    A buffer whose canonical form is a non-empty *prefix* of a silence marker
    (e.g. ``"NO"`` on the way to ``"NO_REPLY"``, or an exact marker that has
    not yet been terminated by stream-end) is held back so a raw marker is
    never edited onto the screen and then belatedly retracted.

    Anything that has already diverged from every marker (ordinary prose) —
    and anything longer than the marker cap — returns False so normal
    streaming resumes immediately.  This is the streaming counterpart to
    :func:`is_intentional_silence_response`, sharing the same marker set and
    canonicalization so the two never drift.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > 64:
        return False
    for candidate in _canonical_silence_candidates(stripped):
        if candidate and any(marker.startswith(candidate) for marker in LIVE_GATEWAY_SILENT_MARKERS):
            return True
    return False


__all__ = [
    "LIVE_GATEWAY_SILENT_MARKERS",
    "SILENT_REPLY_TOKEN",
    "is_autonomous_silence_response",
    "is_intentional_silence_agent_result",
    "is_intentional_silence_response",
    "is_partial_silence_marker",
    "should_suppress_delivery",
    "validated_delivery_outcome",
]
