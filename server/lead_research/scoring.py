"""Fit scoring and evidence confidence remain separate by contract."""
from __future__ import annotations

from collections.abc import Iterable

from .models import Claim, LeadScore, ScoringProfile


DIMENSIONS = {
    "product_sector_fit": 88,
    "buyer_channel_fit": 82,
    "buying_intent": 64,
    "market_coverage": 72,
    "commercial_scale": 66,
    "trade_activity": 58,
    "contactability": 35,
}


def score_lead(candidate, claims: Iterable[Claim], profile: ScoringProfile) -> LeadScore:
    data = candidate if isinstance(candidate, dict) else getattr(candidate, "__dict__", {})
    provided = data.get("dimension_scores") or {}
    dimensions = {key: float(provided.get(key, value)) for key, value in DIMENSIONS.items()}
    weights = profile.weights.model_dump()
    fit = round(sum(dimensions[key] * weights[key] for key in weights) / 100)
    claims = list(claims)
    confidences = [claim.confidence for claim in claims if claim.status not in {"unknown", "not_applicable"}]
    authority = sum(confidences) / len(confidences) if confidences else float(data.get("evidence_confidence", .35))
    sources = {evidence for claim in claims for evidence in claim.evidence_ids}
    corroboration = min(1.0, .45 + max(0, len(sources) - 1) * .12)
    freshness = float(data.get("freshness", .85))
    conflict_penalty = float(data.get("conflict_penalty", 0))
    estimate_share = (sum(1 for claim in claims if claim.status == "estimated_range") / len(claims)) if claims else 0
    confidence = max(0, min(1, authority * .55 + corroboration * .2 + freshness * .25 - conflict_penalty - estimate_share * .15))
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
            "estimate_share": round(estimate_share, 3),
        },
    )
