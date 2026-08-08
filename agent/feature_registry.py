"""Declarative registry of Hermes capabilities for proactive suggestion.

PR-B of the "Feature Onboarding" initiative: the agent learns which Hermes
capability fits the user's current need and *suggests* it (advisory only —
never auto-executes by default).

Design invariants (see SECURITY-BASELINE.md in the PR):

* **No new core tools.** Every registry entry references an existing,
  already-audited tool / skill / capability. The narrow-waist rule from
  AGENTS.md holds: nothing here ships on the API call schema.
* **Advisory by default.** The router emits *text suggestions* appended to
  the current turn's API copy (the ``ext_prefetch_cache`` sidecar). It never
  invokes a tool by itself and never weakens approval/redaction/egress gates.
* **Explainable.** Each suggestion carries a ``why`` string so users (and
  reviewers) know exactly why it fired.
* **Local, offline, cache-safe.** Pure Python keyword/signal matching over
  the current user message; no network; no system-prompt mutation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Feature:
    """A Hermes capability that the router may suggest.

    ``keywords`` and ``signals`` are matched against the current user message
    (case-insensitive substring/regex). ``min_confidence`` gates firing.
    ``suggested_capability`` names an existing tool / slash command / skill
    the agent should consider — resolved against a whitelist at router init.
    """

    id: str
    name: str
    keywords: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()            # regex patterns (compiled at init)
    suggested_capability: str = ""            # e.g. "delegate_task", "/whats-new"
    benefit: str = ""                          # what the user gains
    min_confidence: float = 0.7
    auto_apply_safe: bool = False              # True only when invoking the
                                               # capability has zero
                                               # side effects (e.g. /help)
    # Compiled regexes (populated by compile_features).
    _compiled: tuple = field(default=(), repr=False, compare=False)

    def match(self, text: str) -> float:
        """Return a confidence score in [0, 1] for ``text``.

        Keywords/patterns are OR semantics: hitting ANY signal means the
        feature is relevant. Score = min(1.0, hits) so ``min_confidence``
        acts as a "how many signals must fire" gate (default 0.7 ≈ at least
        one hit; raise to 1.5+ to require multiple signals).
        """
        hits = 0
        for kw in self.keywords:
            if kw.lower() in text.lower():
                hits += 1
        for pat in self._compiled:
            if pat.search(text):
                hits += 1
        return min(1.0, float(hits))


def compile_features(features: List[Feature]) -> List[Feature]:
    """Compile regex patterns into a list of frozen Feature instances."""
    out: List[Feature] = []
    for f in features:
        compiled = tuple(
            re.compile(p, re.IGNORECASE) for p in f.patterns
        )
        out.append(
            Feature(
                id=f.id,
                name=f.name,
                keywords=f.keywords,
                patterns=f.patterns,
                suggested_capability=f.suggested_capability,
                benefit=f.benefit,
                min_confidence=f.min_confidence,
                auto_apply_safe=f.auto_apply_safe,
                _compiled=compiled,
            )
        )
    return out


# ---------------------------------------------------------------------------
# The registry.  Every entry references EXISTING capabilities.
# ---------------------------------------------------------------------------

# Whitelist of capability names the router may suggest.  Anything else is
# dropped at init (defense: a registry entry can't name a tool that doesn't
# exist, and can't be repointed at something dangerous).
KNOWN_CAPABILITIES = frozenset({
    "delegate_task",        # parallel subagents
    "cronjob",              # scheduled jobs
    "/whats-new",           # release briefs (PR-A)
    "web_search",           # web research
    "browser_navigate",     # browser automation
    "terminal",             # shell
    "memory",               # save durable facts
    "skill_manage",         # create/update skills
    "kanban",               # multi-agent work queue
    "moa",                  # mixture of agents
    "computer_use",         # desktop control
})


def _seed_features() -> List[Feature]:
    return [
        Feature(
            id="parallel_subtasks",
            name="Parallelize independent subtasks",
            keywords=(
                "parallel", "batch", "同时", "并行", "批量",
                "multi-task", "multitask", "several files", "multiple repos",
            ),
            patterns=(r"\b(?:at once|in parallel|all of them|each of)\b",),
            suggested_capability="delegate_task",
            benefit="Run independent subtasks in parallel (~3x wall-clock reduction on typical batches).",
            min_confidence=0.6,
        ),
        Feature(
            id="scheduled_recurring",
            name="Schedule recurring work",
            keywords=(
                "every day", "daily", "weekly", "every morning", "每", "每天",
                "recurring", "scheduled", "remind me",
            ),
            patterns=(r"\b(?:daily|weekly|every\s+\w+)\b",),
            suggested_capability="cronjob",
            benefit="Automate recurring checks/pushes without manual re-runs.",
            min_confidence=0.6,
        ),
        Feature(
            id="web_research",
            name="Research the web",
            keywords=(
                "research", "search", "find out", "look up", "查", "搜索",
                "investigate", "sources", "citations",
            ),
            patterns=(r"\b(?:latest|current|today)\b",),
            suggested_capability="web_search",
            benefit="Ground answers in live web sources with citations.",
            min_confidence=0.5,
        ),
        Feature(
            id="remember_fact",
            name="Persist a durable fact",
            keywords=(
                "remember", "don't forget", "always", "prefer", "记住",
                "我的偏好", "以后", "from now on",
            ),
            patterns=(r"\b(?:i prefer|i like|remember that)\b",),
            suggested_capability="memory",
            benefit="Make the preference persist across sessions.",
            min_confidence=0.55,
        ),
        Feature(
            id="release_brief",
            name="Check what's new in this release",
            keywords=(
                "what's new", "whats new", "new features", "changelog",
                "更新了什么", "新功能", "release notes",
            ),
            patterns=(r"\b(?:version|release)\b",),
            suggested_capability="/whats-new",
            benefit="See the current release's new features and how to use them.",
            min_confidence=0.5,
            auto_apply_safe=True,  # read-only informational command
        ),
    ]


class FeatureRegistry:
    """Loads, compiles, and serves the feature registry."""

    def __init__(self, features: Optional[List[Feature]] = None):
        raw = features if features is not None else _seed_features()
        # Whitelist guard: drop any entry naming an unknown capability.
        kept: List[Feature] = []
        for f in raw:
            if f.suggested_capability and f.suggested_capability not in KNOWN_CAPABILITIES:
                logger.warning(
                    "feature %s references unknown capability %r — dropped",
                    f.id, f.suggested_capability,
                )
                continue
            kept.append(f)
        self.features = compile_features(kept)
        self._capability_map = {f.id: f for f in self.features}

    def suggest(self, text: str, *, min_confidence: float | None = None) -> Optional[Feature]:
        """Return the best-matching feature for ``text``, or None.

        ``min_confidence`` overrides the per-feature threshold (used by the
        router's global conservative default).
        """
        best: Optional[Feature] = None
        best_score = 0.0
        for f in self.features:
            threshold = min_confidence if min_confidence is not None else f.min_confidence
            score = f.match(text)
            if score >= threshold and score > best_score:
                best = f
                best_score = score
        return best

    def suggest_text(self, text: str, *, min_confidence: float | None = None) -> str:
        """Return a formatted suggestion string, or '' when nothing matches."""
        f = self.suggest(text, min_confidence=min_confidence)
        if f is None:
            return ""
        lines = [
            f"[feature-suggestion] Consider using **{f.name}** "
            f"(capability: `{f.suggested_capability}`).",
            f"  Why: {f.benefit}",
            "  (Advisory — nothing runs without your approval. Dismiss with "
            "`proactive_features.enabled: false`.)",
        ]
        return "\n".join(lines)
