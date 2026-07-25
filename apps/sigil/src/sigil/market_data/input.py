"""Caller-supplied input for governed market-data construction."""

from __future__ import annotations

from dataclasses import dataclass, field

from sigil.accounting.models import canonical_digest

from .models import MarketDataObservation, MarketDataValidationError


@dataclass(frozen=True, slots=True)
class GovernedMarketDataInput:
    instrument_id: str
    as_of: str
    as_of_epoch_seconds: int
    observations: tuple[MarketDataObservation, ...]
    policy_identity: str
    upstream_package_identities: tuple[str, ...] = ()
    request_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.instrument_id:
            raise MarketDataValidationError("instrument_id must not be empty")
        if not self.as_of:
            raise MarketDataValidationError("as_of must not be empty")
        if self.as_of_epoch_seconds < 0:
            raise MarketDataValidationError(
                "as_of_epoch_seconds must be nonnegative"
            )
        if not self.observations:
            raise MarketDataValidationError("observations must not be empty")
        if not self.policy_identity:
            raise MarketDataValidationError("policy_identity must not be empty")

        canonical_observations = tuple(
            sorted(
                self.observations,
                key=lambda item: (
                    item.field_name,
                    item.observed_at,
                    item.source_id,
                    item.observation_id,
                ),
            )
        )

        object.__setattr__(
            self,
            "request_identity",
            canonical_digest(
                {
                    "instrument_id": self.instrument_id,
                    "as_of": self.as_of,
                    "as_of_epoch_seconds": self.as_of_epoch_seconds,
                    "observations": canonical_observations,
                    "policy_identity": self.policy_identity,
                    "upstream_package_identities": tuple(
                        sorted(self.upstream_package_identities)
                    ),
                }
            ),
        )
