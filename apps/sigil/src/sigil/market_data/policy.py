"""Policy for deterministic governed market-data normalization."""

from __future__ import annotations

from dataclasses import dataclass, field

from sigil.accounting.models import canonical_digest

from .models import MarketDataKind, MarketDataValidationError


@dataclass(frozen=True, slots=True)
class GovernedMarketDataPolicy:
    permitted_kinds: tuple[MarketDataKind, ...] = (
        MarketDataKind.QUOTE,
        MarketDataKind.TRADE,
        MarketDataKind.BAR,
        MarketDataKind.REFERENCE,
    )
    permitted_sources: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ("last_price",)
    maximum_age_seconds: int = 900
    expiration_age_seconds: int = 86400
    require_evidence_references: bool = True
    reject_duplicate_observation_ids: bool = True
    policy_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if self.maximum_age_seconds < 0:
            raise MarketDataValidationError(
                "maximum_age_seconds must be nonnegative"
            )
        if self.expiration_age_seconds < self.maximum_age_seconds:
            raise MarketDataValidationError(
                "expiration_age_seconds must be at least maximum_age_seconds"
            )
        if not self.permitted_kinds:
            raise MarketDataValidationError("permitted_kinds must not be empty")
        if not self.required_fields:
            raise MarketDataValidationError("required_fields must not be empty")

        object.__setattr__(
            self,
            "policy_identity",
            canonical_digest(
                {
                    "permitted_kinds": tuple(item.value for item in self.permitted_kinds),
                    "permitted_sources": self.permitted_sources,
                    "required_fields": self.required_fields,
                    "maximum_age_seconds": self.maximum_age_seconds,
                    "expiration_age_seconds": self.expiration_age_seconds,
                    "require_evidence_references": self.require_evidence_references,
                    "reject_duplicate_observation_ids": (
                        self.reject_duplicate_observation_ids
                    ),
                }
            ),
        )

    def permits_kind(self, kind: MarketDataKind) -> bool:
        return kind in self.permitted_kinds

    def permits_source(self, source_id: str) -> bool:
        return not self.permitted_sources or source_id in self.permitted_sources
