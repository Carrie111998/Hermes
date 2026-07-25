"""Immutable models for governed portfolio-risk analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sigil.accounting.models import canonical_digest


class RiskValidationError(ValueError):
    """Raised when governed risk input violates required constraints."""


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


class RiskSeverity(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class RiskDisposition(str, Enum):
    ACCEPTABLE = "acceptable"
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"


class RiskMetricKind(str, Enum):
    GROSS_EXPOSURE = "gross_exposure"
    NET_EXPOSURE = "net_exposure"
    LONG_EXPOSURE = "long_exposure"
    SHORT_EXPOSURE = "short_exposure"
    LEVERAGE = "leverage"
    POSITION_CONCENTRATION = "position_concentration"
    ISSUER_CONCENTRATION = "issuer_concentration"
    SECTOR_CONCENTRATION = "sector_concentration"
    LIQUIDITY = "liquidity"
    VOLATILITY = "volatility"
    DRAWDOWN = "drawdown"


@dataclass(frozen=True, slots=True)
class RiskPosition:
    position_id: str
    instrument_id: str
    issuer_id: str
    sector_id: str
    side: PositionSide
    market_value: str
    average_daily_volume_value: str
    annualized_volatility: str
    peak_to_trough_drawdown: str
    evidence_references: tuple[str, ...]
    source_id: str
    position_identity: str = field(init=False)

    def __post_init__(self) -> None:
        required = {
            "position_id": self.position_id,
            "instrument_id": self.instrument_id,
            "issuer_id": self.issuer_id,
            "sector_id": self.sector_id,
            "market_value": self.market_value,
            "average_daily_volume_value": self.average_daily_volume_value,
            "annualized_volatility": self.annualized_volatility,
            "peak_to_trough_drawdown": self.peak_to_trough_drawdown,
            "source_id": self.source_id,
        }
        for name, value in required.items():
            if not value:
                raise RiskValidationError(f"{name} must not be empty")
        if not isinstance(self.side, PositionSide):
            raise RiskValidationError("side must be PositionSide")
        object.__setattr__(
            self,
            "evidence_references",
            tuple(sorted(set(self.evidence_references))),
        )
        object.__setattr__(
            self,
            "position_identity",
            canonical_digest(
                {
                    name: getattr(self, name)
                    for name in self.__dataclass_fields__
                    if name != "position_identity"
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class RiskMetric:
    kind: RiskMetricKind
    value: str
    limit: str
    severity: RiskSeverity
    breached: bool
    subject_id: str
    explanation: str
    evidence_references: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RiskProvenance:
    request_identity: str
    policy_identity: str
    source_ids: tuple[str, ...]
    input_position_identities: tuple[str, ...]
    upstream_package_identities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GovernedRiskPackage:
    portfolio_id: str
    as_of: str
    metrics: tuple[RiskMetric, ...]
    severity: RiskSeverity
    disposition: RiskDisposition
    risk_score: int
    breached_limits: tuple[str, ...]
    readiness_blockers: tuple[str, ...]
    review_reasons: tuple[str, ...]
    provenance: RiskProvenance
    analytical_only: bool = True
    authorizes_trading: bool = False
    authorizes_capital_allocation: bool = False
    mutates_positions: bool = False
    submits_orders: bool = False
    package_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if not 0 <= self.risk_score <= 100:
            raise RiskValidationError("risk_score must be between 0 and 100")
        object.__setattr__(
            self,
            "package_identity",
            canonical_digest(
                {
                    name: getattr(self, name)
                    for name in self.__dataclass_fields__
                    if name != "package_identity"
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class RiskComparison:
    before_identity: str
    after_identity: str
    risk_score_change: tuple[int, int]
    severity_change: tuple[str, str] | None
    disposition_change: tuple[str, str] | None
    added_breaches: tuple[str, ...]
    resolved_breaches: tuple[str, ...]


def position_material(position: RiskPosition) -> dict[str, Any]:
    return {
        name: getattr(position, name)
        for name in position.__dataclass_fields__
        if name != "position_identity"
    }
