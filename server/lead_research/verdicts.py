"""Deterministic lead verdicts derived from eligibility, score, and evidence."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .models import Claim, LeadScore
from .qualification import EligibilityResult


@dataclass(frozen=True)
class Verdict:
    kind: Literal["strong_fit", "review", "reject"]
    reasons: list[str]
    missing_evidence: list[str]
    conflicting_claims: list[str]


# Facts that end an assessment rather than lowering it. A company that has
# closed is not a weak lead, and no amount of product fit makes it a lead at
# all — so this is a veto, not a subtraction that leaves it ranked fourth.
#
# It lives here rather than only in eligibility because eligibility is
# configurable: `exclude_inactive` can be switched off, and closure is not a
# preference a tenant should be able to switch off. The gate still reports it
# too, so the reason a lead was dropped stays legible in both places.
HARD_NEGATIVE_CLAIMS = {"lifecycle_status": {"closed", "dissolved", "liquidated"}}


def terminal_value(field: str, values) -> str | None:
    """A named reason when one of `values` ends the assessment, else None.

    Takes a field and raw values rather than a Claim so the same test can run
    on evidence before claims exist — which is what lets a run skip paying for
    deep research on a company that has already closed.
    """
    terminal = HARD_NEGATIVE_CLAIMS.get(field)
    if not terminal:
        return None
    for value in (values if isinstance(values, list) else [values]):
        folded = str(value).strip().casefold()
        if folded in terminal:
            return f"{field}_{folded}"
    return None


def hard_negative(claims: list[Claim]) -> str | None:
    """The first terminal fact in a claim set, or None."""
    for claim in claims:
        if claim.status not in {"observed", "conflicted"}:
            continue
        reason = terminal_value(claim.field, claim.value)
        if reason:
            return reason
    return None


@dataclass(frozen=True)
class SourceCoverage:
    """Which publishers vouched for a company, and in what capacity.

    `official` and `independent` answer *who published it* — the company itself,
    or somebody else. `registry` is orthogonal to both and answers *what
    standing the publisher has*: a TED award notice is published by the EU's
    Publications Office, so its domain is independent and authoritative at the
    same time. It is declared by the source in the provider catalog rather than
    guessed per page, because authority is a property of the publisher.
    """

    official_domains: set[str]
    independent_domains: set[str]
    registry_domains: set[str] = field(default_factory=set)

    @property
    def all_domains(self) -> set[str]:
        return self.official_domains | self.independent_domains | self.registry_domains

    @property
    def has_authority(self) -> bool:
        """Whether anything with standing vouched for this company.

        The company's own page or an authoritative registry both qualify.
        Requiring specifically the company's own page made `strong_fit`
        unreachable for any company whose website was not already in the
        corpus: `official` is only ever produced by fetching a *known* domain,
        so 161 of the 201 TED-derived rows had no path to it at all, however
        much evidence they accumulated. That capped a verdict on corpus
        metadata rather than on evidence.
        """
        return bool(self.official_domains or self.registry_domains)


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
    # Reported in full, whatever the verdict: these are what a reader should
    # know is still absent, and a `strong_fit` that hides its own gaps is how a
    # lead list stops being auditable.
    if not coverage.has_authority:
        missing.append("authoritative_source")
    if not coverage.official_domains:
        missing.append("official_source")
    if not coverage.independent_domains:
        missing.append("independent_source")
    if len(coverage.all_domains) < 2:
        missing.append("second_source")
    missing = sorted(set(missing))
    # What actually disqualifies a strong verdict, as opposed to what is merely
    # absent. One authoritative publisher, and a second publisher agreeing.
    corroborated = coverage.has_authority and len(coverage.all_domains) >= 2

    # Before eligibility and before the score: nothing downstream can make a
    # company that no longer exists worth contacting.
    terminal = hard_negative(claims)
    if terminal:
        return Verdict(
            kind="reject",
            reasons=[terminal],
            missing_evidence=missing,
            conflicting_claims=conflicting,
        )
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
    if score.priority_band == "A" and corroborated and not conflicting:
        return Verdict(
            kind="strong_fit",
            reasons=[
                "a_band_with_official_and_corroborating_evidence"
                if coverage.official_domains
                else "a_band_with_registry_and_corroborating_evidence"
            ],
            missing_evidence=missing,
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
