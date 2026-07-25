from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Iterable

from .audit import deterministic_identifier
from .promotion_readiness import (
    PaperPromotionReadinessAssessment,
    PaperPromotionReadinessStatus,
)


class LiveTradingEligibilityStatus(StrEnum):
    INELIGIBLE = "ineligible"
    CONDITIONAL = "conditional"
    ELIGIBLE_FOR_LIVE_CERTIFICATION = "eligible_for_live_certification"


def _required(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if value.strip()}))


@dataclass(frozen=True, slots=True)
class LiveTradingEligibilityPolicy:
    maximum_readiness_age_seconds: int = 604800
    maximum_initial_capital: Decimal = Decimal("25")
    maximum_order_notional: Decimal = Decimal("5")
    require_operator_approval: bool = True
    require_credentials_attested: bool = True
    require_legal_acknowledgment: bool = True
    require_kill_switch: bool = True
    require_rollback_plan: bool = True
    require_complete_evidence: bool = True

    def __post_init__(self) -> None:
        if self.maximum_readiness_age_seconds < 0:
            raise ValueError(
                "maximum_readiness_age_seconds must be non-negative"
            )
        if self.maximum_initial_capital <= 0:
            raise ValueError("maximum_initial_capital must be positive")
        if self.maximum_order_notional <= 0:
            raise ValueError("maximum_order_notional must be positive")


@dataclass(frozen=True, slots=True)
class LiveTradingEligibilityRequest:
    request_id: str
    requested_at_epoch: int
    operator_identity: str
    operator_approved: bool
    broker_name: str
    account_identifier: str
    credentials_attested: bool
    legal_acknowledgment_reference: str
    jurisdiction: str
    proposed_initial_capital: Decimal
    proposed_maximum_order_notional: Decimal
    kill_switch_reference: str
    rollback_plan_reference: str
    unresolved_blockers: tuple[str, ...] = ()
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "request_id",
            "operator_identity",
            "broker_name",
            "account_identifier",
            "jurisdiction",
        ):
            object.__setattr__(
                self,
                field_name,
                _required(getattr(self, field_name), field_name),
            )
        if self.requested_at_epoch < 0:
            raise ValueError("requested_at_epoch must be non-negative")
        if self.proposed_initial_capital <= 0:
            raise ValueError("proposed_initial_capital must be positive")
        if self.proposed_maximum_order_notional <= 0:
            raise ValueError(
                "proposed_maximum_order_notional must be positive"
            )
        object.__setattr__(
            self,
            "unresolved_blockers",
            _deduplicate(self.unresolved_blockers),
        )
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )


@dataclass(frozen=True, slots=True)
class LiveTradingEligibilityReview:
    review_id: str
    reviewed_at_epoch: int
    reviewer_identity: str
    status: LiveTradingEligibilityStatus
    readiness_assessment_id: str
    request_id: str
    passed_checks: tuple[str, ...]
    failed_checks: tuple[str, ...]
    evidence_references: tuple[str, ...]
    conditions: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "review_id",
            "reviewer_identity",
            "readiness_assessment_id",
            "request_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _required(getattr(self, field_name), field_name),
            )
        if self.reviewed_at_epoch < 0:
            raise ValueError("reviewed_at_epoch must be non-negative")
        for field_name in (
            "passed_checks",
            "failed_checks",
            "evidence_references",
            "conditions",
        ):
            object.__setattr__(
                self,
                field_name,
                _deduplicate(getattr(self, field_name)),
            )


def review_live_trading_eligibility(
    readiness: PaperPromotionReadinessAssessment,
    request: LiveTradingEligibilityRequest,
    *,
    reviewed_at_epoch: int,
    reviewer_identity: str,
    policy: LiveTradingEligibilityPolicy | None = None,
    evidence_references: tuple[str, ...] = (),
) -> LiveTradingEligibilityReview:
    review_policy = policy or LiveTradingEligibilityPolicy()
    reviewer_clean = _required(reviewer_identity, "reviewer_identity")

    if reviewed_at_epoch < 0:
        raise ValueError("reviewed_at_epoch must be non-negative")
    if request.requested_at_epoch > reviewed_at_epoch:
        raise ValueError("request cannot be from the future")

    readiness_age = reviewed_at_epoch - request.requested_at_epoch
    combined_evidence = _deduplicate(
        (
            *readiness.evidence_references,
            *request.evidence_references,
            *evidence_references,
        )
    )

    checks = [
        (
            "paper_ready",
            readiness.status is PaperPromotionReadinessStatus.PAPER_READY,
        ),
        (
            "readiness_current",
            readiness_age <= review_policy.maximum_readiness_age_seconds,
        ),
        (
            "operator_approval",
            not review_policy.require_operator_approval
            or request.operator_approved,
        ),
        (
            "credentials_attested",
            not review_policy.require_credentials_attested
            or request.credentials_attested,
        ),
        (
            "legal_acknowledgment",
            not review_policy.require_legal_acknowledgment
            or bool(request.legal_acknowledgment_reference.strip()),
        ),
        (
            "initial_capital_limit",
            request.proposed_initial_capital
            <= review_policy.maximum_initial_capital,
        ),
        (
            "order_notional_limit",
            request.proposed_maximum_order_notional
            <= review_policy.maximum_order_notional,
        ),
        (
            "kill_switch",
            not review_policy.require_kill_switch
            or bool(request.kill_switch_reference.strip()),
        ),
        (
            "rollback_plan",
            not review_policy.require_rollback_plan
            or bool(request.rollback_plan_reference.strip()),
        ),
        ("no_unresolved_blockers", not request.unresolved_blockers),
        (
            "complete_evidence",
            not review_policy.require_complete_evidence
            or bool(combined_evidence),
        ),
    ]

    passed_checks = tuple(name for name, ok in checks if ok)
    failed_checks = tuple(name for name, ok in checks if not ok)

    hard_failures = {
        "paper_ready",
        "credentials_attested",
        "legal_acknowledgment",
        "kill_switch",
        "rollback_plan",
        "no_unresolved_blockers",
        "complete_evidence",
    } & set(failed_checks)

    if not failed_checks:
        status = (
            LiveTradingEligibilityStatus.ELIGIBLE_FOR_LIVE_CERTIFICATION
        )
    elif hard_failures:
        status = LiveTradingEligibilityStatus.INELIGIBLE
    else:
        status = LiveTradingEligibilityStatus.CONDITIONAL

    conditions = tuple(
        f"resolve eligibility check: {check}" for check in failed_checks
    )

    review_id = deterministic_identifier(
        "live-trading-eligibility-review",
        readiness.assessment_id,
        request.request_id,
        reviewed_at_epoch,
        reviewer_clean,
        status,
        *sorted(failed_checks),
    )

    return LiveTradingEligibilityReview(
        review_id=review_id,
        reviewed_at_epoch=reviewed_at_epoch,
        reviewer_identity=reviewer_clean,
        status=status,
        readiness_assessment_id=readiness.assessment_id,
        request_id=request.request_id,
        passed_checks=passed_checks,
        failed_checks=failed_checks,
        evidence_references=combined_evidence,
        conditions=conditions,
    )
