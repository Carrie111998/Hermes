"""
Skill Hints Plugin — proactive skill injection via pre_llm_call.

On every turn, embeds the user message against the skill index and injects
top-3 matching skill names as a compact hint. The model sees:

    [Skill hints: vrm-rendering, browser-3d-character]

This catches cases where the model wouldn't proactively call skill_retrieve
because it doesn't know a skill exists for its current task.

Zero-cost when no skills match (returns None → no injection).
Skips short messages (<10 chars) to avoid noise on greetings/follow-ups.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_MIN_QUERY_LEN = 10
_TOP_K = 3
_MIN_SCORE = 0.01  # keyword threshold below which we don't inject


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", on_pre_llm_call)


def on_pre_llm_call(**kwargs: Any) -> dict | None:
    """Inject top-3 skill hints based on the user's message.

    Returns {"context": "[Skill hints: ...]"} or None.
    """
    try:
        user_message = kwargs.get("user_message", "")
        if not user_message or len(user_message.strip()) < _MIN_QUERY_LEN:
            return None

        # Lazy import — avoids loading skill_retrieve_tool at module level
        from tools.skill_retrieve_tool import skill_retrieve
        import json

        result_json = skill_retrieve(user_message.strip(), top_k=_TOP_K)
        result = json.loads(result_json)

        skills = result.get("skills", [])
        if not skills:
            return None

        # Filter by minimum score
        filtered = [s for s in skills if s.get("score", 0) >= _MIN_SCORE]
        if not filtered:
            return None

        # Format as compact hint
        names = [s["name"] for s in filtered]
        hint = f"[Skill hints: {', '.join(names)}]"

        return {"context": hint}
    except Exception as exc:
        logger.debug("skill_hints pre_llm_call failed: %s", exc)
        return None
