"""Immutable models for governed market-data packages."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sigil.accounting.models import canonical_digest


class MarketDataValidationError(ValueError):
    """Raised when governed market-data inputs violate policy or schema."""


class MarketDataKind(str, Enum):
    QUOTE = "quote"
    TRADE = "trade"
    BAR = "bar"
    REFERENCE = "reference"


class MarketDataQuality(str, Enum):
    VERIFIED = "verified"
    ACCEPTABLE = "acceptable"
    DEGRADED = "degraded"
    REJECTED = "rejected"


class MarketDataFreshness(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class MarketDataObservation:
    observation_id: str
    instrument_id: str
    kind: MarketDataKind
    field_name: str
    value: str
    unit: str
    observed_at: str
    received_at: str
    source_id: str
    source_sequence: str | None = None
    evidence_references: tuple[str, ...] = ()
    observation_identity: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "observation_id",
            "instrument_id",
            "field_name",
            "value",
            "unit",
            "observed_at",
            "received_at",
            "source_id",
        ):
            if not getattr(self, name):
                raise MarketDataValidationError(f"{name} must not be empty")

        object.__setattr__(
            self,
            "observation_identity",
            canonical_digest(
                {
                    "observation_id": self.observation_id,
                    "instrument_id": self.instrument_id,
                    "kind": self.kind.value,
                    "field_name": self.field_name,
                    "value": self.value,
                    "unit": self.unit,
                    "observed_at": self.observed_at,
                    "received_at": self.received_at,
                    "source_id": self.source_id,
                    "source_sequence": self.source_sequence,
                    "evidence_references": self.evidence_references,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class MarketDataProvenance:
    request_identity: str
    policy_identity: str
    source_ids: tuple[str, ...]
    input_observation_identities: tuple[str, ...]
    upstream_package_identities: tuple[str, ...] = ()
    provenance_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.request_identity:
            raise MarketDataValidationError("request_identity must not be empty")
        if not self.policy_identity:
            raise MarketDataValidationError("policy_identity must not be empty")
        if not self.source_ids:
            raise MarketDataValidationError("source_ids must not be empty")

        object.__setattr__(
            self,
            "provenance_identity",
            canonical_digest(
                {
                    "request_identity": self.request_identity,
                    "policy_identity": self.policy_identity,
                    "source_ids": self.source_ids,
                    "input_observation_identities": self.input_observation_identities,
                    "upstream_package_identities": self.upstream_package_identities,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class GovernedMarketDataPackage:
    instrument_id: str
    as_of: str
    observations: tuple[MarketDataObservation, ...]
    quality: MarketDataQuality
    freshness: MarketDataFreshness
    quality_reasons: tuple[str, ...]
    readiness_blockers: tuple[str, ...]
    provenance: MarketDataProvenance
    analytical_only: bool = True
    trading_authorized: bool = False
    package_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.instrument_id:
            raise MarketDataValidationError("instrument_id must not be empty")
        if not self.as_of:
            raise MarketDataValidationError("as_of must not be empty")
        if not self.observations:
            raise MarketDataValidationError("observations must not be empty")
        if any(item.instrument_id != self.instrument_id for item in self.observations):
            raise MarketDataValidationError(
                "all observations must reference the package instrument"
            )
        if not self.analytical_only:
            raise MarketDataValidationError("market-data packages must remain analytical")
        if self.trading_authorized:
            raise MarketDataValidationError(
                "market-data packages cannot authorize trading"
            )

        object.__setattr__(
            self,
            "package_identity",
            canonical_digest(
                {
                    "instrument_id": self.instrument_id,
                    "as_of": self.as_of,
                    "observations": self.observations,
                    "quality": self.quality.value,
                    "freshness": self.freshness.value,
                    "quality_reasons": self.quality_reasons,
                    "readiness_blockers": self.readiness_blockers,
                    "provenance": self.provenance,
                    "analytical_only": self.analytical_only,
                    "trading_authorized": self.trading_authorized,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class MarketDataComparison:
    before_identity: str
    after_identity: str
    added_observation_ids: tuple[str, ...]
    removed_observation_ids: tuple[str, ...]
    changed_observation_ids: tuple[str, ...]
    quality_change: tuple[str, str] | None
    freshness_change: tuple[str, str] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "before_identity": self.before_identity,
            "after_identity": self.after_identity,
            "added_observation_ids": self.added_observation_ids,
            "removed_observation_ids": self.removed_observation_ids,
            "changed_observation_ids": self.changed_observation_ids,
            "quality_change": self.quality_change,
            "freshness_change": self.freshness_change,
        }
