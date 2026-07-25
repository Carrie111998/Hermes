from __future__ import annotations

from collections import Counter
from decimal import Decimal

from .audit import deterministic_identifier
from .models import (
    BrokerOrderSnapshot,
    BrokerOrderStatus,
    DiscrepancyCategory,
    DiscrepancySeverity,
    ExecutionFill,
    FillReconciliationStatus,
    OrderReconciliation,
    OrderSide,
    ReconciliationDiscrepancy,
    SubmissionAcknowledgement,
    SubmissionRequest,
)
from .policy import ExecutionPolicy


def _discrepancy(
    *,
    request: SubmissionRequest,
    category: DiscrepancyCategory,
    severity: DiscrepancySeverity,
    message: str,
    expected: str | None,
    observed: str | None,
    evidence_references: tuple[str, ...] = (),
) -> ReconciliationDiscrepancy:
    discrepancy_id = deterministic_identifier(
        "execution-discrepancy",
        request.client_order_id,
        category,
        severity,
        message,
        expected,
        observed,
        evidence_references,
    )

    return ReconciliationDiscrepancy(
        discrepancy_id=discrepancy_id,
        category=category,
        severity=severity,
        message=message,
        expected=expected,
        observed=observed,
        evidence_references=evidence_references,
    )


def _quantity_equal(
    left: Decimal,
    right: Decimal,
    policy: ExecutionPolicy,
) -> bool:
    return abs(left - right) <= policy.quantity_tolerance


def _price_equal(
    left: Decimal | None,
    right: Decimal | None,
    policy: ExecutionPolicy,
) -> bool:
    if left is None or right is None:
        return left is right
    return abs(left - right) <= policy.price_tolerance


def _weighted_average_price(
    fills: tuple[ExecutionFill, ...],
) -> Decimal | None:
    total_quantity = sum(
        (fill.quantity for fill in fills),
        Decimal("0"),
    )

    if total_quantity <= 0:
        return None

    gross_notional = sum(
        (fill.quantity * fill.price for fill in fills),
        Decimal("0"),
    )

    return gross_notional / total_quantity


def _fill_status(
    *,
    requested_quantity: Decimal,
    filled_quantity: Decimal,
    broker_status: BrokerOrderStatus | None,
    policy: ExecutionPolicy,
) -> FillReconciliationStatus:
    if filled_quantity > requested_quantity + policy.quantity_tolerance:
        return FillReconciliationStatus.OVERFILLED

    if _quantity_equal(
        filled_quantity,
        requested_quantity,
        policy,
    ):
        return FillReconciliationStatus.FULLY_FILLED

    if filled_quantity > 0:
        return FillReconciliationStatus.PARTIALLY_FILLED

    if broker_status is BrokerOrderStatus.REJECTED:
        return FillReconciliationStatus.FAILED

    if broker_status in {
        BrokerOrderStatus.CANCELLED,
        BrokerOrderStatus.EXPIRED,
    }:
        return FillReconciliationStatus.NOT_FILLED

    if broker_status in {
        BrokerOrderStatus.ACCEPTED,
        BrokerOrderStatus.PENDING,
        BrokerOrderStatus.PARTIALLY_FILLED,
    }:
        return FillReconciliationStatus.PENDING

    if broker_status is BrokerOrderStatus.UNKNOWN:
        return FillReconciliationStatus.UNKNOWN

    return FillReconciliationStatus.NOT_FILLED


