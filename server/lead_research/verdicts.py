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


# The absolute quality floor a band-A lead must clear to be a strong fit.
#
# It replaces "an authoritative publisher plus a second one agreeing", which
# was a statement about publishers rather than about evidence. These four are
# properties of what is known about the company:
#
#   confidence   how much the evidence behind the claims is worth
#   known_weight how much of the scoring model was actually answered — `fit` is
#                a weighted mean over the dimensions a lead *has*, so one
#                dimension out of seven can read 95 and mean almost nothing
#   dimensions   both scored dimensions any shipped verifier can reach must be
#                present; a lead with no buyer evidence is not a buyer
#   conflicts    a live disagreement about what the verdict rests on
#
# A floor, not a target. Nothing below it is promoted to fill a result list.
STRONG_FIT_MIN_CONFIDENCE = .60
STRONG_FIT_MIN_KNOWN_WEIGHT = 50
STRONG_FIT_REQUIRED_DIMENSIONS = frozenset({
    "product_sector_fit", "buyer_channel_fit",
})
# Which conflicts are material. A disagreement about a phone number is not the
# same as one about the company's country: blocking on any conflicted field at
# all made every argument equally disqualifying.
MATERIAL_CONFLICT_FIELDS = frozenset({
    "company_name", "country", "domain", "product_sector_fit",
    "product_term", "buyer_channel_fit", "buyer_role",
})


def strong_fit_floor(score: LeadScore, claims: list[Claim]) -> list[str]:
    """Named reasons this lead is below the strong-fit floor; empty means clear."""
    reasons: list[str] = []
    if score.evidence_confidence < STRONG_FIT_MIN_CONFIDENCE:
        reasons.append("insufficient_evidence_confidence")
    if score.known_weight < STRONG_FIT_MIN_KNOWN_WEIGHT:
        reasons.append("insufficient_answered_weight")
    for dimension in sorted(STRONG_FIT_REQUIRED_DIMENSIONS):
        if score.dimensions.get(dimension) is None:
            reasons.append(f"{dimension}_required")
    if any(
        claim.status == "conflicted" and claim.field in MATERIAL_CONFLICT_FIELDS
        for claim in claims
    ):
        reasons.append("material_conflict")
    return reasons


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
    floor_reasons = strong_fit_floor(score, claims)
    if score.priority_band == "A" and not floor_reasons:
        return Verdict(
            kind="strong_fit",
            reasons=["a_band_above_absolute_quality_floor"],
            missing_evidence=missing,
            conflicting_claims=conflicting,
        )
    reasons = [f"priority_band_{score.priority_band.lower()}"]
    reasons.extend(floor_reasons)
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
