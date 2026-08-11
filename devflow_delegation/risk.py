"""Pure deterministic risk scoring for the DevFlow Delegation Plane.

This module is deliberately side-effect free: it does not inspect repositories,
execute commands, change lifecycle state, or enable autonomy. Callers provide
already-observed diff facts and the operator-owned target allowlist entry. A
missing or malformed fact fails closed as a critical, blocked assessment.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from devflow_delegation.allowlist import TargetConfig, path_allowed


_RISK_TIERS = ("low", "medium", "high", "critical")
_KNOWN_NONEXPLICIT_SOURCES = frozenset({
    "critic", "arch-review", "watchdog", "nightly-gate", "security-audit",
    "tracker", "curator", "matcher-shadow",
})
_SEVERITY_POINTS = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass(frozen=True)
class RiskInput:
    """Observed facts about one proposed change set.

    ``changed_lines`` is the total added + removed line count. ``None`` means
    the evidence was unavailable, not an empty diff. ``reversible`` means a
    concrete rollback path was verified; it is not inferred from a title or a
    request's free-form safety notes.
    """

    changed_paths: Tuple[str, ...]
    changed_lines: Optional[int]
    reversible: Optional[bool]
    severity: str
    source_kind: str


@dataclass(frozen=True)
class RiskAssessment:
    """Stable risk result suitable for durable audit logging."""

    score: int
    tier: str
    reasons: Tuple[str, ...]
    blocked: bool


def _diff_points(changed_lines: int) -> tuple[int, str | None]:
    if changed_lines <= 100:
        return 0, None
    if changed_lines <= 500:
        return 1, "medium_diff"
    if changed_lines <= 1000:
        return 2, "large_diff"
    return 3, "large_diff"


def _tier_for_score(score: int) -> str:
    if score <= 1:
        return "low"
    if score <= 4:
        return "medium"
    if score <= 8:
        return "high"
    return "critical"


def assess_risk(target: TargetConfig, facts: RiskInput) -> RiskAssessment:
    """Assess supplied change facts against the operator-owned target scope.

    Scoring is intentionally small and inspectable:
      * severity: low=0, medium=1, high=2, critical=3
      * diff footprint: <=100=0, <=500=1, <=1000=2, >1000=3
      * reversibility: verified=0, not reversible=2
      * source: explicit=0, known non-explicit=1, unknown=2

    Unscoped paths and missing/invalid evidence are hard blocks. Their score is
    retained for auditability, but their tier is forced to ``critical``.
    """
    reasons: list[str] = []
    blocked = False
    score = 0

    paths = tuple(str(path or "").strip() for path in facts.changed_paths)
    if not paths:
        reasons.append("missing_changed_paths")
        blocked = True
    else:
        for path in paths:
            if not path or not path_allowed(target, path):
                reasons.append("out_of_scope_path")
                blocked = True
                break

    severity = str(facts.severity or "").strip().lower()
    severity_points = _SEVERITY_POINTS.get(severity)
    if severity_points is None:
        reasons.append("invalid_severity")
        blocked = True
    else:
        score += severity_points
        if severity_points:
            reasons.append(f"severity_{severity}")

    changed_lines = facts.changed_lines
    if isinstance(changed_lines, bool) or not isinstance(changed_lines, int):
        reasons.append("missing_diff_size" if changed_lines is None else "invalid_diff_size")
        blocked = True
    elif changed_lines < 0:
        reasons.append("invalid_diff_size")
        blocked = True
    else:
        points, reason = _diff_points(changed_lines)
        score += points
        if reason:
            reasons.append(reason)

    if facts.reversible is True:
        pass
    elif facts.reversible is False:
        score += 2
        reasons.append("not_reversible")
    else:
        reasons.append("unknown_reversibility")
        blocked = True

    source_kind = str(facts.source_kind or "").strip().lower()
    if source_kind == "explicit":
        pass
    elif source_kind in _KNOWN_NONEXPLICIT_SOURCES:
        score += 1
        reasons.append("nonexplicit_source")
    else:
        score += 2
        reasons.append("unknown_source")

    return RiskAssessment(
        score=score,
        tier="critical" if blocked else _tier_for_score(score),
        reasons=tuple(reasons),
        blocked=blocked,
    )
