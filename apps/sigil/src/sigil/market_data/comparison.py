"""Deterministic comparison of governed market-data packages."""

from __future__ import annotations

from .models import (
    GovernedMarketDataPackage,
    MarketDataComparison,
    MarketDataValidationError,
)


def compare_market_data_packages(
    before: GovernedMarketDataPackage,
    after: GovernedMarketDataPackage,
) -> MarketDataComparison:
    if before.instrument_id != after.instrument_id:
        raise MarketDataValidationError(
            "market-data comparison requires the same instrument"
        )

    old = {
        item.observation_id: item.observation_identity
        for item in before.observations
    }
    new = {
        item.observation_id: item.observation_identity
        for item in after.observations
    }

    return MarketDataComparison(
        before_identity=before.package_identity,
        after_identity=after.package_identity,
        added_observation_ids=tuple(sorted(new.keys() - old.keys())),
        removed_observation_ids=tuple(sorted(old.keys() - new.keys())),
        changed_observation_ids=tuple(
            sorted(
                key
                for key in old.keys() & new.keys()
                if old[key] != new[key]
            )
        ),
        quality_change=(
            None
            if before.quality == after.quality
            else (before.quality.value, after.quality.value)
        ),
        freshness_change=(
            None
            if before.freshness == after.freshness
            else (before.freshness.value, after.freshness.value)
        ),
    )
