from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Mapping


class AllocationMethod(StrEnum):
    EQUAL_WEIGHT = "equal_weight"
    SCORE_WEIGHTED = "score_weighted"
    CONSTRAINED_SCORE_WEIGHTED = "constrained_score_weighted"


class ConstructionStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class CandidateAsset:
    symbol: str
    score: Decimal
    price: Decimal
    liquidity_score: Decimal
    volatility: Decimal
    issuer: str
    sector: str
    approved: bool = True
    thesis_reference: str | None = None
    valuation_reference: str | None = None
    risk_reference: str | None = None

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
        if self.price <= 0:
            raise ValueError("price must be positive")
        if self.score < 0:
            raise ValueError("score must be non-negative")
        if not Decimal("0") <= self.liquidity_score <= Decimal("1"):
            raise ValueError("liquidity_score must be between 0 and 1")
        if self.volatility < 0:
            raise ValueError("volatility must be non-negative")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "issuer", issuer)
        object.__setattr__(self, "sector", sector)


@dataclass(frozen=True, slots=True)
class ConstructionPolicy:
    min_positions: int = 1
    max_positions: int = 20
    max_position_weight: Decimal = Decimal("0.10")
    max_issuer_weight: Decimal = Decimal("0.15")
    max_sector_weight: Decimal = Decimal("0.30")
    max_gross_exposure: Decimal = Decimal("1.00")
    min_liquidity_score: Decimal = Decimal("0.50")
    max_asset_volatility: Decimal = Decimal("1.00")
    cash_reserve_weight: Decimal = Decimal("0.00")
    weight_precision: int = 6
    require_evidence_references: bool = True

    def __post_init__(self) -> None:
        if self.min_positions < 1:
            raise ValueError("min_positions must be at least 1")
        if self.max_positions < self.min_positions:
            raise ValueError("max_positions must be >= min_positions")
        for name in (
            "max_position_weight",
            "max_issuer_weight",
            "max_sector_weight",
            "max_gross_exposure",
            "min_liquidity_score",
            "cash_reserve_weight",
        ):
            value = getattr(self, name)
            if not Decimal("0") <= value <= Decimal("1"):
                raise ValueError(f"{name} must be between 0 and 1")
        if self.max_asset_volatility < 0:
            raise ValueError("max_asset_volatility must be non-negative")
        if self.cash_reserve_weight >= self.max_gross_exposure:
            raise ValueError("cash reserve must be below max gross exposure")
        if self.weight_precision < 0:
            raise ValueError("weight_precision must be non-negative")


@dataclass(frozen=True, slots=True)
class PositionProposal:
    symbol: str
    target_weight: Decimal
    target_value: Decimal
    estimated_shares: Decimal
    issuer: str
    sector: str
    score: Decimal
    rationale: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Exclusion:
    symbol: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConstraintResult:
    name: str
    passed: bool
    observed: str
    limit: str


@dataclass(frozen=True, slots=True)
class ConstructionPackage:
    package_id: str
    method: AllocationMethod
    status: ConstructionStatus
    capital: Decimal
    invested_weight: Decimal
    cash_weight: Decimal
    positions: tuple[PositionProposal, ...]
    exclusions: tuple[Exclusion, ...]
    constraints: tuple[ConstraintResult, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    evidence_references: tuple[str, ...]
    policy_snapshot: Mapping[str, str]
    analytical_only: bool = True
    execution_authority: bool = False


@dataclass(frozen=True, slots=True)
class PackageComparison:
    left_package_id: str
    right_package_id: str
    added_symbols: tuple[str, ...]
    removed_symbols: tuple[str, ...]
    weight_changes: Mapping[str, Decimal] = field(default_factory=dict)
    status_changed: bool = False
