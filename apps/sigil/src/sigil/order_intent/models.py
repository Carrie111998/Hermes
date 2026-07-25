from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Mapping


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"


class OrderIntentStatus(StrEnum):
    READY_FOR_APPROVAL = "ready_for_approval"
    BLOCKED = "blocked"
    NO_ACTION = "no_action"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class OrderIntentPolicy:
    max_intents: int = 25
    minimum_order_notional: Decimal = Decimal("5.00")
    max_order_notional: Decimal = Decimal("1000.00")
    max_aggregate_buy_notional: Decimal = Decimal("2500.00")
    max_aggregate_sell_notional: Decimal = Decimal("2500.00")
    max_aggregate_turnover: Decimal = Decimal("5000.00")
    allow_market_orders: bool = True
    allow_limit_orders: bool = True
    allow_fractional_shares: bool = True
    allowed_time_in_force: tuple[TimeInForce, ...] = (
        TimeInForce.DAY,
        TimeInForce.GTC,
    )
    require_evidence: bool = True
    require_verified_source_identity: bool = True
    require_human_approval: bool = True
    require_limit_price_for_limit_orders: bool = True
    maximum_source_age_seconds: int | None = None
    quantity_precision: int = 6
    price_precision: int = 2
    notional_precision: int = 2

    def __post_init__(self) -> None:
        if self.max_intents <= 0:
            raise ValueError("max_intents must be positive")

        for name in (
            "minimum_order_notional",
            "max_order_notional",
            "max_aggregate_buy_notional",
            "max_aggregate_sell_notional",
            "max_aggregate_turnover",
        ):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} must be non-negative")

        if self.max_order_notional < self.minimum_order_notional:
            raise ValueError(
                "max_order_notional must be greater than or equal to "
                "minimum_order_notional"
            )

        if not self.allowed_time_in_force:
            raise ValueError("allowed_time_in_force must not be empty")

        if len(set(self.allowed_time_in_force)) != len(
            self.allowed_time_in_force
        ):
            raise ValueError("allowed_time_in_force must not contain duplicates")

        if (
            self.maximum_source_age_seconds is not None
            and self.maximum_source_age_seconds < 0
        ):
            raise ValueError(
                "maximum_source_age_seconds must be non-negative"
            )

        for name in (
            "quantity_precision",
            "price_precision",
            "notional_precision",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True, slots=True)
class AccountCapacity:
    available_buying_power: Decimal
    sellable_quantities: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.available_buying_power < 0:
            raise ValueError("available_buying_power must be non-negative")

        normalized: dict[str, Decimal] = {}
        for symbol, quantity in self.sellable_quantities.items():
            clean_symbol = symbol.strip().upper()
            if not clean_symbol:
                raise ValueError("sellable quantity symbol must not be empty")
            if quantity < 0:
                raise ValueError(
                    f"sellable quantity for {clean_symbol} must be non-negative"
                )
            if clean_symbol in normalized:
                raise ValueError(
                    f"duplicate sellable quantity symbol: {clean_symbol}"
                )
            normalized[clean_symbol] = quantity

        object.__setattr__(
            self,
            "sellable_quantities",
            dict(sorted(normalized.items())),
        )


@dataclass(frozen=True, slots=True)
class OrderIntentConstraint:
    name: str
    passed: bool
    observed: str
    limit: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("constraint name must not be empty")
        object.__setattr__(self, "name", self.name.strip())


@dataclass(frozen=True, slots=True)
class OrderIntent:
    intent_id: str
    source_proposal_id: str
    source_rebalance_package_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce
    quantity: Decimal
    reference_price: Decimal
    notional: Decimal
    limit_price: Decimal | None
    issuer: str
    sector: str
    rationale: tuple[str, ...]
    evidence_references: tuple[str, ...]
    constraints: tuple[OrderIntentConstraint, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    analytical_only: bool = True
    execution_authority: bool = False

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        issuer = self.issuer.strip()
        sector = self.sector.strip()

        if not symbol:
            raise ValueError("symbol must not be empty")
        if not issuer:
            raise ValueError("issuer must not be empty")
        if not sector:
            raise ValueError("sector must not be empty")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.reference_price <= 0:
            raise ValueError("reference_price must be positive")
        if self.notional <= 0:
            raise ValueError("notional must be positive")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("market orders must not define limit_price")
        if not self.source_proposal_id.strip():
            raise ValueError("source_proposal_id must not be empty")
        if not self.source_rebalance_package_id.strip():
            raise ValueError(
                "source_rebalance_package_id must not be empty"
            )
        if not self.analytical_only:
            raise ValueError("order intents must remain analytical only")
        if self.execution_authority:
            raise ValueError("order intents must not have execution authority")

        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "issuer", issuer)
        object.__setattr__(self, "sector", sector)
        object.__setattr__(
            self,
            "evidence_references",
            tuple(sorted(set(self.evidence_references))),
        )
        object.__setattr__(
            self,
            "blockers",
            tuple(sorted(set(self.blockers))),
        )
        object.__setattr__(
            self,
            "warnings",
            tuple(sorted(set(self.warnings))),
        )


