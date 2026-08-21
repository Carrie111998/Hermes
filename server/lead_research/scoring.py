"""Fit scoring and evidence confidence remain separate by contract."""
from __future__ import annotations

from collections.abc import Iterable
from time import time

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


# How strongly a piece of evidence carries its dimension. Anchors, at the
# authority a real source carries (.85 independent, .95 official, ~.90 for the
# two agreeing):
#
#   one value, one source        ~50   a mention with nothing corroborating it
#   one value, two sources       ~74   corroborated, but narrow
#   two values, two sources      ~82   corroborated and more than incidental
#   three or more, two sources   ~90   the strongest this evidence model states
#
# Corroboration saturates at two sources deliberately: "an official source and
# an independent one agreeing" is already the standard `evaluate_verdict` uses
# for strong_fit, so the score must not keep paying for a third. Above that
# line it is breadth that separates one lead from another.
CORROBORATED_SOURCES = 2
_BASE_STRENGTH = .55
_CORROBORATION_STRENGTH = .27
_BREADTH_STRENGTH = .18
_BREADTH_SATURATES_AT = 3

# An unvalidated claim — an independent page, a web-search result — is real
# evidence and scores, but it does not carry a validated fact's weight. The
# discount is what makes "validated matters more" arithmetic rather than a
# comment: an unvalidated claim can never outrank a validated one on the same
# dimension, because it is never allowed to be the dimension's anchor while a
# validated claim exists.
_UNVALIDATED_WEIGHT = .75

# A dimension is anchored by its strongest claim; every further claim is
# support, worth a bounded share of the remaining headroom. Support saturates
# at two corroborators for the same reason corroboration does above.
#
# This replaces a mean. Averaging meant a second, weaker claim *lowered* the
# dimension — a company with an official fact plus a corroborating web mention
# scored 54 where the official fact alone scored 78 — so the system was
# penalised for looking harder, and the whole point of a web-search fallback is
# to look harder. Combining is monotonic: a new claim either becomes the anchor
# or adds support, and both only ever raise the score.
_SUPPORT_HEADROOM = .25
_SUPPORT_SATURATES_AT = 2

# How long a fact stays true, by field. Confidence ages each claim against its
# own shelf life rather than against one global window: a founding year does not
# expire, a headcount is good for about a year, an open tender is stale in a
# week. This is what makes a long-lived shared cache safe to score from — a
# cached fact that never ages would keep reporting last year's company.
FACT_TTL_DAYS = {
    "company_name": 3650, "domain": 3650, "registry_id": 3650, "country": 3650,
    "sector_ids": 1095, "hs_code": 1095, "product_term": 730, "product_fit": 730,
    "brands_carried": 365, "certifications": 365, "locations": 365,
    "countries_served": 365, "market_coverage": 365, "buyer_role": 365,
    "buyer_type": 365, "employee_count": 365, "store_count": 365,
    "revenue": 550, "market_cap": 180, "relevant_import_value": 550,
    "relevant_export_value": 550, "lifecycle_status": 180, "email": 365,
    "phone": 365, "linkedin_url": 365, "tender": 30,
    "procurement_intent": 60, "sourcing_intent": 60, "buying_intent": 60,
}
_DEFAULT_TTL_DAYS = 180
_DAY_SECONDS = 86400.0
# What freshness reports when no claim carries a retrieval time. The old code
# hardcoded this for every lead and called it freshness; it survives only as the
# not-measured fallback, so pre-existing claims score exactly as they did.
_FRESHNESS_UNKNOWN = .85


def _distinct_values(claim: Claim) -> int:
    if isinstance(claim.value, list):
        return len({str(item) for item in claim.value if str(item).strip()})
    return 1 if claim.value is not None and str(claim.value).strip() else 0


def _evidence_strength(claim: Claim) -> float:
    """Degree of support, in [BASE, 1], from corroboration and breadth."""
    corroborated = len(set(claim.evidence_ids)) >= CORROBORATED_SOURCES
    breadth = min(
        1.0,
        max(0, _distinct_values(claim) - 1) / (_BREADTH_SATURATES_AT - 1),
    )
    return (
        _BASE_STRENGTH
        + _CORROBORATION_STRENGTH * (1.0 if corroborated else 0.0)
        + _BREADTH_STRENGTH * breadth
    )


