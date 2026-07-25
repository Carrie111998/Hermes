from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from decimal import Decimal, ROUND_DOWN

from .audit import package_identity
from .constraints import evaluate_constraints
from .input import normalize_candidates, validate_capital
from .models import (
    AllocationMethod,
    CandidateAsset,
    ConstructionPackage,
    ConstructionPolicy,
    ConstructionStatus,
    Exclusion,
    PositionProposal,
)
from .policy import candidate_rejection_reasons, policy_snapshot


def _quantum(precision: int) -> Decimal:
    return Decimal("1").scaleb(-precision)


def _eligible_and_excluded(
    candidates: tuple[CandidateAsset, ...],
    policy: ConstructionPolicy,
) -> tuple[tuple[CandidateAsset, ...], tuple[Exclusion, ...]]:
    eligible: list[CandidateAsset] = []
    exclusions: list[Exclusion] = []
    for candidate in candidates:
        reasons = candidate_rejection_reasons(candidate, policy)
        if reasons:
            exclusions.append(Exclusion(symbol=candidate.symbol, reasons=reasons))
        else:
            eligible.append(candidate)
    eligible.sort(key=lambda item: (-item.score, item.symbol))
    return tuple(eligible[: policy.max_positions]), tuple(exclusions)


def _raw_weights(
    candidates: tuple[CandidateAsset, ...],
    method: AllocationMethod,
) -> dict[str, Decimal]:
    if not candidates:
        return {}
    if method is AllocationMethod.EQUAL_WEIGHT:
        unit = Decimal("1") / Decimal(len(candidates))
        return {candidate.symbol: unit for candidate in candidates}
    score_total = sum((candidate.score for candidate in candidates), Decimal("0"))
    if score_total == 0:
        unit = Decimal("1") / Decimal(len(candidates))
        return {candidate.symbol: unit for candidate in candidates}
    return {candidate.symbol: candidate.score / score_total for candidate in candidates}


def _apply_caps(
    candidates: tuple[CandidateAsset, ...],
    raw: dict[str, Decimal],
    policy: ConstructionPolicy,
    investable_weight: Decimal,
) -> dict[str, Decimal]:
    weights = {symbol: weight * investable_weight for symbol, weight in raw.items()}
    issuer_used: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    sector_used: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    final: dict[str, Decimal] = {}

    for candidate in candidates:
        requested = weights[candidate.symbol]
        allowed = min(
            requested,
            policy.max_position_weight,
            policy.max_issuer_weight - issuer_used[candidate.issuer],
            policy.max_sector_weight - sector_used[candidate.sector],
        )
        allowed = max(allowed, Decimal("0"))
        final[candidate.symbol] = allowed
        issuer_used[candidate.issuer] += allowed
        sector_used[candidate.sector] += allowed
    return final


def construct_portfolio(
    candidates: tuple[CandidateAsset, ...] | list[CandidateAsset],
    *,
    capital: Decimal,
    method: AllocationMethod,
    policy: ConstructionPolicy | None = None,
) -> ConstructionPackage:
    active_policy = policy or ConstructionPolicy()
    normalized = normalize_candidates(candidates)
    validated_capital = validate_capital(capital)
    eligible, exclusions = _eligible_and_excluded(normalized, active_policy)
    investable_weight = min(
        active_policy.max_gross_exposure - active_policy.cash_reserve_weight,
        Decimal("1") - active_policy.cash_reserve_weight,
    )

    raw = _raw_weights(eligible, method)
    if method is AllocationMethod.CONSTRAINED_SCORE_WEIGHTED:
        allocated = _apply_caps(eligible, raw, active_policy, investable_weight)
    else:
        allocated = {
            symbol: min(weight * investable_weight, active_policy.max_position_weight)
            for symbol, weight in raw.items()
        }

    quantum = _quantum(active_policy.weight_precision)
    candidate_by_symbol = {candidate.symbol: candidate for candidate in eligible}
    positions: list[PositionProposal] = []
    for symbol in sorted(allocated):
        weight = allocated[symbol].quantize(quantum, rounding=ROUND_DOWN)
        if weight <= 0:
            continue
        candidate = candidate_by_symbol[symbol]
        target_value = (validated_capital * weight).quantize(
            Decimal("0.01"),
            rounding=ROUND_DOWN,
        )
        shares = (target_value / candidate.price).quantize(
            Decimal("0.000001"),
            rounding=ROUND_DOWN,
        )
        positions.append(
            PositionProposal(
                symbol=symbol,
                target_weight=weight,
                target_value=target_value,
                estimated_shares=shares,
                issuer=candidate.issuer,
                sector=candidate.sector,
                score=candidate.score,
                rationale=(
                    f"selected by {method.value}",
                    f"score={candidate.score}",
                    f"liquidity={candidate.liquidity_score}",
                    "weight is a proposal, not an execution instruction",
                ),
            )
        )

    position_tuple = tuple(positions)
    constraints = evaluate_constraints(position_tuple, active_policy)
    blockers = [result.name for result in constraints if not result.passed]
    if len(position_tuple) < active_policy.min_positions:
        blockers.append("insufficient eligible candidates")
    invested_weight = sum(
        (position.target_weight for position in position_tuple),
        Decimal("0"),
    )
    cash_weight = Decimal("1") - invested_weight
    warnings: list[str] = []
    if invested_weight < investable_weight:
        warnings.append("constraints left part of the portfolio unallocated")
    evidence = sorted(
        {
            reference
            for candidate in eligible
            for reference in (
                candidate.thesis_reference,
                candidate.valuation_reference,
                candidate.risk_reference,
            )
            if reference
        }
    )

    unsigned = ConstructionPackage(
        package_id="",
        method=method,
        status=ConstructionStatus.BLOCKED if blockers else ConstructionStatus.READY,
        capital=validated_capital,
        invested_weight=invested_weight,
        cash_weight=cash_weight,
        positions=position_tuple,
        exclusions=exclusions,
        constraints=constraints,
        blockers=tuple(sorted(set(blockers))),
        warnings=tuple(warnings),
        evidence_references=tuple(evidence),
        policy_snapshot=policy_snapshot(active_policy),
    )
    identity = package_identity(unsigned)
    return replace(unsigned, package_id=identity)
