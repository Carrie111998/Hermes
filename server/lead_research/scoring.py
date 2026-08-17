"""Fit scoring and evidence confidence remain separate by contract."""
from __future__ import annotations

from collections.abc import Iterable

from .models import Claim, LeadScore, ScoringProfile


SCORE_DIMENSIONS = (
    "product_sector_fit",
    "buyer_channel_fit",
    "buying_intent",
    "market_coverage",
    "commercial_scale",
    "trade_activity",
    "contactability",
)

# Every dimension is earned by a named, evidence-backed claim.  Candidate
# corpus fields are deliberately absent: they are search hints, not evidence.
DIMENSION_CLAIM_FIELDS = {
    "product_sector_fit": (
        "product_sector_fit", "product_fit", "product_term", "hs_code", "sector_ids", "brands_carried",
    ),
    "buyer_channel_fit": ("buyer_channel_fit", "buyer_role", "buyer_type"),
    "buying_intent": ("buying_intent", "procurement_intent", "sourcing_intent", "tender"),
    "market_coverage": ("market_coverage", "locations", "countries_served", "route_to_market"),
    "commercial_scale": (
        "commercial_scale", "store_count", "employee_count", "revenue", "market_cap",
        "reported_company_valuation", "estimated_company_value_range",
    ),
    "trade_activity": ("trade_activity", "relevant_import_value", "relevant_export_value"),
    "contactability": ("contactability", "domain", "email", "phone", "linkedin_url", "contact_channel"),
}
_DIRECT_DIMENSION_FIELDS = frozenset(SCORE_DIMENSIONS)
_SUPPORTED_CLAIM_KINDS = frozenset((
    ("observed", "observed"),
    ("estimated_range", "estimated_range"),
))


def _claim_score(claim: Claim) -> float | None:
    """Return a bounded score only when a claim supplies an observed value."""
    if claim.value is None and claim.low is None and claim.high is None:
        return None
    if claim.field in _DIRECT_DIMENSION_FIELDS:
        if claim.status == "estimated_range":
            if claim.low is None or claim.high is None:
                return None
            value = (claim.low + claim.high) / 2
        else:
            value = claim.value
        if isinstance(value, bool):
            return 100.0 if value else 0.0
        if isinstance(value, (int, float)):
            return float(max(0, min(100, value)))
        return 100.0 if value else 0.0

    value = (claim.low + claim.high) / 2 if claim.status == "estimated_range" else claim.value
    if isinstance(value, bool):
        return 100.0 if value else 0.0
    if isinstance(value, (int, float)):
        return 100.0 if value > 0 else 0.0
    return 100.0 if value else 0.0


def derive_dimension_scores(claims: Iterable[Claim]) -> dict[str, float | None]:
    """Aggregate only supported observations into fit dimensions.

    A missing dimension is intentionally ``None`` rather than an assumed
    baseline.  This makes unknown evidence visible and prevents candidate
    selection hints from changing a lead's fit score.
    """
    field_dimensions = {
        field: dimension
        for dimension, fields in DIMENSION_CLAIM_FIELDS.items()
        for field in fields
    }
    values: dict[str, list[float]] = {key: [] for key in SCORE_DIMENSIONS}
    for claim in claims:
        dimension = field_dimensions.get(claim.field)
        if (
            dimension is None
            or (claim.status, claim.method) not in _SUPPORTED_CLAIM_KINDS
            or not claim.evidence_ids
        ):
            continue
        score = _claim_score(claim)
        if score is not None:
            values[dimension].append(score)
    return {
        key: round(sum(scores) / len(scores), 3) if scores else None
        for key, scores in values.items()
    }


def score_lead(candidate, claims: Iterable[Claim], profile: ScoringProfile) -> LeadScore:
    del candidate
    claims = list(claims)
    dimensions = derive_dimension_scores(claims)
    weights = profile.weights.model_dump()
    known = {key: value for key, value in dimensions.items() if value is not None}
    known_weight = sum(weights[key] for key in known)
    fit = round(sum(known[key] * weights[key] for key in known) / known_weight) if known_weight else 0
    supported_claims = [
        claim for claim in claims
        if (claim.status, claim.method) in _SUPPORTED_CLAIM_KINDS and claim.evidence_ids
    ]
    confidences = [claim.confidence for claim in supported_claims]
    authority = sum(confidences) / len(confidences) if confidences else 0.0
    sources = {evidence for claim in supported_claims for evidence in claim.evidence_ids}
    corroboration = min(1.0, .45 + max(0, len(sources) - 1) * .12) if sources else 0.0
    freshness = .85 if supported_claims else 0.0
    conflict_penalty = sum(1 for claim in claims if claim.status == "conflicted") / max(1, len(claims))
    estimate_share = (
        sum(1 for claim in supported_claims if claim.status == "estimated_range") / len(supported_claims)
        if supported_claims else 0.0
    )
    completeness = len(known) / len(SCORE_DIMENSIONS)
    confidence = max(0, min(
        1,
        authority * .45 + corroboration * .15 + freshness * .2 + completeness * .2
        - conflict_penalty - estimate_share * .15,
    ))
    band = "Rejected"
    for name in ("A", "B", "C"):
        threshold = profile.bands[name]
        if fit >= threshold.min_fit and confidence >= threshold.min_confidence:
            band = name
            break
    return LeadScore(
        fit_score=fit, evidence_confidence=round(confidence, 3), priority_band=band,
        dimensions=dimensions,
        confidence_factors={
            "authority": round(authority, 3), "corroboration": round(corroboration, 3),
            "freshness": round(freshness, 3), "conflict_penalty": conflict_penalty,
            "estimate_share": round(estimate_share, 3), "completeness": round(completeness, 3),
        },
    )
