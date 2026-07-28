"""Gateway response filtering helpers.

These helpers operate at the gateway boundary: they decide whether a completed
agent turn should be delivered to the chat, not what should be persisted in the
conversation history.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

# Canonical model-emitted control token for intentional silence.
SILENT_REPLY_TOKEN = "NO_REPLY"

# Exact whole-response markers that mean "the agent intentionally chose not to
# reply".  Keep this list small and explicit; arbitrary empty output remains an
# error/empty-response path, not silence.
LIVE_GATEWAY_SILENT_MARKERS = frozenset({
    "[SILENT]",
    "SILENT",
    "NO_REPLY",
    "NO REPLY",
})


def _canonical_silence_candidate(text: str) -> str:
    return " ".join(text.strip().upper().split())


def _strip_edge_silence_punctuation(text: str) -> str:
    """Strip stray edge punctuation without erasing marker structure.

    Models sometimes emit ``.NO_REPLY`` or ``*NO_REPLY*`` instead of the exact
    marker. Keep square brackets structural so malformed ``[SILENT`` does not
    become ``SILENT``.
    """
    start = 0
    end = len(text)
    while start < end and text[start] not in "[]" and unicodedata.category(text[start]).startswith("P"):
        start += 1
    while end > start and text[end - 1] not in "[]" and unicodedata.category(text[end - 1]).startswith("P"):
        end -= 1
    return text[start:end].strip()


def _canonical_silence_candidates(text: str) -> tuple[str, ...]:
    exact = _canonical_silence_candidate(text)
    stripped = _strip_edge_silence_punctuation(text.strip())
    if stripped == text.strip():
        return (exact,)
    fallback = _canonical_silence_candidate(stripped)
    return (exact, fallback)


# --- JSON silence envelope recognition (#72935) ---
#
# Models sometimes emit ``{"action":"NO_REPLY"}`` instead of the bare
# ``NO_REPLY`` text marker.  The envelope must be *exact*: one key named
# ``action`` whose decoded value is ``NO_REPLY``.  Anything else (extra
# keys, duplicate keys, different action, invalid JSON, surrounding
# whitespace) is treated as visible content.

_JSON_NO_REPLY_KEYS = ("action",)
_JSON_NO_REPLY_VALUE = "NO_REPLY"


def _is_json_no_reply_envelope(text: str) -> bool:
    """Return True when *text* is exactly ``{"action":"NO_REPLY"}``."""
    stripped = text.strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        return False
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return False
    # Must be a dict with exactly one key.
    if not isinstance(obj, dict) or len(obj) != 1:
        return False
    key, value = next(iter(obj.items()))
    return key == _JSON_NO_REPLY_KEYS[0] and value == _JSON_NO_REPLY_VALUE


# Streaming prefixes that could still become {"action":"NO_REPLY"}.
# We track which characters of the canonical JSON are valid prefixes.
_JSON_CANONICAL = json.dumps(
    {"action": _JSON_NO_REPLY_VALUE}, separators=(",", ":")
)  # '{"action":"NO_REPLY"}'


def _is_json_no_reply_prefix(text: str) -> bool:
    """Return True while *text* could still become ``{"action":"NO_REPLY"}``.

    Handles incremental streaming prefixes like ``{"action":"NO``, ``{"``,
    ``{``, etc.  Stops buffering as soon as the prefix diverges from the
    canonical envelope.
    """
    stripped = text.strip()
    if not stripped:
        return False
    # Must start with ``{`` to be a candidate.
    if stripped[0] != "{":
        return False
    canon = _JSON_CANONICAL
    if len(stripped) > len(canon):
        return False
    # Check character-by-character against the canonical JSON.
    for i, ch in enumerate(stripped):
        if ch != canon[i]:
            return False
    return True


def is_intentional_silence_response(response: Any) -> bool:
    """Return True only when ``response`` is exactly a silence marker.

    Substantive prose that merely mentions ``NO_REPLY`` or ``[SILENT]`` must be
    delivered normally.  A blank response is also not silence; blank output is
    handled by the empty-response failure path.

    Recognises both bare text markers (``NO_REPLY``, ``[SILENT]``) and the
    structured JSON envelope ``{"action":"NO_REPLY"}`` (#72935).
    """
    if not isinstance(response, str):
        return False
    stripped = response.strip()
    if not stripped:
        return False
    if len(stripped) > 64:
        return False
    if any(candidate in LIVE_GATEWAY_SILENT_MARKERS for candidate in _canonical_silence_candidates(stripped)):
        return True
    return _is_json_no_reply_envelope(stripped)


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
    # JSON silence envelope {"action":"NO_REPLY"} (#72935).
    if _is_json_no_reply_envelope(stripped):
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

    Also recognises streaming prefixes of the JSON silence envelope
    ``{"action":"NO_REPLY"}`` (#72935).
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > 64:
        return False
    for candidate in _canonical_silence_candidates(stripped):
        if candidate and any(marker.startswith(candidate) for marker in LIVE_GATEWAY_SILENT_MARKERS):
            return True
    # JSON envelope streaming prefix (#72935).
    return _is_json_no_reply_prefix(stripped)
