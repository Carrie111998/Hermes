from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Iterable

from .audit import deterministic_identifier
from .live_certification import (
    LiveTradingCertification,
    LiveTradingCertificationStatus,
    effective_certification_status,
)


class LiveLaunchControlStatus(StrEnum):
    BLOCKED = "blocked"
    ARMED = "armed"
    SUSPENDED = "suspended"
    EXPIRED = "expired"


def _required(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if value.strip()}))


@dataclass(frozen=True, slots=True)
class LiveLaunchControlPolicy:
    maximum_launch_duration_seconds: int = 86400
    maximum_daily_loss: Decimal = Decimal("5")
    maximum_open_positions: int = 1
    maximum_live_capital: Decimal = Decimal("25")
    maximum_order_notional: Decimal = Decimal("5")
    require_operator_approval: bool = True
    require_armed_kill_switch: bool = True
    require_confirmed_rollback: bool = True
    require_authorization_reference: bool = True
    require_evidence: bool = True

    def __post_init__(self) -> None:
        if self.maximum_launch_duration_seconds <= 0:
            raise ValueError("maximum_launch_duration_seconds must be positive")
        if self.maximum_daily_loss <= 0:
            raise ValueError("maximum_daily_loss must be positive")
        if self.maximum_open_positions <= 0:
            raise ValueError("maximum_open_positions must be positive")
        if self.maximum_live_capital <= 0:
            raise ValueError("maximum_live_capital must be positive")
        if self.maximum_order_notional <= 0:
            raise ValueError("maximum_order_notional must be positive")


@dataclass(frozen=True, slots=True)
class LiveLaunchControlRequest:
    request_id: str
    broker_name: str
    account_identifier: str
    asset_classes: tuple[str, ...]
    order_types: tuple[str, ...]
    symbols: tuple[str, ...]
    live_capital: Decimal
    maximum_order_notional: Decimal
    maximum_daily_loss: Decimal
    maximum_open_positions: int
    launch_from_epoch: int
    launch_until_epoch: int
    operator_identity: str
    operator_approved: bool
    kill_switch_armed: bool
    rollback_confirmed: bool
    authorization_reference: str
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "request_id",
            "broker_name",
            "account_identifier",
            "operator_identity",
        ):
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
        if not self.symbols:
            raise ValueError("symbols must not be empty")
        if self.live_capital <= 0:
            raise ValueError("live_capital must be positive")
        if self.maximum_order_notional <= 0:
            raise ValueError("maximum_order_notional must be positive")
        if self.maximum_daily_loss <= 0:
            raise ValueError("maximum_daily_loss must be positive")
        if self.maximum_open_positions <= 0:
            raise ValueError("maximum_open_positions must be positive")
        if self.launch_from_epoch < 0 or self.launch_until_epoch < 0:
            raise ValueError("launch timestamps must be non-negative")
        if self.launch_until_epoch <= self.launch_from_epoch:
            raise ValueError(
                "launch_until_epoch must be after launch_from_epoch"
            )


@dataclass(frozen=True, slots=True)
class LiveLaunchControl:
    launch_control_id: str
    certification_id: str
    request_id: str
    status: LiveLaunchControlStatus
    broker_name: str
    account_identifier: str
    asset_classes: tuple[str, ...]
    order_types: tuple[str, ...]
    symbols: tuple[str, ...]
    live_capital: Decimal
    maximum_order_notional: Decimal
    maximum_daily_loss: Decimal
    maximum_open_positions: int
    launch_from_epoch: int
    launch_until_epoch: int
    operator_identity: str
    authorization_reference: str
    evidence_references: tuple[str, ...]
    passed_checks: tuple[str, ...]
    failed_checks: tuple[str, ...]
    suspension_reason: str = ""
    suspended_at_epoch: int | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "launch_control_id",
            "certification_id",
            "request_id",
            "broker_name",
            "account_identifier",
            "operator_identity",
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
            "passed_checks",
            "failed_checks",
        ):
            object.__setattr__(
                self,
                field_name,
                _deduplicate(getattr(self, field_name)),
            )


