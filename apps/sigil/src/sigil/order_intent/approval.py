from __future__ import annotations

from datetime import UTC, datetime

from .audit import (
    approval_record_identity,
    approval_request_identity,
)
from .models import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalStatus,
    OrderIntentPackage,
    OrderIntentStatus,
)


def create_approval_request(
    package: OrderIntentPackage,
    *,
    created_at: str,
    expires_at: str | None = None,
    required_approver_role: str = "portfolio_owner",
) -> ApprovalRequest:
    if package.status is not OrderIntentStatus.READY_FOR_APPROVAL:
        raise ValueError(
            "approval requests require a ready-for-approval package"
        )

    if package.blockers:
        raise ValueError(
            "approval requests cannot be created for blocked packages"
        )

    if not package.intents:
        raise ValueError(
            "approval requests require at least one order intent"
        )

    payload = {
        "order_intent_package_id": package.package_id,
        "source_rebalance_package_id": (
            package.source_rebalance_package_id
        ),
        "intent_ids": tuple(
            sorted(intent.intent_id for intent in package.intents)
        ),
        "created_at": created_at,
        "expires_at": expires_at,
        "required_approver_role": required_approver_role,
    }

    return ApprovalRequest(
        request_id=approval_request_identity(payload),
        order_intent_package_id=package.package_id,
        source_rebalance_package_id=(
            package.source_rebalance_package_id
        ),
        intent_ids=payload["intent_ids"],
        requested_action=(
            "Authorize downstream consideration of the governed "
            "order-intent package."
        ),
        summary=(
            f"Review {len(package.intents)} governed order intent(s) "
            f"with aggregate turnover of {package.aggregate_turnover}."
        ),
        aggregate_buy_notional=package.aggregate_buy_notional,
        aggregate_sell_notional=package.aggregate_sell_notional,
        aggregate_turnover=package.aggregate_turnover,
        constraint_summary=tuple(
            f"{constraint.name}:{'pass' if constraint.passed else 'fail'}"
            for constraint in package.constraints
        ),
        evidence_references=package.evidence_references,
        created_at=created_at,
        expires_at=expires_at,
        required_approver_role=required_approver_role,
    )


def decide_approval_request(
    request: ApprovalRequest,
    *,
    decision: ApprovalDecision,
    approver_identity: str,
    decided_at: str,
    reason: str | None = None,
) -> ApprovalRecord:
    if request.status is not ApprovalStatus.PENDING:
        raise ValueError("only pending approval requests may be decided")

    status = (
        ApprovalStatus.APPROVED
        if decision is ApprovalDecision.APPROVE
        else ApprovalStatus.REJECTED
    )

    payload = {
        "request_id": request.request_id,
        "order_intent_package_id": request.order_intent_package_id,
        "decision": decision.value,
        "status": status.value,
        "approver_identity": approver_identity,
        "decided_at": decided_at,
        "reason": reason,
    }

    return ApprovalRecord(
        record_id=approval_record_identity(payload),
        request_id=request.request_id,
        order_intent_package_id=request.order_intent_package_id,
        decision=decision,
        status=status,
        approver_identity=approver_identity,
        decided_at=decided_at,
        reason=reason,
    )


def expire_approval_request(
    request: ApprovalRequest,
    *,
    decided_at: str | None = None,
    reason: str = "Approval request expired before a decision.",
) -> ApprovalRecord:
    if request.status is not ApprovalStatus.PENDING:
        raise ValueError("only pending approval requests may expire")

    effective_decided_at = decided_at or datetime.now(UTC).isoformat()

    payload = {
        "request_id": request.request_id,
        "order_intent_package_id": request.order_intent_package_id,
        "decision": ApprovalDecision.REJECT.value,
        "status": ApprovalStatus.EXPIRED.value,
        "approver_identity": "system-expiration",
        "decided_at": effective_decided_at,
        "reason": reason,
    }

    return ApprovalRecord(
        record_id=approval_record_identity(payload),
        request_id=request.request_id,
        order_intent_package_id=request.order_intent_package_id,
        decision=ApprovalDecision.REJECT,
        status=ApprovalStatus.EXPIRED,
        approver_identity="system-expiration",
        decided_at=effective_decided_at,
        reason=reason,
    )