def _claim_score(claim: Claim) -> float | None:
    """Return a bounded score only when a claim supplies an observed value.

    Two different kinds of claim arrive here. A claim whose field *is* a
    dimension is a provider stating that dimension's score, so its value is
    respected. Everything else is evidence *of* a dimension — a matched product
    term, an observed buyer role — and is scored by degree.

    Degree is the point. This used to return 100.0 for any truthy value, so a
    single term found in a single search snippet scored the same as a company
    corroborated across four sources, and because `score_lead` divides by the
    weight of the dimensions it actually has, nearly every lead came out at fit
    100. Two real runs produced 91 and 173 leads, every one of them fit 100 and
    `review`: a ranked list with nothing to rank by.
    """
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
    if isinstance(value, (int, float)) and value <= 0:
        return 0.0
    if not isinstance(value, (int, float)) and not value:
        return 0.0
    # ponytail: magnitude is not read for numeric evidence fields (store_count,
    # revenue) — no verifier emits one today, so a scale curve would be tuned
    # against nothing. Add one when a provider starts supplying them.
    return round(min(100.0, 100.0 * claim.confidence * _evidence_strength(claim)), 3)


def _combine(scored: list[tuple[float, bool]]) -> float:
    """One dimension score from its claims: anchor plus bounded support.

    `scored` is (score, validated) per claim. The anchor is the strongest
    validated claim when there is one, so an unvalidated claim can raise a
    dimension but never define it — which is what "validated counts for more"
    has to mean if it is to survive a web-search fallback that will always
    produce more claims than the validated sources do.

    Monotonic by construction: every claim is either the anchor or a supporter,
    and neither role can reduce the result.
    """
    validated = [score for score, is_validated in scored if is_validated]
    anchor = max(validated) if validated else max(
        score * _UNVALIDATED_WEIGHT for score, _ in scored
    )
    supporters = len(scored) - 1
    support = min(1.0, supporters / _SUPPORT_SATURATES_AT)
    return round(min(100.0, anchor + (100.0 - anchor) * _SUPPORT_HEADROOM * support), 3)


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
    values: dict[str, list[tuple[float, bool]]] = {key: [] for key in SCORE_DIMENSIONS}
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
            values[dimension].append((score, claim.validated))
    return {
        key: _combine(scored) if scored else None
        for key, scored in values.items()
    }


def claim_freshness(claims: Iterable[Claim], at: float) -> float | None:
    """How much of a claim set's shelf life is left, in [0, 1].

    None when nothing carries a retrieval time, which is not the same as stale
    and must not be scored as fresh either. Each claim ages against its own
    field's TTL, so a shared cache holding a permanent fact and a weekly one
    does not have to choose a single window for both.
    """
    remaining = []
    for claim in claims:
        if claim.observed_at is None:
            continue
        ttl = FACT_TTL_DAYS.get(claim.field, _DEFAULT_TTL_DAYS) * _DAY_SECONDS
        age = max(0.0, at - claim.observed_at)
        remaining.append(max(0.0, 1.0 - age / ttl))
    return sum(remaining) / len(remaining) if remaining else None


def attainable_dimensions(emitted_fields: Iterable[str]) -> set[str]:
    """Dimensions the configured sources could speak to at all.

    Derived from what each source declares it emits, so a dimension nothing can
    reach is not counted against a lead. Four of the seven dimensions —
    buying_intent, market_coverage, commercial_scale, trade_activity — have no
    field that any shipped verifier produces, so with a fixed denominator of
    seven no company could exceed 3/7 completeness and every lead's confidence
    was understated by the same fixed amount. Band A's threshold was then only
    just clearable, and a company with no website could not clear it at all.
    """
    fields = set(emitted_fields)
    return {
        dimension
        for dimension, dimension_fields in DIMENSION_CLAIM_FIELDS.items()
        if fields.intersection(dimension_fields)
    }


def score_lead(
    candidate,
    claims: Iterable[Claim],
    profile: ScoringProfile,
    attainable: set[str] | None = None,
    at: float | None = None,
) -> LeadScore:
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
    # Measured, not assumed. This was a constant .85 for every lead that had any
    # claim at all, which is the one factor a long-lived shared cache makes
    # dangerous: a fact cached last year scored as fresh as one fetched this
    # morning. `_FRESHNESS_UNKNOWN` keeps the old value for claims with no
    # retrieval time, so nothing already stored changes score.
    measured = claim_freshness(supported_claims, at if at is not None else time())
    freshness = (
        (_FRESHNESS_UNKNOWN if measured is None else measured)
        if supported_claims else 0.0
    )
    conflict_penalty = sum(1 for claim in claims if claim.status == "conflicted") / max(1, len(claims))
    estimate_share = (
        sum(1 for claim in supported_claims if claim.status == "estimated_range") / len(supported_claims)
        if supported_claims else 0.0
    )
    # Measured against what the configured sources could supply, not against all
    # seven dimensions. `known` is unioned in so a source emitting something it
    # never declared can only ever help: an undeclared fact adds to both sides
    # rather than being dropped from the numerator. An empty declaration means
    # nobody said anything, which falls back to the full set rather than
    # silently scoring a lead as complete on the strength of one claim.
    denominator = (attainable | set(known)) if attainable else set(SCORE_DIMENSIONS)
    completeness = len(known) / len(denominator) if denominator else 0.0
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
