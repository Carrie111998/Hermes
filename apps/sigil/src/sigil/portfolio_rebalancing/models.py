from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Mapping


class RebalanceAction(StrEnum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class RebalanceStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"
    NO_ACTION = "no_action"


@dataclass(frozen=True, slots=True)
class CurrentPosition:
    symbol: str
    quantity: Decimal
    price: Decimal
    issuer: str
    sector: str

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol must not be empty")
        if self.quantity < 0:
            raise ValueError("quantity must be non-negative")
        if self.price <= 0:
            raise ValueError("price must be positive")
        if not self.issuer.strip():
            raise ValueError("issuer must not be empty")
        if not self.sector.strip():
            raise ValueError("sector must not be empty")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "issuer", self.issuer.strip())
        object.__setattr__(self, "sector", self.sector.strip())

    @property
    def market_value(self) -> Decimal:
        return self.quantity * self.price


@dataclass(frozen=True, slots=True)
class TargetPosition:
    symbol: str
    target_weight: Decimal
    reference_price: Decimal
    issuer: str
    sector: str

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol must not be empty")
        if not Decimal("0") <= self.target_weight <= Decimal("1"):
            raise ValueError("target_weight must be between 0 and 1")
        if self.reference_price <= 0:
            raise ValueError("reference_price must be positive")
        object.__setattr__(self, "symbol", symbol)


@dataclass(frozen=True, slots=True)
class RebalancingPolicy:
    max_turnover_weight: Decimal = Decimal("0.20")
    minimum_trade_value: Decimal = Decimal("5.00")
    drift_tolerance: Decimal = Decimal("0.01")
    max_single_trade_weight: Decimal = Decimal("0.10")
    allow_new_positions: bool = True
    allow_full_exits: bool = True
    require_approved_target_package: bool = True
    weight_precision: int = 6
    share_precision: int = 6

    def __post_init__(self) -> None:
        for name in (
            "max_turnover_weight",
            "drift_tolerance",
            "max_single_trade_weight",
        ):
            value = getattr(self, name)
            if not Decimal("0") <= value <= Decimal("1"):
                raise ValueError(f"{name} must be between 0 and 1")
        if self.minimum_trade_value < 0:
            raise ValueError("minimum_trade_value must be non-negative")
        if self.weight_precision < 0 or self.share_precision < 0:
            raise ValueError("precision must be non-negative")


@dataclass(frozen=True, slots=True)
class TradeProposal:
    symbol: str
    action: RebalanceAction
    current_weight: Decimal
    target_weight: Decimal
    drift_weight: Decimal
    proposed_weight: Decimal
    proposed_value: Decimal
    proposed_quantity: Decimal
    price: Decimal
    issuer: str
    sector: str
    rationale: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RebalanceConstraint:
    name: str
    passed: bool
    observed: str
    limit: str


@dataclass(frozen=True, slots=True)
class RebalancePackage:
    package_id: str
    source_target_package_id: str
    status: RebalanceStatus
    portfolio_value: Decimal
    proposed_turnover_weight: Decimal
    proposals: tuple[TradeProposal, ...]
    constraints: tuple[RebalanceConstraint, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    policy_snapshot: Mapping[str, str]
    evidence_references: tuple[str, ...]
    analytical_only: bool = True
    execution_authority: bool = False


@dataclass(frozen=True, slots=True)
class RebalanceComparison:
    left_package_id: str
    right_package_id: str
    added_trade_symbols: tuple[str, ...]
    removed_trade_symbols: tuple[str, ...]
    value_changes: Mapping[str, Decimal] = field(default_factory=dict)
    status_changed: bool = False
