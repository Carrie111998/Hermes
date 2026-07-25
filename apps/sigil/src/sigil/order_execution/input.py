from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sigil.order_intent.models import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalStatus,
    OrderIntentPackage,
    OrderIntentStatus,
)

from .models import (
    ApprovedOrder,
    ExecutionContext,
    SubmissionAdmissionStatus,
)
from .policy import ExecutionPolicy


def _clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return _clean(raw).lower()


def _record_value(record: ApprovalRecord, *names: str) -> Any:
    for name in names:
        if hasattr(record, name):
            return getattr(record, name)
    return None


def _request_value(request: ApprovalRequest, *names: str) -> Any:
    for name in names:
        if hasattr(request, name):
            return getattr(request, name)
    return None


def _decimal_matches(
    left: Decimal,
    right: Decimal,
    tolerance: Decimal,
) -> bool:
    return abs(left - right) <= tolerance


@dataclass(frozen=True, slots=True)
class ExecutionAdmission:
    status: SubmissionAdmissionStatus
    package: OrderIntentPackage
    approval_request: ApprovalRequest
    approval_record: ApprovalRecord
    context: ExecutionContext
    approved_orders: tuple[ApprovedOrder, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    evidence_references: tuple[str, ...]

    @property
    def admitted(self) -> bool:
        return self.status is SubmissionAdmissionStatus.READY


def evaluate_execution_input(
    package: OrderIntentPackage,
    approval_request: ApprovalRequest,
    approval_record: ApprovalRecord,
    context: ExecutionContext,
    policy: ExecutionPolicy,
) -> ExecutionAdmission:
    blockers: list[str] = []
    warnings: list[str] = []
    evidence: set[str] = set()

    evidence.update(package.evidence_references)
    evidence.update(approval_request.evidence_references)

    record_evidence = _record_value(
        approval_record,
        "evidence_references",
        "evidence",
    )
    if record_evidence:
        evidence.update(record_evidence)

    if package.status is not OrderIntentStatus.READY_FOR_APPROVAL:
        blockers.append(
            "order-intent package is not ready for approval"
        )

    if package.blockers:
        blockers.extend(
            f"order-intent package blocker: {blocker}"
            for blocker in package.blockers
        )

    warnings.extend(package.warnings)

    if not package.intents:
        blockers.append("order-intent package contains no intents")

    if len(package.intents) > policy.max_orders:
        blockers.append(
            "order count exceeds execution policy maximum"
        )

    if approval_request.order_intent_package_id != package.package_id:
        blockers.append(
            "approval request does not reference the supplied "
            "order-intent package"
        )

    if (
        approval_request.source_rebalance_package_id
        != package.source_rebalance_package_id
    ):
        blockers.append(
            "approval request source rebalance package does not match"
        )

    package_intent_ids = tuple(
        sorted(intent.intent_id for intent in package.intents)
    )
    requested_intent_ids = tuple(sorted(approval_request.intent_ids))

    if requested_intent_ids != package_intent_ids:
        blockers.append(
            "approval request intent identifiers do not exactly match "
            "the order-intent package"
        )

    if approval_request.status not in {
        ApprovalStatus.PENDING,
        ApprovalStatus.APPROVED,
    }:
        blockers.append(
            "approval request is rejected or expired"
        )

    if not approval_request.approval_does_not_equal_execution:
        blockers.append(
            "approval request must preserve the approval/execution boundary"
        )

    record_request_id = _clean(
        _record_value(
            approval_record,
            "approval_request_id",
            "request_id",
            "source_approval_request_id",
        )
    )
    if record_request_id and record_request_id != approval_request.request_id:
        blockers.append(
            "approval record does not reference the supplied "
            "approval request"
        )

    record_package_id = _clean(
        _record_value(
            approval_record,
            "order_intent_package_id",
            "package_id",
            "source_order_intent_package_id",
        )
    )
    if record_package_id and record_package_id != package.package_id:
        blockers.append(
            "approval record does not reference the supplied "
            "order-intent package"
        )

    record_decision = _enum_value(
        _record_value(
            approval_record,
            "decision",
            "approval_decision",
        )
    )
    if record_decision != ApprovalDecision.APPROVE.value:
        blockers.append(
            "approval record does not contain an approve decision"
        )

    record_status = _enum_value(
        _record_value(
            approval_record,
            "status",
            "approval_status",
        )
    )
    if record_status and record_status != ApprovalStatus.APPROVED.value:
        blockers.append(
            "approval record status is not approved"
        )

    approval_boundary = _record_value(
        approval_record,
        "approval_does_not_equal_execution",
    )
    if approval_boundary is False:
        blockers.append(
            "approval record violates the approval/execution boundary"
        )

    approver_identity = _clean(
        _record_value(
            approval_record,
            "approver_identity",
            "approved_by",
            "decided_by",
            "actor_identity",
        )
    )
    if policy.require_verified_approver_identity and not approver_identity:
        blockers.append(
            "approval record does not identify the human approver"
        )

    approver_role = _clean(
        _record_value(
            approval_record,
            "approver_role",
            "approved_by_role",
            "decided_by_role",
            "actor_role",
        )
    )
    if (
        approver_role
        and approval_request.required_approver_role
        and approver_role != approval_request.required_approver_role
    ):
        blockers.append(
            "approval record approver role does not satisfy the request"
        )

    if context.provider not in policy.allowed_providers:
        blockers.append(
            f"provider is not allowed by execution policy: "
            f"{context.provider}"
        )

    if (
        policy.allowed_account_ids
        and context.account_id not in policy.allowed_account_ids
    ):
        blockers.append(
            "account identifier is not allowed by execution policy"
        )

    if context.account_class not in policy.allowed_account_classes:
        blockers.append(
            "account class is not allowed by execution policy"
        )

    if context.environment not in policy.allowed_environments:
        blockers.append(
            "execution environment is not allowed by execution policy"
        )

    if (
        context.environment.value == "live"
        and not policy.allow_live_execution
    ):
        blockers.append(
            "live execution is disabled by execution policy"
        )

    if (
        context.environment.value == "live"
        and not context.live_execution_explicitly_requested
    ):
        blockers.append(
            "live execution was not explicitly requested"
        )

    if policy.require_evidence and not evidence:
        blockers.append(
            "execution requires evidence references"
        )

    if package.aggregate_buy_notional > policy.max_aggregate_buy_notional:
        blockers.append(
            "aggregate approved buy notional exceeds execution policy"
        )

    if package.aggregate_sell_notional > policy.max_aggregate_sell_notional:
        blockers.append(
            "aggregate approved sell notional exceeds execution policy"
        )

    if package.aggregate_turnover > policy.max_aggregate_turnover:
        blockers.append(
            "aggregate approved turnover exceeds execution policy"
        )

    if not _decimal_matches(
        approval_request.aggregate_buy_notional,
        package.aggregate_buy_notional,
        policy.notional_tolerance,
    ):
        blockers.append(
            "approval request buy notional does not match package"
        )

    if not _decimal_matches(
        approval_request.aggregate_sell_notional,
        package.aggregate_sell_notional,
        policy.notional_tolerance,
    ):
        blockers.append(
            "approval request sell notional does not match package"
        )

    if not _decimal_matches(
        approval_request.aggregate_turnover,
        package.aggregate_turnover,
        policy.notional_tolerance,
    ):
        blockers.append(
            "approval request turnover does not match package"
        )

    approved_orders: list[ApprovedOrder] = []

    for intent in sorted(
        package.intents,
        key=lambda item: item.intent_id,
    ):
        if intent.blockers:
            blockers.extend(
                f"{intent.intent_id}: {blocker}"
                for blocker in intent.blockers
            )

        warnings.extend(
            f"{intent.intent_id}: {warning}"
            for warning in intent.warnings
        )

        if intent.order_type not in policy.allowed_order_types:
            blockers.append(
                f"{intent.intent_id}: order type is not allowed"
            )

        if intent.time_in_force not in policy.allowed_time_in_force:
            blockers.append(
                f"{intent.intent_id}: time in force is not allowed"
            )

        if intent.quantity > policy.max_order_quantity:
            blockers.append(
                f"{intent.intent_id}: quantity exceeds policy maximum"
            )

        if intent.notional > policy.max_order_notional:
            blockers.append(
                f"{intent.intent_id}: notional exceeds policy maximum"
            )

        if not intent.analytical_only:
            blockers.append(
                f"{intent.intent_id}: intent must remain analytical only"
            )

        if intent.execution_authority:
            blockers.append(
                f"{intent.intent_id}: intent improperly contains "
                "execution authority"
            )

        if policy.require_evidence and not intent.evidence_references:
            blockers.append(
                f"{intent.intent_id}: intent has no evidence references"
            )

        evidence.update(intent.evidence_references)

        approved_orders.append(
            ApprovedOrder(
                intent_id=intent.intent_id,
                source_proposal_id=intent.source_proposal_id,
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                time_in_force=intent.time_in_force,
                quantity=intent.quantity,
                reference_price=intent.reference_price,
                notional=intent.notional,
                limit_price=intent.limit_price,
                evidence_references=intent.evidence_references,
            )
        )

    normalized_blockers = tuple(
        sorted(set(blocker.strip() for blocker in blockers if blocker.strip()))
    )
    normalized_warnings = tuple(
        sorted(set(warning.strip() for warning in warnings if warning.strip()))
    )

    status = (
        SubmissionAdmissionStatus.BLOCKED
        if normalized_blockers
        else SubmissionAdmissionStatus.READY
    )

    return ExecutionAdmission(
        status=status,
        package=package,
        approval_request=approval_request,
        approval_record=approval_record,
        context=context,
        approved_orders=tuple(approved_orders),
        blockers=normalized_blockers,
        warnings=normalized_warnings,
        evidence_references=tuple(sorted(evidence)),
    )
