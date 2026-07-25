from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

from .models import CandidateAsset, ConstructionPolicy


def candidate_rejection_reasons(
    candidate: CandidateAsset,
    policy: ConstructionPolicy,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not candidate.approved:
        reasons.append("candidate is not approved")
    if candidate.liquidity_score < policy.min_liquidity_score:
        reasons.append("liquidity score is below policy minimum")
    if candidate.volatility > policy.max_asset_volatility:
        reasons.append("volatility exceeds policy maximum")
    if policy.require_evidence_references:
        if not candidate.thesis_reference:
            reasons.append("missing thesis reference")
        if not candidate.valuation_reference:
            reasons.append("missing valuation reference")
        if not candidate.risk_reference:
            reasons.append("missing risk reference")
    return tuple(reasons)


def policy_snapshot(policy: ConstructionPolicy) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for key, value in asdict(policy).items():
        snapshot[key] = str(value) if isinstance(value, Decimal) else repr(value)
    return snapshot
