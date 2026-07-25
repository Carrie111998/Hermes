from __future__ import annotations

from .models import (
    OrderIntent,
    OrderIntentComparison,
    OrderIntentPackage,
)


def _intent_map(
    package: OrderIntentPackage,
) -> dict[str, OrderIntent]:
    return {
        intent.symbol: intent
        for intent in package.intents
    }


def compare_order_intent_packages(
    left: OrderIntentPackage,
    right: OrderIntentPackage,
) -> OrderIntentComparison:
    left_by_symbol = _intent_map(left)
    right_by_symbol = _intent_map(right)

    left_symbols = set(left_by_symbol)
    right_symbols = set(right_by_symbol)

    added_symbols = tuple(sorted(right_symbols - left_symbols))
    removed_symbols = tuple(sorted(left_symbols - right_symbols))

    changed_sides = {}
    changed_quantities = {}
    changed_notionals = {}
    changed_order_types = {}
    changed_limit_prices = {}

    for symbol in sorted(left_symbols & right_symbols):
        left_intent = left_by_symbol[symbol]
        right_intent = right_by_symbol[symbol]

        if left_intent.side is not right_intent.side:
            changed_sides[symbol] = (
                left_intent.side,
                right_intent.side,
            )

        if left_intent.quantity != right_intent.quantity:
            changed_quantities[symbol] = (
                left_intent.quantity,
                right_intent.quantity,
            )

        if left_intent.notional != right_intent.notional:
            changed_notionals[symbol] = (
                left_intent.notional,
                right_intent.notional,
            )

        if left_intent.order_type is not right_intent.order_type:
            changed_order_types[symbol] = (
                left_intent.order_type,
                right_intent.order_type,
            )

        if left_intent.limit_price != right_intent.limit_price:
            changed_limit_prices[symbol] = (
                left_intent.limit_price,
                right_intent.limit_price,
            )

    return OrderIntentComparison(
        left_package_id=left.package_id,
        right_package_id=right.package_id,
        added_symbols=added_symbols,
        removed_symbols=removed_symbols,
        changed_sides=changed_sides,
        changed_quantities=changed_quantities,
        changed_notionals=changed_notionals,
        changed_order_types=changed_order_types,
        changed_limit_prices=changed_limit_prices,
        status_changed=left.status is not right.status,
        blockers_changed=left.blockers != right.blockers,
        policy_changed=left.policy_snapshot != right.policy_snapshot,
    )
