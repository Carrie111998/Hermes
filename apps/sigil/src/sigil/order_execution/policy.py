from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from sigil.order_intent.models import OrderType, TimeInForce

from .models import ExecutionEnvironment


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    max_orders: int = 25
    max_order_quantity: Decimal = Decimal("1000000")
    max_order_notional: Decimal = Decimal("1000.00")
    max_aggregate_buy_notional: Decimal = Decimal("2500.00")
    max_aggregate_sell_notional: Decimal = Decimal("2500.00")
    max_aggregate_turnover: Decimal = Decimal("5000.00")
    allowed_providers: tuple[str, ...] = ("paper",)
    allowed_account_ids: tuple[str, ...] = ()
    allowed_account_classes: tuple[str, ...] = ("standard",)
    allowed_environments: tuple[ExecutionEnvironment, ...] = (
        ExecutionEnvironment.PAPER,
        ExecutionEnvironment.SANDBOX,
    )
    allowed_order_types: tuple[OrderType, ...] = (
        OrderType.MARKET,
        OrderType.LIMIT,
    )
    allowed_time_in_force: tuple[TimeInForce, ...] = (
        TimeInForce.DAY,
        TimeInForce.GTC,
    )
    allow_live_execution: bool = False
    require_human_approval: bool = True
    require_evidence: bool = True
    require_verified_approver_identity: bool = True
    maximum_approval_age_seconds: int | None = 3600
    maximum_intent_package_age_seconds: int | None = None
    maximum_acknowledgement_age_seconds: int | None = 3600
    maximum_broker_evidence_age_seconds: int | None = 3600
    quantity_tolerance: Decimal = Decimal("0.000001")
    price_tolerance: Decimal = Decimal("0.01")
    notional_tolerance: Decimal = Decimal("0.01")
    maximum_fee: Decimal = Decimal("25.00")
    maximum_slippage_basis_points: Decimal = Decimal("100.00")
    allow_partial_fills: bool = True
    unknown_provider_orders_are_blocking: bool = True
    duplicate_execution_evidence_is_blocking: bool = True
    quantity_precision: int = 6
    price_precision: int = 2
    notional_precision: int = 2
    fee_precision: int = 2

    def __post_init__(self) -> None:
        if self.max_orders <= 0:
            raise ValueError("max_orders must be positive")

        for name in (
            "max_order_quantity",
            "max_order_notional",
            "max_aggregate_buy_notional",
            "max_aggregate_sell_notional",
            "max_aggregate_turnover",
            "maximum_fee",
        ):
            value = getattr(self, name)
            if value <= 0:
                raise ValueError(f"{name} must be positive")

        for name in (
            "quantity_tolerance",
            "price_tolerance",
            "notional_tolerance",
            "maximum_slippage_basis_points",
        ):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} must be non-negative")

        if not self.allowed_providers:
            raise ValueError("allowed_providers must not be empty")
        if not self.allowed_account_classes:
            raise ValueError("allowed_account_classes must not be empty")
        if not self.allowed_environments:
            raise ValueError("allowed_environments must not be empty")
        if not self.allowed_order_types:
            raise ValueError("allowed_order_types must not be empty")
        if not self.allowed_time_in_force:
            raise ValueError("allowed_time_in_force must not be empty")

        normalized_providers = tuple(
            sorted(
                {
                    provider.strip().lower()
                    for provider in self.allowed_providers
                    if provider.strip()
                }
            )
        )
        if not normalized_providers:
            raise ValueError("allowed_providers must contain valid values")

        normalized_account_ids = tuple(
            sorted(
                {
                    account_id.strip()
                    for account_id in self.allowed_account_ids
                    if account_id.strip()
                }
            )
        )
        normalized_account_classes = tuple(
            sorted(
                {
                    account_class.strip().lower()
                    for account_class in self.allowed_account_classes
                    if account_class.strip()
                }
            )
        )
        if not normalized_account_classes:
            raise ValueError(
                "allowed_account_classes must contain valid values"
            )

        object.__setattr__(
            self,
            "allowed_providers",
            normalized_providers,
        )
        object.__setattr__(
            self,
            "allowed_account_ids",
            normalized_account_ids,
        )
        object.__setattr__(
            self,
            "allowed_account_classes",
            normalized_account_classes,
        )
        object.__setattr__(
            self,
            "allowed_environments",
            tuple(sorted(set(self.allowed_environments), key=str)),
        )
        object.__setattr__(
            self,
            "allowed_order_types",
            tuple(sorted(set(self.allowed_order_types), key=str)),
        )
        object.__setattr__(
            self,
            "allowed_time_in_force",
            tuple(sorted(set(self.allowed_time_in_force), key=str)),
        )

        if (
            ExecutionEnvironment.LIVE in self.allowed_environments
            and not self.allow_live_execution
        ):
            raise ValueError(
                "live environment requires allow_live_execution=True"
            )

        for name in (
            "maximum_approval_age_seconds",
            "maximum_intent_package_age_seconds",
            "maximum_acknowledgement_age_seconds",
            "maximum_broker_evidence_age_seconds",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")

        for name in (
            "quantity_precision",
            "price_precision",
            "notional_precision",
            "fee_precision",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")

    def snapshot(self) -> Mapping[str, str]:
        return {
            "allow_live_execution": str(self.allow_live_execution).lower(),
            "allow_partial_fills": str(self.allow_partial_fills).lower(),
            "allowed_account_classes": ",".join(
                self.allowed_account_classes
            ),
            "allowed_account_ids": ",".join(self.allowed_account_ids),
            "allowed_environments": ",".join(
                environment.value
                for environment in self.allowed_environments
            ),
            "allowed_order_types": ",".join(
                order_type.value
                for order_type in self.allowed_order_types
            ),
            "allowed_providers": ",".join(self.allowed_providers),
            "allowed_time_in_force": ",".join(
                time_in_force.value
                for time_in_force in self.allowed_time_in_force
            ),
            "duplicate_execution_evidence_is_blocking": str(
                self.duplicate_execution_evidence_is_blocking
            ).lower(),
            "fee_precision": str(self.fee_precision),
            "max_aggregate_buy_notional": str(
                self.max_aggregate_buy_notional
            ),
            "max_aggregate_sell_notional": str(
                self.max_aggregate_sell_notional
            ),
            "max_aggregate_turnover": str(
                self.max_aggregate_turnover
            ),
            "max_order_notional": str(self.max_order_notional),
            "max_order_quantity": str(self.max_order_quantity),
            "max_orders": str(self.max_orders),
            "maximum_acknowledgement_age_seconds": str(
                self.maximum_acknowledgement_age_seconds
            ),
            "maximum_approval_age_seconds": str(
                self.maximum_approval_age_seconds
            ),
            "maximum_broker_evidence_age_seconds": str(
                self.maximum_broker_evidence_age_seconds
            ),
            "maximum_fee": str(self.maximum_fee),
            "maximum_intent_package_age_seconds": str(
                self.maximum_intent_package_age_seconds
            ),
            "maximum_slippage_basis_points": str(
                self.maximum_slippage_basis_points
            ),
            "notional_precision": str(self.notional_precision),
            "notional_tolerance": str(self.notional_tolerance),
            "price_precision": str(self.price_precision),
            "price_tolerance": str(self.price_tolerance),
            "quantity_precision": str(self.quantity_precision),
            "quantity_tolerance": str(self.quantity_tolerance),
            "require_evidence": str(self.require_evidence).lower(),
            "require_human_approval": str(
                self.require_human_approval
            ).lower(),
            "require_verified_approver_identity": str(
                self.require_verified_approver_identity
            ).lower(),
            "unknown_provider_orders_are_blocking": str(
                self.unknown_provider_orders_are_blocking
            ).lower(),
        }
