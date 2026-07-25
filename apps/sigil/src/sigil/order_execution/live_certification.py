from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Iterable

from .audit import deterministic_identifier
from .live_eligibility import (
    LiveTradingEligibilityReview,
    LiveTradingEligibilityStatus,
)


class LiveTradingCertificationStatus(StrEnum):
    DENIED = "denied"
    CERTIFIED = "certified"
    REVOKED = "revoked"
    EXPIRED = "expired"


def _required(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if value.strip()}))


@dataclass(frozen=True, slots=True)
class LiveTradingCertificationPolicy:
    maximum_certification_duration_seconds: int = 604800
    maximum_initial_capital: Decimal = Decimal("25")
    maximum_order_notional: Decimal = Decimal("5")
    allowed_order_types: tuple[str, ...] = ("limit", "market")
    require_symbol_scope: bool = True
    require_evidence: bool = True
    require_kill_switch: bool = True
    require_rollback_plan: bool = True

    def __post_init__(self) -> None:
        if self.maximum_certification_duration_seconds <= 0:
            raise ValueError(
                "maximum_certification_duration_seconds must be positive"
            )
        if self.maximum_initial_capital <= 0:
            raise ValueError("maximum_initial_capital must be positive")
        if self.maximum_order_notional <= 0:
            raise ValueError("maximum_order_notional must be positive")
        object.__setattr__(
            self,
            "allowed_order_types",
            _deduplicate(self.allowed_order_types),
        )
        if not self.allowed_order_types:
            raise ValueError("allowed_order_types must not be empty")


@dataclass(frozen=True, slots=True)
class LiveTradingCertificationRequest:
    request_id: str
    broker_name: str
    account_identifier: str
    asset_classes: tuple[str, ...]
    order_types: tuple[str, ...]
    symbols: tuple[str, ...]
    initial_capital: Decimal
    maximum_order_notional: Decimal
    valid_from_epoch: int
    valid_until_epoch: int
    kill_switch_reference: str
    rollback_plan_reference: str
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("request_id", "broker_name", "account_identifier"):
            object.__setattr__(
                self,
                field_name,
                _required(getattr(self, field_name), field_name),
            )
        for field_name in ("asset_classes", "order_types", "symbols"):
            object.__setattr__(
                self,
                field_name,
                _deduplicate(getattr(self, field_name)),
            )
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )
        if not self.asset_classes:
            raise ValueError("asset_classes must not be empty")
        if not self.order_types:
            raise ValueError("order_types must not be empty")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if self.maximum_order_notional <= 0:
            raise ValueError("maximum_order_notional must be positive")
        if self.valid_from_epoch < 0 or self.valid_until_epoch < 0:
            raise ValueError("validity timestamps must be non-negative")
        if self.valid_until_epoch <= self.valid_from_epoch:
            raise ValueError("valid_until_epoch must be after valid_from_epoch")


@dataclass(frozen=True, slots=True)
class LiveTradingCertification:
    certification_id: str
    eligibility_review_id: str
    request_id: str
    certifier_identity: str
    status: LiveTradingCertificationStatus
    broker_name: str
    account_identifier: str
    asset_classes: tuple[str, ...]
    order_types: tuple[str, ...]
    symbols: tuple[str, ...]
    initial_capital: Decimal
    maximum_order_notional: Decimal
    valid_from_epoch: int
    valid_until_epoch: int
    kill_switch_reference: str
    rollback_plan_reference: str
    evidence_references: tuple[str, ...]
    failed_checks: tuple[str, ...]
    revocation_reason: str = ""
    revoked_at_epoch: int | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "certification_id",
            "eligibility_review_id",
            "request_id",
            "certifier_identity",
            "broker_name",
            "account_identifier",
        ):
            object.__setattr__(
                self,
                field_name,
                _required(getattr(self, field_name), field_name),
            )
        for field_name in (
            "asset_classes",
            "order_types",
            "symbols",
            "evidence_references",
            "failed_checks",
        ):
            object.__setattr__(
                self,
                field_name,
                _deduplicate(getattr(self, field_name)),
            )