def arm_live_launch_control(
    certification: LiveTradingCertification,
    request: LiveLaunchControlRequest,
    *,
    evaluated_at_epoch: int,
    policy: LiveLaunchControlPolicy | None = None,
) -> LiveLaunchControl:
    launch_policy = policy or LiveLaunchControlPolicy()

    if evaluated_at_epoch < 0:
        raise ValueError("evaluated_at_epoch must be non-negative")

    certification_status = effective_certification_status(
        certification,
        at_epoch=evaluated_at_epoch,
    )
    combined_evidence = _deduplicate(
        (*certification.evidence_references, *request.evidence_references)
    )
    launch_duration = request.launch_until_epoch - request.launch_from_epoch

    checks = [
        (
            "certification_active",
            certification_status is LiveTradingCertificationStatus.CERTIFIED,
        ),
        (
            "broker_scope_match",
            request.broker_name == certification.broker_name,
        ),
        (
            "account_scope_match",
            request.account_identifier == certification.account_identifier,
        ),
        (
            "asset_scope_match",
            set(request.asset_classes).issubset(certification.asset_classes),
        ),
        (
            "order_type_scope_match",
            set(request.order_types).issubset(certification.order_types),
        ),
        (
            "symbol_scope_match",
            set(request.symbols).issubset(certification.symbols),
        ),
        (
            "launch_within_certification_window",
            certification.valid_from_epoch <= request.launch_from_epoch
            and request.launch_until_epoch <= certification.valid_until_epoch,
        ),
        (
            "launch_window_current",
            request.launch_from_epoch
            <= evaluated_at_epoch
            < request.launch_until_epoch,
        ),
        (
            "launch_duration_within_policy",
            launch_duration <= launch_policy.maximum_launch_duration_seconds,
        ),
        (
            "operator_approval",
            not launch_policy.require_operator_approval
            or request.operator_approved,
        ),
        (
            "kill_switch_armed",
            not launch_policy.require_armed_kill_switch
            or request.kill_switch_armed,
        ),
        (
            "rollback_confirmed",
            not launch_policy.require_confirmed_rollback
            or request.rollback_confirmed,
        ),
        (
            "authorization_reference",
            not launch_policy.require_authorization_reference
            or bool(request.authorization_reference.strip()),
        ),
        (
            "live_capital_within_certification",
            request.live_capital <= certification.initial_capital,
        ),
        (
            "live_capital_within_policy",
            request.live_capital <= launch_policy.maximum_live_capital,
        ),
        (
            "order_notional_within_certification",
            request.maximum_order_notional
            <= certification.maximum_order_notional,
        ),
        (
            "order_notional_within_policy",
            request.maximum_order_notional
            <= launch_policy.maximum_order_notional,
        ),
        (
            "daily_loss_limit",
            request.maximum_daily_loss <= launch_policy.maximum_daily_loss,
        ),
        (
            "open_position_limit",
            request.maximum_open_positions
            <= launch_policy.maximum_open_positions,
        ),
        (
            "evidence",
            not launch_policy.require_evidence or bool(combined_evidence),
        ),
    ]

    passed_checks = tuple(name for name, passed in checks if passed)
    failed_checks = tuple(name for name, passed in checks if not passed)
    status = (
        LiveLaunchControlStatus.ARMED
        if not failed_checks
        else LiveLaunchControlStatus.BLOCKED
    )

    launch_control_id = deterministic_identifier(
        "live-launch-control",
        certification.certification_id,
        request.request_id,
        request.operator_identity,
        request.authorization_reference,
        request.launch_from_epoch,
        request.launch_until_epoch,
        request.live_capital,
        request.maximum_order_notional,
        request.maximum_daily_loss,
        request.maximum_open_positions,
        status,
        *request.asset_classes,
        *request.order_types,
        *request.symbols,
        *failed_checks,
    )

    return LiveLaunchControl(
        launch_control_id=launch_control_id,
        certification_id=certification.certification_id,
        request_id=request.request_id,
        status=status,
        broker_name=request.broker_name,
        account_identifier=request.account_identifier,
        asset_classes=request.asset_classes,
        order_types=request.order_types,
        symbols=request.symbols,
        live_capital=request.live_capital,
        maximum_order_notional=request.maximum_order_notional,
        maximum_daily_loss=request.maximum_daily_loss,
        maximum_open_positions=request.maximum_open_positions,
        launch_from_epoch=request.launch_from_epoch,
        launch_until_epoch=request.launch_until_epoch,
        operator_identity=request.operator_identity,
        authorization_reference=request.authorization_reference.strip(),
        evidence_references=combined_evidence,
        passed_checks=passed_checks,
        failed_checks=failed_checks,
    )


def effective_live_launch_control_status(
    control: LiveLaunchControl,
    *,
    at_epoch: int,
) -> LiveLaunchControlStatus:
    if at_epoch < 0:
        raise ValueError("at_epoch must be non-negative")
    if control.status is LiveLaunchControlStatus.SUSPENDED:
        return LiveLaunchControlStatus.SUSPENDED
    if control.status is LiveLaunchControlStatus.BLOCKED:
        return LiveLaunchControlStatus.BLOCKED
    if at_epoch >= control.launch_until_epoch:
        return LiveLaunchControlStatus.EXPIRED
    return control.status


def suspend_live_launch_control(
    control: LiveLaunchControl,
    *,
    suspended_at_epoch: int,
    reason: str,
) -> LiveLaunchControl:
    reason_clean = _required(reason, "reason")
    if control.status is not LiveLaunchControlStatus.ARMED:
        raise ValueError("only armed launch controls may be suspended")
    if suspended_at_epoch < control.launch_from_epoch:
        raise ValueError("suspended_at_epoch cannot predate launch window")
    return replace(
        control,
        status=LiveLaunchControlStatus.SUSPENDED,
        suspension_reason=reason_clean,
        suspended_at_epoch=suspended_at_epoch,
    )
