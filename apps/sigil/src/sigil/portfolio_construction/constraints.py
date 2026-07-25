from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .models import ConstraintResult, ConstructionPolicy, PositionProposal


def evaluate_constraints(
    positions: tuple[PositionProposal, ...],
    policy: ConstructionPolicy,
) -> tuple[ConstraintResult, ...]:
    results: list[ConstraintResult] = []
    gross = sum((position.target_weight for position in positions), Decimal("0"))
    results.append(
        ConstraintResult(
            name="gross_exposure",
            passed=gross <= policy.max_gross_exposure,
            observed=str(gross),
            limit=str(policy.max_gross_exposure),
        )
    )

    largest = max(
        (position.target_weight for position in positions),
        default=Decimal("0"),
    )
    results.append(
        ConstraintResult(
            name="position_concentration",
            passed=largest <= policy.max_position_weight,
            observed=str(largest),
            limit=str(policy.max_position_weight),
        )
    )

    issuer_weights: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    sector_weights: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for position in positions:
        issuer_weights[position.issuer] += position.target_weight
        sector_weights[position.sector] += position.target_weight

    largest_issuer = max(issuer_weights.values(), default=Decimal("0"))
    results.append(
        ConstraintResult(
            name="issuer_concentration",
            passed=largest_issuer <= policy.max_issuer_weight,
            observed=str(largest_issuer),
            limit=str(policy.max_issuer_weight),
        )
    )

    largest_sector = max(sector_weights.values(), default=Decimal("0"))
    results.append(
        ConstraintResult(
            name="sector_concentration",
            passed=largest_sector <= policy.max_sector_weight,
            observed=str(largest_sector),
            limit=str(policy.max_sector_weight),
        )
    )

    count = len(positions)
    results.append(
        ConstraintResult(
            name="position_count",
            passed=policy.min_positions <= count <= policy.max_positions,
            observed=str(count),
            limit=f"{policy.min_positions}..{policy.max_positions}",
        )
    )
    return tuple(results)
