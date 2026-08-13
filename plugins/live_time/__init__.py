"""live-time plugin — inject live current time into every LLM call.

Fixes the stale "Conversation started" timestamp problem: Hermes stamps the
session-start time once and never refreshes it, so long or cross-day
conversations leave the model with no sense of "now". This plugin injects the
real current time at every LLM call — ephemeral, on the user-message side,
never touching the cached system prompt (prompt caching stays intact).

See https://github.com/NousResearch/hermes-agent/issues/10421
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

_WEEKDAYS = "一二三四五六日"


def _on_pre_llm_call(**_: Any) -> Optional[dict]:
    """Return live-time context injected before every LLM call.

    The returned ``{"context": ...}`` block is appended on the user-message
    side of the request, so the agent always sees the current local time
    without invalidating prompt-cache prefixes.
    """
    now = datetime.now().astimezone()
    offset_h = now.utcoffset().total_seconds() / 3600
    weekday = _WEEKDAYS[now.weekday()]

    context = (
        f"[LIVE-TIME] Now: {now.strftime('%Y-%m-%d %H:%M:%S')} "
        f"(Weekday {now.weekday() + 1}/7, {weekday}), "
        f"TZ offset UTC{offset_h:+.0f}. "
        "Injected by live-time plugin at THIS LLM call's moment. "
        "Use THIS as the authoritative current time for any today/now/elapsed/"
        "date judgment. Ignore the 'Conversation started' timestamp in the "
        "system prompt for anything except session-creation facts."
    )
    return {"context": context}


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
