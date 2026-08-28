"""Deterministic, dependency-free routing for conditional MoA fan-out.

The router decides only whether a configured MoA preset should consult its
reference models.  The aggregator remains the acting model in both branches,
so a decision never swaps providers, tools, or the cached system prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


_DELIBERATION = re.compile(
    r"\b(compare|contrast|evaluate|assess|critique|review|recommend|trade[ -]?offs?|"
    r"pros and cons|multiple perspectives?|second opinion)\b",
    re.IGNORECASE,
)
_HIGH_STAKES = re.compile(
    r"\b(security|privacy|legal|contract|medical|financial|production|architecture|"
    r"migration|incident|risk|threat model)\b",
    re.IGNORECASE,
)
_MULTI_PART = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*])\s+", re.MULTILINE)
_SIMPLE = re.compile(
    r"^\s*(?:what time|what date|translate|rephrase|format|spell|define|convert)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MoARouteDecision:
    """Explainable result of one request-local routing decision."""

    fanout: bool
    mode: str
    score: int
    reasons: tuple[str, ...]


def decide_moa_route(
    prompt: str,
    routing: Mapping[str, Any] | None = None,
) -> MoARouteDecision:
    """Choose direct aggregator-only execution or advisor fan-out.

    ``always`` preserves historical MoA behaviour and is the default.
    ``never`` makes the preset an aggregator-only route. ``auto`` applies a
    small explainable intent/complexity scorer without an embedding service or
    extra billable model call.
    """

    config = routing if isinstance(routing, Mapping) else {}
    mode = str(config.get("mode") or "always").strip().lower()
    if mode not in {"always", "never", "auto"}:
        mode = "always"
    if mode == "always":
        return MoARouteDecision(True, mode, 0, ("configured-always",))
    if mode == "never":
        return MoARouteDecision(False, mode, 0, ("configured-never",))

    text = str(prompt or "").strip()
    score = 0
    reasons: list[str] = []
    if _DELIBERATION.search(text):
        score += 2
        reasons.append("deliberation")
    if _HIGH_STAKES.search(text):
        score += 3
        reasons.append("high-stakes")
    if _MULTI_PART.search(text) or len(re.findall(r"\b(?:and|then|also)\b", text, re.I)) >= 2:
        score += 1
        reasons.append("multi-part")
    word_count = len(text.split())
    if word_count >= 120:
        score += 1
        reasons.append("long-context")
    if word_count >= 300:
        score += 1
        reasons.append("very-long-context")
    simple_evidence = bool(_SIMPLE.search(text))
    if simple_evidence:
        reasons.append("simple-utility")

    try:
        threshold = int(config.get("threshold", 3))
    except (TypeError, ValueError):
        threshold = 3
    threshold = max(1, threshold)
    # Fail safe: auto-routing may bypass advisors only when the prompt carries
    # affirmative evidence of being a simple utility request. Unknown,
    # ambiguous, empty, and non-English intent keeps historical MoA fan-out.
    fanout = score >= threshold or not simple_evidence
    if fanout and score < threshold and not simple_evidence:
        reasons.append("ambiguous")
    return MoARouteDecision(fanout, mode, score, tuple(reasons))
