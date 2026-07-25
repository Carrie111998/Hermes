from __future__ import annotations

from decimal import Decimal

from .models import RebalanceComparison, RebalancePackage


def compare_rebalance_packages(
    left: RebalancePackage,
    right: RebalancePackage,
) -> RebalanceComparison:
    left_values = {
        proposal.symbol: proposal.proposed_value for proposal in left.proposals
    }
    right_values = {
        proposal.symbol: proposal.proposed_value for proposal in right.proposals
    }
    left_symbols = set(left_values)
    right_symbols = set(right_values)
    all_symbols = sorted(left_symbols | right_symbols)
    changes = {
        symbol: right_values.get(symbol, Decimal("0"))
        - left_values.get(symbol, Decimal("0"))
        for symbol in all_symbols
        if right_values.get(symbol, Decimal("0"))
        != left_values.get(symbol, Decimal("0"))
    }
    return RebalanceComparison(
        left_package_id=left.package_id,
        right_package_id=right.package_id,
        added_trade_symbols=tuple(sorted(right_symbols - left_symbols)),
        removed_trade_symbols=tuple(sorted(left_symbols - right_symbols)),
        value_changes=changes,
        status_changed=left.status != right.status,
    )
