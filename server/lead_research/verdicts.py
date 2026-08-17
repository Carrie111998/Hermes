"""Deterministic lead verdicts derived from eligibility, score, and evidence."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import Claim, LeadScore
from .qualification import EligibilityResult


@dataclass(frozen=True)
class Verdict:
    kind: Literal["strong_fit", "review", "reject"]
    reasons: list[str]
    missing_evidence: list[str]
    conflicting_claims: list[str]


@dataclass(frozen=True)
class SourceCoverage:
    official_domains: set[str]
    independent_domains: set[str]


def evaluate_verdict(
    candidate,
    claims: list[Claim],
    score: LeadScore,
    eligibility: EligibilityResult,
    coverage: SourceCoverage,
) -> Verdict:
    """Return a conservative verdict without upgrading hints into evidence."""
    del candidate
    conflicting = sorted({claim.field for claim in claims if claim.status == "conflicted"})
    missing = sorted({
        claim.field
        for claim in claims
        if claim.applicability == "required" and claim.status == "unknown"
    })
    if not coverage.official_domains:
        missing.append("official_source")
    if not coverage.independent_domains:
        missing.append("independent_source")
    if len(coverage.official_domains | coverage.independent_domains) < 2:
        missing.append("second_source")
    missing = sorted(set(missing))

    if not eligibility.eligible:
        return Verdict(
            kind="reject",
            reasons=sorted(set(eligibility.reasons)),
            missing_evidence=missing,
            conflicting_claims=conflicting,
        )
    if score.priority_band == "Rejected":
        return Verdict(
            kind="reject",
            reasons=["below_scoring_threshold"],
            missing_evidence=missing,
            conflicting_claims=conflicting,
        )
    if score.priority_band == "A" and not missing and not conflicting:
        return Verdict(
            kind="strong_fit",
            reasons=["a_band_with_official_and_independent_evidence"],
            missing_evidence=[],
            conflicting_claims=[],
        )
    reasons = [f"priority_band_{score.priority_band.lower()}"]
    if conflicting:
        reasons.append("conflicting_claims")
    if missing:
        reasons.append("additional_evidence_required")
    return Verdict(
        kind="review",
        reasons=reasons,
        missing_evidence=missing,
        conflicting_claims=conflicting,
    )
