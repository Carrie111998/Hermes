from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, ROUND_DOWN

from .audit import package_identity
from .constraints import evaluate_constraints
from .input import normalize_current_positions, normalize_target_positions
from .models import (
    CurrentPosition,
    RebalanceAction,
    RebalancePackage,
    RebalancingPolicy,
    RebalanceStatus,
    TargetPosition,
    TradeProposal,
)
from .policy import policy_snapshot


def _quantum(precision: int) -> Decimal:
    return Decimal("1").scaleb(-precision)


def build_rebalance_package(
    current_positions: tuple[CurrentPosition, ...] | list[CurrentPosition],
    target_positions: tuple[TargetPosition, ...] | list[TargetPosition],
    *,
    source_target_package_id: str,
    target_package_approved: bool,
    cash_value: Decimal = Decimal("0"),
    policy: RebalancingPolicy | None = None,
    evidence_references: tuple[str, ...] = (),
) -> RebalancePackage:
    active_policy = policy or RebalancingPolicy()
    current = normalize_current_positions(current_positions)
    targets = normalize_target_positions(target_positions)

    if cash_value < 0:
        raise ValueError("cash_value must be non-negative")
    if not source_target_package_id.strip():
        raise ValueError("source_target_package_id must not be empty")

    current_by_symbol = {position.symbol: position for position in current}
    target_by_symbol = {position.symbol: position for position in targets}
    portfolio_value = cash_value + sum(
        (position.market_value for position in current),
        Decimal("0"),
    )
    if portfolio_value <= 0:
        raise ValueError("portfolio value must be positive")

    blockers: list[str] = []
    warnings: list[str] = []
    if active_policy.require_approved_target_package and not target_package_approved:
        blockers.append("target portfolio package is not approved")

    weight_quantum = _quantum(active_policy.weight_precision)
    share_quantum = _quantum(active_policy.share_precision)
    proposals: list[TradeProposal] = []

    for symbol in sorted(set(current_by_symbol) | set(target_by_symbol)):
        current_position = current_by_symbol.get(symbol)
        target_position = target_by_symbol.get(symbol)

        current_value = (
            current_position.market_value if current_position else Decimal("0")
        )
        current_weight = (current_value / portfolio_value).quantize(
            weight_quantum,
            rounding=ROUND_DOWN,
        )
        target_weight = (
            target_position.target_weight if target_position else Decimal("0")
        ).quantize(weight_quantum, rounding=ROUND_DOWN)
        drift = target_weight - current_weight

        if abs(drift) <= active_policy.drift_tolerance:
            continue
        if current_position is None and not active_policy.allow_new_positions:
            warnings.append(f"{symbol}: new positions are disabled")
            continue
        if target_position is None and not active_policy.allow_full_exits:
            warnings.append(f"{symbol}: full exits are disabled")
            continue

        bounded = max(
            -active_policy.max_single_trade_weight,
            min(drift, active_policy.max_single_trade_weight),
        )
        proposed_value = (abs(bounded) * portfolio_value).quantize(
            Decimal("0.01"),
            rounding=ROUND_DOWN,
        )
        if proposed_value < active_policy.minimum_trade_value:
            warnings.append(f"{symbol}: trade is below minimum value")
            continue

        price = (
            current_position.price
            if current_position is not None
            else target_position.reference_price
        )
        proposed_quantity = (proposed_value / price).quantize(
            share_quantum,
            rounding=ROUND_DOWN,
        )
        action = RebalanceAction.BUY if bounded > 0 else RebalanceAction.SELL
        issuer = (
            target_position.issuer
            if target_position is not None
            else current_position.issuer
        )
        sector = (
            target_position.sector
            if target_position is not None
            else current_position.sector
        )
        proposals.append(
            TradeProposal(
                symbol=symbol,
                action=action,
                current_weight=current_weight,
                target_weight=target_weight,
                drift_weight=drift,
                proposed_weight=bounded,
                proposed_value=proposed_value,
                proposed_quantity=proposed_quantity,
                price=price,
                issuer=issuer,
                sector=sector,
                rationale=(
                    f"current_weight={current_weight}",
                    f"target_weight={target_weight}",
                    f"drift={drift}",
                    "proposal is analytical and requires downstream approval",
                ),
            )
        )

    proposal_tuple = tuple(proposals)
    constraints = evaluate_constraints(proposal_tuple, active_policy)
    blockers.extend(result.name for result in constraints if not result.passed)

    turnover = sum(
        (abs(proposal.proposed_weight) for proposal in proposal_tuple),
        Decimal("0"),
    )
    if blockers:
        status = RebalanceStatus.BLOCKED
    elif not proposal_tuple:
        status = RebalanceStatus.NO_ACTION
    else:
        status = RebalanceStatus.READY

    unsigned = RebalancePackage(
        package_id="",
        source_target_package_id=source_target_package_id,
        status=status,
        portfolio_value=portfolio_value,
        proposed_turnover_weight=turnover,
        proposals=proposal_tuple,
        constraints=constraints,
        blockers=tuple(sorted(set(blockers))),
        warnings=tuple(sorted(set(warnings))),
        policy_snapshot=policy_snapshot(active_policy),
        evidence_references=tuple(sorted(set(evidence_references))),
    )
    return replace(unsigned, package_id=package_identity(unsigned))
