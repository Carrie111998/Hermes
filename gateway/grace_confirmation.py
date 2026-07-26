"""Render a natural user-facing preview of Grace's compiled Loop Contract."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


MAX_CONFIRMATION_CHARS = 900
_MAX_ITEM_CHARS = 320


def _clean(value: Any, *, limit: int = _MAX_ITEM_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _direct_address(text: str) -> str:
    """Keep Grace's chat voice in first/second person, not third-person KJ prose."""
    direct = (
        text.replace("KJ 選擇由他本人", "你會自己")
        .replace("KJ 本人", "你自己")
        .replace("值得 KJ 人工申請", "值得你手動申請")
        .replace("Grace／ClawOps 僅需", "我只需要")
        .replace("Grace/ClawOps 僅需", "我只需要")
        .replace("不得代為", "我不會代你")
        .replace("並需修正", "另外，我也會修正")
        .replace("KJ 已", "你已")
        .replace("KJ 將", "你會")
        .replace("KJ", "你")
    )
    return re.sub(r"\s*你\s*", "你", direct)


def build_delegation_confirmation(
    tool_name: str,
    args: Mapping[str, Any] | None,
) -> str | None:
    """Return a bounded confirmation derived only from the canonical contract.

    The raw ``original_request`` is deliberately ignored. The preview therefore
    reflects Grace's interpreted contract—the same instruction ClawOps is about
    to receive—without echoing audit-only input or claiming the task is queued.
    """
    if tool_name != "clawops_delegate" or not isinstance(args, Mapping):
        return None

    goal = args.get("goal")
    scope = args.get("scope")
    verification = args.get("verification")
    if not all(isinstance(section, Mapping) for section in (goal, scope, verification)):
        return None

    interpretation = _clean(args.get("grace_interpretation"), limit=320)
    objective = _clean(goal.get("objective"), limit=320)
    if not interpretation or not objective:
        return None

    interpretation = _direct_address(interpretation)
    lines = [
        f"好的，我了解了。{interpretation}",
        (
            "我先檢查這份委派的執行範圍、路由與核准條件。"
            "只有驗證通過且任務確實建立後，我才會回報 execution task ID "
            "和 Grace review task ID；若需要對外操作，我會先給你精確的核准範圍。"
        ),
    ]
    rendered = "\n".join(lines)
    if len(rendered) <= MAX_CONFIRMATION_CHARS:
        return rendered
    return rendered[: MAX_CONFIRMATION_CHARS - 1].rstrip() + "…"
