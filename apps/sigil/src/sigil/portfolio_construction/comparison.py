from __future__ import annotations

from decimal import Decimal

from .models import ConstructionPackage, PackageComparison


def compare_packages(
    left: ConstructionPackage,
    right: ConstructionPackage,
) -> PackageComparison:
    left_weights = {position.symbol: position.target_weight for position in left.positions}
    right_weights = {position.symbol: position.target_weight for position in right.positions}
    left_symbols = set(left_weights)
    right_symbols = set(right_weights)
    all_symbols = sorted(left_symbols | right_symbols)
    changes = {
        symbol: right_weights.get(symbol, Decimal("0"))
        - left_weights.get(symbol, Decimal("0"))
        for symbol in all_symbols
        if right_weights.get(symbol, Decimal("0"))
        != left_weights.get(symbol, Decimal("0"))
    }
    return PackageComparison(
        left_package_id=left.package_id,
        right_package_id=right.package_id,
        added_symbols=tuple(sorted(right_symbols - left_symbols)),
        removed_symbols=tuple(sorted(left_symbols - right_symbols)),
        weight_changes=changes,
        status_changed=left.status != right.status,
    )
