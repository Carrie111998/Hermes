from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Iterable

from .audit import deterministic_identifier
from .live_launch_control import (
    LiveLaunchControl,
    LiveLaunchControlStatus,
    effective_live_launch_control_status,
)


class LiveOrderAdmissionStatus(StrEnum):
    REJECTED = "rejected"
    ADMITTED = "admitted"
    DUPLICATE = "duplicate"
    EXPIRED = "expired"
    SUSPENDED = "suspended"


def _required(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if value.strip()}))


@dataclass(frozen=True, slots=True)
class LiveOrderAdmissionPolicy:
    maximum_market_data_age_seconds: int = 30
    require_operator_authorization: bool = True
    require_evidence: bool = True
    reject_duplicate_client_order_ids: bool = True

    def __post_init__(self) -> None:
        if self.maximum_market_data_age_seconds < 0:
            raise ValueError(
                "maximum_market_data_age_seconds must be non-negative"
            )


@dataclass(frozen=True, slots=True)
class LiveOrderAdmissionRequest:
    request_id: str
    client_order_id: str
    broker_name: str
    account_identifier: str
    asset_class: str
    symbol: str
    order_type: str
    side: str
    quantity: Decimal
    limit_price: Decimal | None
    estimated_notional: Decimal
    committed_live_capital: Decimal
    realized_daily_loss: Decimal
    open_position_count: int
    market_data_observed_at_epoch: int
    operator_authorization_reference: str
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "request_id",
            "client_order_id",
            "broker_name",
            "account_identifier",
            "asset_class",
            "symbol",
            "order_type",
            "side",
        ):
            object.__setattr__(
                self,
                field_name,
                _required(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "side", self.side.lower())
        object.__setattr__(self, "order_type", self.order_type.lower())
        object.__setattr__(self, "asset_class", self.asset_class.lower())

        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive when provided")
        if self.estimated_notional <= 0:
            raise ValueError("estimated_notional must be positive")
        if self.committed_live_capital < 0:
            raise ValueError("committed_live_capital must be non-negative")
        if self.realized_daily_loss < 0:
            raise ValueError("realized_daily_loss must be non-negative")
        if self.open_position_count < 0:
            raise ValueError("open_position_count must be non-negative")
        if self.market_data_observed_at_epoch < 0:
            raise ValueError(
                "market_data_observed_at_epoch must be non-negative"
            )


@dataclass(frozen=True, slots=True)
class LiveOrderAdmission:
    admission_id: str
    launch_control_id: str
    request_id: str
    client_order_id: str
    status: LiveOrderAdmissionStatus
    broker_name: str
    account_identifier: str
    asset_class: str
    symbol: str
    order_type: str
    side: str
    quantity: Decimal
    limit_price: Decimal | None
    estimated_notional: Decimal
    projected_live_capital: Decimal
    realized_daily_loss: Decimal
    projected_open_position_count: int
    market_data_age_seconds: int
    operator_authorization_reference: str
    evidence_references: tuple[str, ...]
    passed_checks: tuple[str, ...]
    failed_checks: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "admission_id",
            "launch_control_id",
            "request_id",
            "client_order_id",
            "broker_name",
            "account_identifier",
            "asset_class",
            "symbol",
            "order_type",
            "side",
        ):
            object.__setattr__(
                self,
                field_name,
                _required(getattr(self, field_name), field_name),
            )
        for field_name in (
            "evidence_references",
            "passed_checks",
            "failed_checks",
        ):
            object.__setattr__(
                self,
                field_name,
                _deduplicate(getattr(self, field_name)),
            )


