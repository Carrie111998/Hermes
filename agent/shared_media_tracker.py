"""Recent-shared-links tracking for the per-turn user-message sidecar.

A URL pasted in message *text* (as opposed to a file/image attachment) gets
no structured extraction anywhere in the pipeline, so it survives only as
prose inside one historical user turn. Several turns later the model has no
durable "the user cares about this" marker to look at, the user re-sends the
link, and FTS5 session search can't recover it from a vague description
("that video you sent") because the query never matches the URL text.

This module keeps a small, bounded index of those URLs: it scans persisted
user-message rows, renders each distinct URL as a short host+path label with
a recency marker, and hands the caller a fenced block to inject.

Both functions are pure — the caller fetches the rows (see
``SessionDB.get_recent_user_messages``) and passes them in. Reading from the
PERSISTED rows rather than the live ``conversation_history`` is deliberate:
idle/preflight compaction rewrites the in-memory list into a summary, so a
link shared before a compaction boundary would vanish from the in-memory
view while still sitting in the durable transcript.

The rendered block rides the per-turn ``api_content`` sidecar (see
``agent.turn_context.compose_user_api_content``), never the system prompt —
its contents change every turn, and the system prompt must stay byte-stable
turn-to-turn for the provider prompt cache.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

from agent.message_content import flatten_message_text

# http/https only: those are the links a user can actually be asked about
# again. Stops at whitespace and at the bracket/quote characters that
# routinely *surround* a pasted URL rather than belong to it.
_URL_RE = re.compile(r'https?://[^\s<>"\'`\]\)\}]+', re.IGNORECASE)

# Trailing punctuation that reads as sentence punctuation, not URL content
# ("see https://example.com/x." → the period is the sentence's).
_TRAILING_PUNCT = ".,;:!?'\"`"

# Label budget. The point of the label is to be *recognisable*, not
# resolvable — the full URL still lives in the transcript, so paying ~40
# extra tokens per entry to repeat it every turn buys nothing.
_MAX_LABEL_CHARS = 48
_ELLIPSIS = "..."


def _row_text(row: Dict[str, Any]) -> str:
    """Return the scannable text of a persisted message row.

    Session rows store ``content``; some callers (and older row shapes)
    carry ``text`` instead. Multimodal rows decode to a list of parts, so
    the shared flattener is used rather than a str() that would stringify
    the whole part list including base64 image payloads.
    """
    if not isinstance(row, dict):
        return ""
    content = row.get("content")
    if content is None:
        content = row.get("text")
    return flatten_message_text(content)


def _normalize_url(raw: str) -> str:
    """Strip trailing sentence punctuation from a regex-matched URL."""
    return raw.rstrip(_TRAILING_PUNCT)


def link_label(url: str) -> str:
    """Render ``url`` as a short host+path label for the context block.

    Drops the scheme and a leading ``www.`` (pure noise at this size) and
    truncates the whole thing to ``_MAX_LABEL_CHARS`` with an ellipsis, so
    a 300-character tracking URL costs the same as a short one.
    """
    label = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    label = re.sub(r"^www\.", "", label, flags=re.IGNORECASE)
    label = label.rstrip("/") or label
    if len(label) > _MAX_LABEL_CHARS:
        label = label[: _MAX_LABEL_CHARS - len(_ELLIPSIS)] + _ELLIPSIS
    return label


def extract_recent_shared_links(
    rows: Sequence[Dict[str, Any]], limit: int = 8
) -> List[Dict[str, Any]]:
    """Extract the most recent distinct URLs shared in ``rows``.

    ``rows`` are persisted user-message rows in chronological order (oldest
    first) — the shape ``SessionDB.get_recent_user_messages`` returns. Rows
    that carry a ``role`` other than ``user`` are skipped, so a caller that
    hands over a mixed slice of the transcript still only mines what the
    *user* actually shared.

    Returns at most ``limit`` entries, newest first, each a dict of:

    * ``url`` — the full URL, for callers that need to resolve it,
    * ``label`` — the truncated host+path rendering (see :func:`link_label`),
    * ``turns_ago`` — distance in user turns, counting the newest supplied
      row as 1. At the prologue call site the current turn's row has not
      been persisted yet, so the newest row is genuinely the previous turn.

    Deduped by URL keeping the most recent mention: a link re-sent this turn
    should read as fresh, not as however old its first appearance was.
    """
    if limit <= 0:
        return []

    user_rows = [
        row
        for row in rows or []
        if isinstance(row, dict) and row.get("role", "user") == "user"
    ]

    found: List[Dict[str, Any]] = []
    seen: set = set()
    # Newest first so the first sighting of a URL is also its most recent.
    for offset, row in enumerate(reversed(user_rows)):
        text = _row_text(row)
        if not text or "http" not in text:
            continue
        for match in _URL_RE.finditer(text):
            url = _normalize_url(match.group(0))
            if not url or url in seen:
                continue
            seen.add(url)
            found.append(
                {
                    "url": url,
                    "label": link_label(url),
                    "turns_ago": offset + 1,
                }
            )
            if len(found) >= limit:
                return found
    return found


def build_recent_links_context_block(
    rows: Sequence[Dict[str, Any]], limit: int = 8
) -> str:
    """Render recent shared links as a fenced block, or ``""`` when none.

    Empty-input convention mirrors
    :func:`agent.memory_manager.build_memory_context_block`: an empty string
    (not ``None``), so ``compose_user_api_content`` can treat every
    injection source with the same falsy check.
    """
    links = extract_recent_shared_links(rows, limit=limit)
    if not links:
        return ""
    lines = [
        "- {label} ({turns} turn{plural} ago)".format(
            label=link["label"],
            turns=link["turns_ago"],
            plural="" if link["turns_ago"] == 1 else "s",
        )
        for link in links
    ]
    return (
        "<recent-shared-links>\n"
        + "\n".join(lines)
        + "\n</recent-shared-links>"
    )