@dataclass(frozen=True, slots=True)
class OrderIntentPackage:
    package_id: str
    source_rebalance_package_id: str
    source_target_package_id: str
    status: OrderIntentStatus
    intents: tuple[OrderIntent, ...]
    aggregate_buy_notional: Decimal
    aggregate_sell_notional: Decimal
    aggregate_turnover: Decimal
    constraints: tuple[OrderIntentConstraint, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    policy_snapshot: Mapping[str, str]
    evidence_references: tuple[str, ...]
    analytical_only: bool = True
    approval_required: bool = True
    approved: bool = False
    execution_authority: bool = False

    def __post_init__(self) -> None:
        if not self.source_rebalance_package_id.strip():
            raise ValueError(
                "source_rebalance_package_id must not be empty"
            )
        if not self.source_target_package_id.strip():
            raise ValueError("source_target_package_id must not be empty")
        if self.aggregate_buy_notional < 0:
            raise ValueError("aggregate_buy_notional must be non-negative")
        if self.aggregate_sell_notional < 0:
            raise ValueError("aggregate_sell_notional must be non-negative")
        if self.aggregate_turnover < 0:
            raise ValueError("aggregate_turnover must be non-negative")
        if not self.analytical_only:
            raise ValueError(
                "order-intent packages must remain analytical only"
            )
        if not self.approval_required:
            raise ValueError(
                "order-intent packages must require approval"
            )
        if self.approved:
            raise ValueError(
                "order-intent packages must not embed approval state"
            )
        if self.execution_authority:
            raise ValueError(
                "order-intent packages must not have execution authority"
            )

        object.__setattr__(
            self,
            "blockers",
            tuple(sorted(set(self.blockers))),
        )
        object.__setattr__(
            self,
            "warnings",
            tuple(sorted(set(self.warnings))),
        )
        object.__setattr__(
            self,
            "evidence_references",
            tuple(sorted(set(self.evidence_references))),
        )


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    request_id: str
    order_intent_package_id: str
    source_rebalance_package_id: str
    intent_ids: tuple[str, ...]
    requested_action: str
    summary: str
    aggregate_buy_notional: Decimal
    aggregate_sell_notional: Decimal
    aggregate_turnover: Decimal
    constraint_summary: tuple[str, ...]
    evidence_references: tuple[str, ...]
    created_at: str
    expires_at: str | None
    required_approver_role: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    approval_does_not_equal_execution: bool = True

    def __post_init__(self) -> None:
        if not self.order_intent_package_id.strip():
            raise ValueError(
                "order_intent_package_id must not be empty"
            )
        if not self.source_rebalance_package_id.strip():
            raise ValueError(
                "source_rebalance_package_id must not be empty"
            )
        if not self.intent_ids:
            raise ValueError("intent_ids must not be empty")
        if not self.requested_action.strip():
            raise ValueError("requested_action must not be empty")
        if not self.summary.strip():
            raise ValueError("summary must not be empty")
        if not self.created_at.strip():
            raise ValueError("created_at must not be empty")
        if self.expires_at is not None and not self.expires_at.strip():
            raise ValueError("expires_at must not be empty")
        if not self.required_approver_role.strip():
            raise ValueError(
                "required_approver_role must not be empty"
            )
        if self.status is not ApprovalStatus.PENDING:
            raise ValueError("approval requests must begin pending")
        if not self.approval_does_not_equal_execution:
            raise ValueError(
                "approval request must preserve execution boundary"
            )

        object.__setattr__(
            self,
            "intent_ids",
            tuple(sorted(set(self.intent_ids))),
        )
        object.__setattr__(
            self,
            "evidence_references",
            tuple(sorted(set(self.evidence_references))),
        )


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    record_id: str
    request_id: str
    order_intent_package_id: str
    decision: ApprovalDecision
    status: ApprovalStatus
    approver_identity: str
    decided_at: str
    reason: str | None
    downstream_consideration_only: bool = True
    execution_authority: bool = False

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id must not be empty")
        if not self.order_intent_package_id.strip():
            raise ValueError(
                "order_intent_package_id must not be empty"
            )
        if not self.approver_identity.strip():
            raise ValueError("approver_identity must not be empty")
        if not self.decided_at.strip():
            raise ValueError("decided_at must not be empty")
        if self.status not in (
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
        ):
            raise ValueError("approval record status must be final")
        if not self.downstream_consideration_only:
            raise ValueError(
                "approval must authorize downstream consideration only"
            )
        if self.execution_authority:
            raise ValueError(
                "approval records must not have execution authority"
            )


@dataclass(frozen=True, slots=True)
class OrderIntentComparison:
    left_package_id: str
    right_package_id: str
    added_symbols: tuple[str, ...]
    removed_symbols: tuple[str, ...]
    changed_sides: Mapping[str, tuple[OrderSide, OrderSide]]
    changed_quantities: Mapping[str, tuple[Decimal, Decimal]]
    changed_notionals: Mapping[str, tuple[Decimal, Decimal]]
    changed_order_types: Mapping[str, tuple[OrderType, OrderType]]
    changed_limit_prices: Mapping[
        str,
        tuple[Decimal | None, Decimal | None],
    ]
    status_changed: bool
    blockers_changed: bool
    policy_changed: bool
