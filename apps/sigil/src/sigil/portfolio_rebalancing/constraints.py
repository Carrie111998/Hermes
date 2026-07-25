from __future__ import annotations

from decimal import Decimal

from .models import RebalanceConstraint, RebalancingPolicy, TradeProposal


def evaluate_constraints(
    proposals: tuple[TradeProposal, ...],
    policy: RebalancingPolicy,
) -> tuple[RebalanceConstraint, ...]:
    turnover = sum(
        (abs(proposal.proposed_weight) for proposal in proposals),
        Decimal("0"),
    )
    largest = max(
        (abs(proposal.proposed_weight) for proposal in proposals),
        default=Decimal("0"),
    )
    return (
        RebalanceConstraint(
            name="turnover",
            passed=turnover <= policy.max_turnover_weight,
            observed=str(turnover),
            limit=str(policy.max_turnover_weight),
        ),
        RebalanceConstraint(
            name="single_trade_weight",
            passed=largest <= policy.max_single_trade_weight,
            observed=str(largest),
            limit=str(policy.max_single_trade_weight),
        ),
    )