def evaluate_live_order_admission(
    launch_control: LiveLaunchControl,
    request: LiveOrderAdmissionRequest,
    *,
    evaluated_at_epoch: int,
    prior_client_order_ids: Iterable[str] = (),
    policy: LiveOrderAdmissionPolicy | None = None,
) -> LiveOrderAdmission:
    admission_policy = policy or LiveOrderAdmissionPolicy()

    if evaluated_at_epoch < 0:
        raise ValueError("evaluated_at_epoch must be non-negative")

    effective_status = effective_live_launch_control_status(
        launch_control,
        at_epoch=evaluated_at_epoch,
    )
    known_client_order_ids = {
        value.strip() for value in prior_client_order_ids if value.strip()
    }
    is_duplicate = request.client_order_id in known_client_order_ids
    market_data_age_seconds = (
        evaluated_at_epoch - request.market_data_observed_at_epoch
    )
    projected_live_capital = (
        request.committed_live_capital + request.estimated_notional
    )
    projected_open_position_count = (
        request.open_position_count + (1 if request.side == "buy" else 0)
    )
    combined_evidence = _deduplicate(
        (*launch_control.evidence_references, *request.evidence_references)
    )

    checks = [
        (
            "launch_control_armed",
            effective_status is LiveLaunchControlStatus.ARMED,
        ),
        (
            "broker_match",
            request.broker_name == launch_control.broker_name,
        ),
        (
            "account_match",
            request.account_identifier == launch_control.account_identifier,
        ),
        (
            "asset_class_allowed",
            request.asset_class in launch_control.asset_classes,
        ),
        (
            "symbol_allowed",
            request.symbol in launch_control.symbols,
        ),
        (
            "order_type_allowed",
            request.order_type in launch_control.order_types,
        ),
        (
            "order_notional_within_limit",
            request.estimated_notional
            <= launch_control.maximum_order_notional,
        ),
        (
            "live_capital_within_limit",
            projected_live_capital <= launch_control.live_capital,
        ),
        (
            "daily_loss_within_limit",
            request.realized_daily_loss
            <= launch_control.maximum_daily_loss,
        ),
        (
            "open_positions_within_limit",
            projected_open_position_count
            <= launch_control.maximum_open_positions,
        ),
        (
            "market_data_not_from_future",
            market_data_age_seconds >= 0,
        ),
        (
            "market_data_fresh",
            0
            <= market_data_age_seconds
            <= admission_policy.maximum_market_data_age_seconds,
        ),
        (
            "operator_authorization",
            not admission_policy.require_operator_authorization
            or bool(request.operator_authorization_reference.strip()),
        ),
        (
            "evidence",
            not admission_policy.require_evidence or bool(combined_evidence),
        ),
        (
            "not_duplicate",
            not admission_policy.reject_duplicate_client_order_ids
            or not is_duplicate,
        ),
    ]

    passed_checks = tuple(name for name, passed in checks if passed)
    failed_checks = tuple(name for name, passed in checks if not passed)

    if effective_status is LiveLaunchControlStatus.SUSPENDED:
        status = LiveOrderAdmissionStatus.SUSPENDED
    elif effective_status is LiveLaunchControlStatus.EXPIRED:
        status = LiveOrderAdmissionStatus.EXPIRED
    elif is_duplicate and admission_policy.reject_duplicate_client_order_ids:
        status = LiveOrderAdmissionStatus.DUPLICATE
    elif failed_checks:
        status = LiveOrderAdmissionStatus.REJECTED
    else:
        status = LiveOrderAdmissionStatus.ADMITTED

    admission_id = deterministic_identifier(
        "live-order-admission",
        launch_control.launch_control_id,
        request.request_id,
        request.client_order_id,
        request.broker_name,
        request.account_identifier,
        request.asset_class,
        request.symbol,
        request.order_type,
        request.side,
        request.quantity,
        request.limit_price,
        request.estimated_notional,
        request.committed_live_capital,
        request.realized_daily_loss,
        request.open_position_count,
        request.market_data_observed_at_epoch,
        request.operator_authorization_reference,
        evaluated_at_epoch,
        status,
        *failed_checks,
    )

    return LiveOrderAdmission(
        admission_id=admission_id,
        launch_control_id=launch_control.launch_control_id,
        request_id=request.request_id,
        client_order_id=request.client_order_id,
        status=status,
        broker_name=request.broker_name,
        account_identifier=request.account_identifier,
        asset_class=request.asset_class,
        symbol=request.symbol,
        order_type=request.order_type,
        side=request.side,
        quantity=request.quantity,
        limit_price=request.limit_price,
        estimated_notional=request.estimated_notional,
        projected_live_capital=projected_live_capital,
        realized_daily_loss=request.realized_daily_loss,
        projected_open_position_count=projected_open_position_count,
        market_data_age_seconds=market_data_age_seconds,
        operator_authorization_reference=(
            request.operator_authorization_reference.strip()
        ),
        evidence_references=combined_evidence,
        passed_checks=passed_checks,
        failed_checks=failed_checks,
    )