def certify_live_trading(
    eligibility: LiveTradingEligibilityReview,
    request: LiveTradingCertificationRequest,
    *,
    certifier_identity: str,
    policy: LiveTradingCertificationPolicy | None = None,
    evidence_references: tuple[str, ...] = (),
) -> LiveTradingCertification:
    certification_policy = policy or LiveTradingCertificationPolicy()
    certifier = _required(certifier_identity, "certifier_identity")

    combined_evidence = _deduplicate(
        (
            *eligibility.evidence_references,
            *request.evidence_references,
            *evidence_references,
        )
    )
    duration = request.valid_until_epoch - request.valid_from_epoch

    checks = [
        (
            "eligible_for_live_certification",
            eligibility.status
            is LiveTradingEligibilityStatus.ELIGIBLE_FOR_LIVE_CERTIFICATION,
        ),
        (
            "duration_within_policy",
            duration
            <= certification_policy.maximum_certification_duration_seconds,
        ),
        (
            "initial_capital_limit",
            request.initial_capital
            <= certification_policy.maximum_initial_capital,
        ),
        (
            "order_notional_limit",
            request.maximum_order_notional
            <= certification_policy.maximum_order_notional,
        ),
        (
            "order_types_allowed",
            set(request.order_types).issubset(
                certification_policy.allowed_order_types
            ),
        ),
        (
            "symbol_scope",
            not certification_policy.require_symbol_scope
            or bool(request.symbols),
        ),
        (
            "kill_switch",
            not certification_policy.require_kill_switch
            or bool(request.kill_switch_reference.strip()),
        ),
        (
            "rollback_plan",
            not certification_policy.require_rollback_plan
            or bool(request.rollback_plan_reference.strip()),
        ),
        (
            "evidence",
            not certification_policy.require_evidence
            or bool(combined_evidence),
        ),
    ]
    failed_checks = tuple(name for name, passed in checks if not passed)
    status = (
        LiveTradingCertificationStatus.CERTIFIED
        if not failed_checks
        else LiveTradingCertificationStatus.DENIED
    )

    certification_id = deterministic_identifier(
        "live-trading-certification",
        eligibility.review_id,
        request.request_id,
        certifier,
        request.broker_name,
        request.account_identifier,
        request.valid_from_epoch,
        request.valid_until_epoch,
        status,
        *request.asset_classes,
        *request.order_types,
        *request.symbols,
        *failed_checks,
    )

    return LiveTradingCertification(
        certification_id=certification_id,
        eligibility_review_id=eligibility.review_id,
        request_id=request.request_id,
        certifier_identity=certifier,
        status=status,
        broker_name=request.broker_name,
        account_identifier=request.account_identifier,
        asset_classes=request.asset_classes,
        order_types=request.order_types,
        symbols=request.symbols,
        initial_capital=request.initial_capital,
        maximum_order_notional=request.maximum_order_notional,
        valid_from_epoch=request.valid_from_epoch,
        valid_until_epoch=request.valid_until_epoch,
        kill_switch_reference=request.kill_switch_reference.strip(),
        rollback_plan_reference=request.rollback_plan_reference.strip(),
        evidence_references=combined_evidence,
        failed_checks=failed_checks,
    )


def effective_certification_status(
    certification: LiveTradingCertification,
    *,
    at_epoch: int,
) -> LiveTradingCertificationStatus:
    if at_epoch < 0:
        raise ValueError("at_epoch must be non-negative")
    if certification.status is LiveTradingCertificationStatus.REVOKED:
        return LiveTradingCertificationStatus.REVOKED
    if certification.status is LiveTradingCertificationStatus.DENIED:
        return LiveTradingCertificationStatus.DENIED
    if at_epoch >= certification.valid_until_epoch:
        return LiveTradingCertificationStatus.EXPIRED
    return certification.status


def revoke_live_trading_certification(
    certification: LiveTradingCertification,
    *,
    revoked_at_epoch: int,
    reason: str,
) -> LiveTradingCertification:
    reason_clean = _required(reason, "reason")
    if certification.status is not LiveTradingCertificationStatus.CERTIFIED:
        raise ValueError("only certified certifications may be revoked")
    if revoked_at_epoch < certification.valid_from_epoch:
        raise ValueError("revoked_at_epoch cannot predate certification")
    return replace(
        certification,
        status=LiveTradingCertificationStatus.REVOKED,
        revocation_reason=reason_clean,
        revoked_at_epoch=revoked_at_epoch,
    )
