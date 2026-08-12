"""Shared rendering for "still working" progress heartbeats.

Two independent code paths tell a user that something is still running:

- ``gateway/run.py``'s ``_notify_long_running`` — the periodic ping for a
  long CoS conversational turn.
- ``gateway/kanban_watchers.py``'s ``progress`` event branch — the periodic
  ping for a dispatched kanban task.

They used to render in two different dialects ("⏳ Working — 4 min —
iteration 4/40, delegate_task" vs "⏳ Still working on X (running as Y) —
about 7 minutes in."), so the same user got two vocabularies for the same
idea. This module owns the one canonical shape; both call sites format
through it so wording changes happen in exactly one place.

The kanban phrasing is the canonical template — it is the richer of the two
and its exact substrings are asserted by
``tests/gateway/test_kanban_progress_heartbeat.py``. Fields the caller does
not have (title, assignee) are omitted gracefully rather than faked.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["format_progress_heartbeat"]

# Keep parity with the kanban payload truncation that predated this helper.
_TITLE_LIMIT = 120
_NOTE_LIMIT = 200


def _elapsed_phrase(elapsed_minutes: Any) -> str:
    """Plain-English elapsed wording, or "still going" when unknown."""
    try:
        minutes = int(elapsed_minutes)
    except (TypeError, ValueError):
        return "still going"
    if minutes < 1:
        return "less than a minute in"
    if minutes == 1:
        return "about 1 minute in"
    return f"about {minutes} minutes in"


def format_progress_heartbeat(
    title: Optional[Any] = None,
    who: Optional[Any] = None,
    elapsed_minutes: Optional[Any] = None,
    note: Optional[Any] = None,
    board_tag: str = "",
) -> str:
    """Render one progress heartbeat in the canonical two-line shape.

    ``⏳ <board_tag>Still working[ on <title>][ (running as <who>)] — <elapsed>.``
    ``Latest update: <note>`` (or ``No status update yet.``)

    Every field is optional: a caller with no task title (the CoS turn path)
    simply gets "Still working — about 7 minutes in." with no dangling "on".
    """
    on_what = f" on {str(title)[:_TITLE_LIMIT]}" if title else ""
    as_who = f" (running as {who})" if who else ""
    update = (
        f"Latest update: {str(note)[:_NOTE_LIMIT]}"
        if note else "No status update yet."
    )
    return (
        f"⏳ {board_tag}Still working"
        f"{on_what}{as_who} — {_elapsed_phrase(elapsed_minutes)}."
        f"\n{update}"
    )