def reconcile_order(
    *,
    request: SubmissionRequest,
    acknowledgement: SubmissionAcknowledgement | None,
    snapshot: BrokerOrderSnapshot | None,
    fills: tuple[ExecutionFill, ...],
    policy: ExecutionPolicy,
) -> OrderReconciliation:
    discrepancies: list[ReconciliationDiscrepancy] = []
    blockers: list[str] = []
    warnings: list[str] = []
    evidence: set[str] = set(request.evidence_references)

    if acknowledgement is None:
        discrepancy = _discrepancy(
            request=request,
            category=DiscrepancyCategory.MISSING_ACKNOWLEDGEMENT,
            severity=DiscrepancySeverity.BLOCKING,
            message="No provider acknowledgement was supplied",
            expected=request.client_order_id,
            observed=None,
            evidence_references=request.evidence_references,
        )
        discrepancies.append(discrepancy)
        blockers.append(discrepancy.message)
        provider_order_id = None
        broker_status = None
    else:
        evidence.update(acknowledgement.evidence_references)
        provider_order_id = acknowledgement.provider_order_id
        broker_status = acknowledgement.status

        if acknowledgement.client_order_id != request.client_order_id:
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.APPROVAL_MISMATCH,
                severity=DiscrepancySeverity.BLOCKING,
                message="Acknowledgement client order identifier mismatch",
                expected=request.client_order_id,
                observed=acknowledgement.client_order_id,
                evidence_references=acknowledgement.evidence_references,
            )
            discrepancies.append(discrepancy)
            blockers.append(discrepancy.message)

        if acknowledgement.request_id != request.request_id:
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.APPROVAL_MISMATCH,
                severity=DiscrepancySeverity.BLOCKING,
                message="Acknowledgement request identifier mismatch",
                expected=request.request_id,
                observed=acknowledgement.request_id,
                evidence_references=acknowledgement.evidence_references,
            )
            discrepancies.append(discrepancy)
            blockers.append(discrepancy.message)

        if acknowledgement.status is BrokerOrderStatus.REJECTED:
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.REJECTED_ORDER,
                severity=DiscrepancySeverity.BLOCKING,
                message="Provider rejected the order",
                expected="accepted or filled",
                observed=acknowledgement.status.value,
                evidence_references=acknowledgement.evidence_references,
            )
            discrepancies.append(discrepancy)
            blockers.append(discrepancy.message)

    if snapshot is not None:
        evidence.update(snapshot.evidence_references)
        broker_status = snapshot.status

        if provider_order_id is None:
            provider_order_id = snapshot.provider_order_id

        if snapshot.client_order_id != request.client_order_id:
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.UNKNOWN_PROVIDER_ORDER,
                severity=DiscrepancySeverity.BLOCKING,
                message="Broker snapshot client order identifier mismatch",
                expected=request.client_order_id,
                observed=snapshot.client_order_id,
                evidence_references=snapshot.evidence_references,
            )
            discrepancies.append(discrepancy)
            blockers.append(discrepancy.message)

        if snapshot.symbol != request.symbol:
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.SYMBOL_MISMATCH,
                severity=DiscrepancySeverity.BLOCKING,
                message="Broker snapshot symbol mismatch",
                expected=request.symbol,
                observed=snapshot.symbol,
                evidence_references=snapshot.evidence_references,
            )
            discrepancies.append(discrepancy)
            blockers.append(discrepancy.message)

        if snapshot.side is not request.side:
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.SIDE_MISMATCH,
                severity=DiscrepancySeverity.BLOCKING,
                message="Broker snapshot side mismatch",
                expected=request.side.value,
                observed=snapshot.side.value,
                evidence_references=snapshot.evidence_references,
            )
            discrepancies.append(discrepancy)
            blockers.append(discrepancy.message)

        if snapshot.order_type is not request.order_type:
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.ORDER_TYPE_MISMATCH,
                severity=DiscrepancySeverity.BLOCKING,
                message="Broker snapshot order type mismatch",
                expected=request.order_type.value,
                observed=snapshot.order_type.value,
                evidence_references=snapshot.evidence_references,
            )
            discrepancies.append(discrepancy)
            blockers.append(discrepancy.message)

        if snapshot.time_in_force is not request.time_in_force:
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.TIME_IN_FORCE_MISMATCH,
                severity=DiscrepancySeverity.BLOCKING,
                message="Broker snapshot time in force mismatch",
                expected=request.time_in_force.value,
                observed=snapshot.time_in_force.value,
                evidence_references=snapshot.evidence_references,
            )
            discrepancies.append(discrepancy)
            blockers.append(discrepancy.message)

        if not _quantity_equal(
            snapshot.requested_quantity,
            request.quantity,
            policy,
        ):
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.QUANTITY_MISMATCH,
                severity=DiscrepancySeverity.BLOCKING,
                message="Broker snapshot requested quantity mismatch",
                expected=str(request.quantity),
                observed=str(snapshot.requested_quantity),
                evidence_references=snapshot.evidence_references,
            )
            discrepancies.append(discrepancy)
            blockers.append(discrepancy.message)

        if not _price_equal(
            snapshot.limit_price,
            request.limit_price,
            policy,
        ):
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.LIMIT_PRICE_MISMATCH,
                severity=DiscrepancySeverity.BLOCKING,
                message="Broker snapshot limit price mismatch",
                expected=str(request.limit_price),
                observed=str(snapshot.limit_price),
                evidence_references=snapshot.evidence_references,
            )
            discrepancies.append(discrepancy)
            blockers.append(discrepancy.message)

        if snapshot.status is BrokerOrderStatus.CANCELLED:
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.CANCELLED_ORDER,
                severity=DiscrepancySeverity.WARNING,
                message="Provider order is cancelled",
                expected="filled or active",
                observed=snapshot.status.value,
                evidence_references=snapshot.evidence_references,
            )
            discrepancies.append(discrepancy)
            warnings.append(discrepancy.message)

        if snapshot.status is BrokerOrderStatus.EXPIRED:
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.EXPIRED_ORDER,
                severity=DiscrepancySeverity.WARNING,
                message="Provider order expired",
                expected="filled or active",
                observed=snapshot.status.value,
                evidence_references=snapshot.evidence_references,
            )
            discrepancies.append(discrepancy)
            warnings.append(discrepancy.message)

    fill_ids = [fill.fill_id for fill in fills]
    duplicate_fill_ids = tuple(
        sorted(
            fill_id
            for fill_id, count in Counter(fill_ids).items()
            if count > 1
        )
    )

    if duplicate_fill_ids:
        severity = (
            DiscrepancySeverity.BLOCKING
            if policy.duplicate_execution_evidence_is_blocking
            else DiscrepancySeverity.WARNING
        )
        discrepancy = _discrepancy(
            request=request,
            category=DiscrepancyCategory.DUPLICATE_FILL,
            severity=severity,
            message="Duplicate fill evidence detected",
            expected="unique fill identifiers",
            observed=",".join(duplicate_fill_ids),
            evidence_references=tuple(
                sorted(
                    {
                        reference
                        for fill in fills
                        for reference in fill.evidence_references
                    }
                )
            ),
        )
        discrepancies.append(discrepancy)

        if severity is DiscrepancySeverity.BLOCKING:
            blockers.append(discrepancy.message)
        else:
            warnings.append(discrepancy.message)

    accepted_fills: list[ExecutionFill] = []

    for fill in fills:
        evidence.update(fill.evidence_references)

        fill_mismatch = False

        if fill.client_order_id != request.client_order_id:
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.FOREIGN_FILL,
                severity=DiscrepancySeverity.BLOCKING,
                message="Fill references another client order",
                expected=request.client_order_id,
                observed=fill.client_order_id,
                evidence_references=fill.evidence_references,
            )
            discrepancies.append(discrepancy)
            blockers.append(discrepancy.message)
            fill_mismatch = True

        if (
            provider_order_id is not None
            and fill.provider_order_id != provider_order_id
        ):
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.FOREIGN_FILL,
                severity=DiscrepancySeverity.BLOCKING,
                message="Fill references another provider order",
                expected=provider_order_id,
                observed=fill.provider_order_id,
                evidence_references=fill.evidence_references,
            )
            discrepancies.append(discrepancy)
            blockers.append(discrepancy.message)
            fill_mismatch = True

        if fill.symbol != request.symbol:
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.SYMBOL_MISMATCH,
                severity=DiscrepancySeverity.BLOCKING,
                message="Fill symbol mismatch",
                expected=request.symbol,
                observed=fill.symbol,
                evidence_references=fill.evidence_references,
            )
            discrepancies.append(discrepancy)
            blockers.append(discrepancy.message)
            fill_mismatch = True

        if fill.side is not request.side:
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.SIDE_MISMATCH,
                severity=DiscrepancySeverity.BLOCKING,
                message="Fill side mismatch",
                expected=request.side.value,
                observed=fill.side.value,
                evidence_references=fill.evidence_references,
            )
            discrepancies.append(discrepancy)
            blockers.append(discrepancy.message)
            fill_mismatch = True

        if not fill_mismatch:
            accepted_fills.append(fill)

    unique_fills = {
        fill.fill_id: fill
        for fill in accepted_fills
    }
    normalized_fills = tuple(
        sorted(
            unique_fills.values(),
            key=lambda item: item.fill_id,
        )
    )

    filled_quantity = sum(
        (fill.quantity for fill in normalized_fills),
        Decimal("0"),
    )
    gross_executed_notional = sum(
        (fill.quantity * fill.price for fill in normalized_fills),
        Decimal("0"),
    )
    total_fees = sum(
        (fill.fee for fill in normalized_fills),
        Decimal("0"),
    )
    average_fill_price = _weighted_average_price(normalized_fills)

    remaining_quantity = request.quantity - filled_quantity
    if remaining_quantity < 0:
        remaining_quantity = Decimal("0")

    status = _fill_status(
        requested_quantity=request.quantity,
        filled_quantity=filled_quantity,
        broker_status=broker_status,
        policy=policy,
    )

    if status is FillReconciliationStatus.PARTIALLY_FILLED:
        severity = (
            DiscrepancySeverity.WARNING
            if policy.allow_partial_fills
            else DiscrepancySeverity.BLOCKING
        )
        discrepancy = _discrepancy(
            request=request,
            category=DiscrepancyCategory.PARTIAL_FILL,
            severity=severity,
            message="Order was only partially filled",
            expected=str(request.quantity),
            observed=str(filled_quantity),
            evidence_references=tuple(sorted(evidence)),
        )
        discrepancies.append(discrepancy)

        if severity is DiscrepancySeverity.BLOCKING:
            blockers.append(discrepancy.message)
        else:
            warnings.append(discrepancy.message)

    if status is FillReconciliationStatus.OVERFILLED:
        discrepancy = _discrepancy(
            request=request,
            category=DiscrepancyCategory.OVERFILL,
            severity=DiscrepancySeverity.BLOCKING,
            message="Order was filled above its approved quantity",
            expected=str(request.quantity),
            observed=str(filled_quantity),
            evidence_references=tuple(sorted(evidence)),
        )
        discrepancies.append(discrepancy)
        blockers.append(discrepancy.message)

    if total_fees > policy.maximum_fee:
        discrepancy = _discrepancy(
            request=request,
            category=DiscrepancyCategory.EXCESSIVE_FEES,
            severity=DiscrepancySeverity.BLOCKING,
            message="Execution fees exceed policy maximum",
            expected=str(policy.maximum_fee),
            observed=str(total_fees),
            evidence_references=tuple(sorted(evidence)),
        )
        discrepancies.append(discrepancy)
        blockers.append(discrepancy.message)

    slippage_amount: Decimal | None = None
    slippage_basis_points: Decimal | None = None

    if average_fill_price is not None:
        if request.side is OrderSide.BUY:
            slippage_amount = (
                average_fill_price - request.reference_price
            )
        else:
            slippage_amount = (
                request.reference_price - average_fill_price
            )

        slippage_basis_points = (
            slippage_amount
            / request.reference_price
            * Decimal("10000")
        )

        if slippage_basis_points > policy.maximum_slippage_basis_points:
            discrepancy = _discrepancy(
                request=request,
                category=DiscrepancyCategory.EXCESSIVE_SLIPPAGE,
                severity=DiscrepancySeverity.BLOCKING,
                message="Execution slippage exceeds policy maximum",
                expected=str(policy.maximum_slippage_basis_points),
                observed=str(slippage_basis_points),
                evidence_references=tuple(sorted(evidence)),
            )
            discrepancies.append(discrepancy)
            blockers.append(discrepancy.message)

    if request.side is OrderSide.BUY:
        net_cash_effect = -(gross_executed_notional + total_fees)
    else:
        net_cash_effect = gross_executed_notional - total_fees

    reconciliation_id = deterministic_identifier(
        "order-reconciliation",
        request,
        acknowledgement,
        snapshot,
        normalized_fills,
        policy.snapshot(),
    )

    return OrderReconciliation(
        reconciliation_id=reconciliation_id,
        source_intent_id=request.source_intent_id,
        client_order_id=request.client_order_id,
        provider_order_id=provider_order_id,
        status=status,
        approved_quantity=request.quantity,
        filled_quantity=filled_quantity,
        remaining_quantity=remaining_quantity,
        weighted_average_fill_price=average_fill_price,
        gross_executed_notional=gross_executed_notional,
        total_fees=total_fees,
        net_cash_effect=net_cash_effect,
        slippage_amount=slippage_amount,
        slippage_basis_points=slippage_basis_points,
        discrepancies=tuple(discrepancies),
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        evidence_references=tuple(sorted(evidence)),
    )
