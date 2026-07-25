from __future__ import annotations

from decimal import Decimal

from .models import (
    ExecutionComparison,
    GovernedExecutionPackage,
    OrderReconciliation,
)


def _by_client_order_id(
    package: GovernedExecutionPackage,
) -> dict[str, OrderReconciliation]:
    return {
        reconciliation.client_order_id: reconciliation
        for reconciliation in package.reconciliations
    }


def _discrepancy_ids(
    package: GovernedExecutionPackage,
) -> set[str]:
    return {
        discrepancy.discrepancy_id
        for reconciliation in package.reconciliations
        for discrepancy in reconciliation.discrepancies
    }


def compare_execution_packages(
    left: GovernedExecutionPackage,
    right: GovernedExecutionPackage,
) -> ExecutionComparison:
    left_by_id = _by_client_order_id(left)
    right_by_id = _by_client_order_id(right)

    left_ids = set(left_by_id)
    right_ids = set(right_by_id)

    added_ids = tuple(sorted(right_ids - left_ids))
    removed_ids = tuple(sorted(left_ids - right_ids))
    shared_ids = sorted(left_ids & right_ids)

    changed_provider_order_ids: dict[
        str,
        tuple[str | None, str | None],
    ] = {}
    changed_statuses = {}
    changed_filled_quantities: dict[
        str,
        tuple[Decimal, Decimal],
    ] = {}
    changed_average_prices: dict[
        str,
        tuple[Decimal | None, Decimal | None],
    ] = {}
    changed_fees: dict[
        str,
        tuple[Decimal, Decimal],
    ] = {}
    changed_cash_effects: dict[
        str,
        tuple[Decimal, Decimal],
    ] = {}

    for client_order_id in shared_ids:
        left_item = left_by_id[client_order_id]
        right_item = right_by_id[client_order_id]

        if left_item.provider_order_id != right_item.provider_order_id:
            changed_provider_order_ids[client_order_id] = (
                left_item.provider_order_id,
                right_item.provider_order_id,
            )

        if left_item.status is not right_item.status:
            changed_statuses[client_order_id] = (
                left_item.status,
                right_item.status,
            )

        if left_item.filled_quantity != right_item.filled_quantity:
            changed_filled_quantities[client_order_id] = (
                left_item.filled_quantity,
                right_item.filled_quantity,
            )

        if (
            left_item.weighted_average_fill_price
            != right_item.weighted_average_fill_price
        ):
            changed_average_prices[client_order_id] = (
                left_item.weighted_average_fill_price,
                right_item.weighted_average_fill_price,
            )

        if left_item.total_fees != right_item.total_fees:
            changed_fees[client_order_id] = (
                left_item.total_fees,
                right_item.total_fees,
            )

        if left_item.net_cash_effect != right_item.net_cash_effect:
            changed_cash_effects[client_order_id] = (
                left_item.net_cash_effect,
                right_item.net_cash_effect,
            )

    left_discrepancies = _discrepancy_ids(left)
    right_discrepancies = _discrepancy_ids(right)

    return ExecutionComparison(
        left_package_id=left.package_id,
        right_package_id=right.package_id,
        added_client_order_ids=added_ids,
        removed_client_order_ids=removed_ids,
        changed_provider_order_ids=changed_provider_order_ids,
        changed_statuses=changed_statuses,
        changed_filled_quantities=changed_filled_quantities,
        changed_average_prices=changed_average_prices,
        changed_fees=changed_fees,
        changed_cash_effects=changed_cash_effects,
        added_discrepancy_ids=tuple(
            sorted(right_discrepancies - left_discrepancies)
        ),
        removed_discrepancy_ids=tuple(
            sorted(left_discrepancies - right_discrepancies)
        ),
        blockers_changed=left.blockers != right.blockers,
        policy_changed=dict(left.policy_snapshot)
        != dict(right.policy_snapshot),
        reconciliation_status_changed=(
            left.final_status is not right.final_status
        ),
    )
