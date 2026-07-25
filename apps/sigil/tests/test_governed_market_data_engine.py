"""Tests for Sigil Step 17 governed market-data engine."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from sigil.market_data import (
    GovernedMarketDataInput,
    GovernedMarketDataPolicy,
    MarketDataFreshness,
    MarketDataKind,
    MarketDataObservation,
    MarketDataQuality,
    MarketDataValidationError,
    compare_market_data_packages,
    construct_governed_market_data_package,
    list_readiness_blockers,
    list_sources,
    verify_package_identity,
)


AS_OF = "2026-07-24T21:00:00Z"
AS_OF_EPOCH = 1784926800


def observation(
    *,
    observation_id: str = "quote-last-primary",
    field_name: str = "last_price",
    value: str = "125.50",
    source_id: str = "primary-feed",
    observed_at: str = "2026-07-24T20:59:30Z",
    received_at: str = "2026-07-24T20:59:31Z",
    evidence_references: tuple[str, ...] = ("evidence://primary/quote/1",),
) -> MarketDataObservation:
    return MarketDataObservation(
        observation_id=observation_id,
        instrument_id="security-acme-common",
        kind=MarketDataKind.QUOTE,
        field_name=field_name,
        value=value,
        unit="USD",
        observed_at=observed_at,
        received_at=received_at,
        source_id=source_id,
        source_sequence="1001",
        evidence_references=evidence_references,
    )


def request(
    policy: GovernedMarketDataPolicy,
    *observations: MarketDataObservation,
) -> GovernedMarketDataInput:
    return GovernedMarketDataInput(
        instrument_id="security-acme-common",
        as_of=AS_OF,
        as_of_epoch_seconds=AS_OF_EPOCH,
        observations=observations or (observation(),),
        policy_identity=policy.policy_identity,
        upstream_package_identities=("valuation-package-identity",),
    )


def test_constructs_deterministic_acceptable_package() -> None:
    policy = GovernedMarketDataPolicy()
    package = construct_governed_market_data_package(request(policy), policy)

    assert package.instrument_id == "security-acme-common"
    assert package.quality is MarketDataQuality.ACCEPTABLE
    assert package.freshness is MarketDataFreshness.FRESH
    assert package.analytical_only is True
    assert package.trading_authorized is False
    assert list_sources(package) == ("primary-feed",)
    assert list_readiness_blockers(package) == ()
    assert verify_package_identity(package)


def test_two_sources_produce_verified_quality() -> None:
    policy = GovernedMarketDataPolicy()
    package = construct_governed_market_data_package(
        request(
            policy,
            observation(),
            observation(
                observation_id="quote-last-secondary",
                source_id="secondary-feed",
                value="125.49",
            ),
        ),
        policy,
    )

    assert package.quality is MarketDataQuality.VERIFIED
    assert list_sources(package) == ("primary-feed", "secondary-feed")


def test_sorting_and_identity_are_input_order_independent() -> None:
    policy = GovernedMarketDataPolicy()
    first = observation()
    second = observation(
        observation_id="quote-bid-primary",
        field_name="bid",
        value="125.40",
    )

    package_a = construct_governed_market_data_package(
        request(policy, first, second), policy
    )
    package_b = construct_governed_market_data_package(
        request(policy, second, first), policy
    )

    assert package_a.package_identity == package_b.package_identity
    assert tuple(item.field_name for item in package_a.observations) == (
        "bid",
        "last_price",
    )


def test_missing_required_field_is_rejected() -> None:
    policy = GovernedMarketDataPolicy(required_fields=("last_price", "volume"))
    package = construct_governed_market_data_package(request(policy), policy)

    assert package.quality is MarketDataQuality.REJECTED
    assert package.readiness_blockers == ("missing-required-field:volume",)


def test_stale_data_is_degraded() -> None:
    policy = GovernedMarketDataPolicy(
        maximum_age_seconds=10,
        expiration_age_seconds=120,
    )
    package = construct_governed_market_data_package(
        request(
            policy,
            observation(
                observed_at="2026-07-24T20:58:30Z",
                received_at="2026-07-24T20:58:31Z",
            ),
        ),
        policy,
    )

    assert package.freshness is MarketDataFreshness.STALE
    assert package.quality is MarketDataQuality.DEGRADED
    assert package.readiness_blockers == ("stale-market-data",)


def test_expired_data_is_rejected() -> None:
    policy = GovernedMarketDataPolicy(
        maximum_age_seconds=10,
        expiration_age_seconds=20,
    )
    package = construct_governed_market_data_package(
        request(
            policy,
            observation(
                observed_at="2026-07-24T20:00:00Z",
                received_at="2026-07-24T20:00:01Z",
            ),
        ),
        policy,
    )

    assert package.freshness is MarketDataFreshness.EXPIRED
    assert package.quality is MarketDataQuality.REJECTED
    assert package.readiness_blockers == ("expired-market-data",)


def test_policy_identity_mismatch_is_rejected() -> None:
    policy = GovernedMarketDataPolicy()
    bad_request = replace(request(policy), policy_identity="wrong-policy")

    with pytest.raises(MarketDataValidationError, match="policy mismatch"):
        construct_governed_market_data_package(bad_request, policy)


def test_unpermitted_source_is_rejected() -> None:
    policy = GovernedMarketDataPolicy(permitted_sources=("approved-feed",))

    with pytest.raises(MarketDataValidationError, match="not permitted"):
        construct_governed_market_data_package(request(policy), policy)


def test_missing_evidence_is_rejected() -> None:
    policy = GovernedMarketDataPolicy()

    with pytest.raises(MarketDataValidationError, match="evidence"):
        construct_governed_market_data_package(
            request(policy, observation(evidence_references=())),
            policy,
        )


def test_duplicate_observation_ids_are_rejected() -> None:
    policy = GovernedMarketDataPolicy()
    first = observation()
    second = observation(value="125.60")

    with pytest.raises(MarketDataValidationError, match="duplicate"):
        construct_governed_market_data_package(
            request(policy, first, second),
            policy,
        )


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("last_price", "not-a-number"),
        ("bid", "-1"),
        ("volume", "-5"),
    ],
)
def test_invalid_numeric_market_data_is_rejected(
    field_name: str,
    value: str,
) -> None:
    policy = GovernedMarketDataPolicy()

    with pytest.raises(MarketDataValidationError):
        construct_governed_market_data_package(
            request(
                policy,
                observation(field_name=field_name, value=value),
            ),
            policy,
        )


def test_received_at_cannot_precede_observed_at() -> None:
    policy = GovernedMarketDataPolicy()

    with pytest.raises(MarketDataValidationError, match="cannot precede"):
        construct_governed_market_data_package(
            request(
                policy,
                observation(
                    observed_at="2026-07-24T20:59:30Z",
                    received_at="2026-07-24T20:59:29Z",
                ),
            ),
            policy,
        )


def test_future_observation_is_rejected() -> None:
    policy = GovernedMarketDataPolicy()

    with pytest.raises(MarketDataValidationError, match="after"):
        construct_governed_market_data_package(
            request(
                policy,
                observation(
                    observed_at="2026-07-24T21:01:00Z",
                    received_at="2026-07-24T21:01:01Z",
                ),
            ),
            policy,
        )


def test_packages_are_immutable() -> None:
    policy = GovernedMarketDataPolicy()
    package = construct_governed_market_data_package(request(policy), policy)

    with pytest.raises(FrozenInstanceError):
        package.quality = MarketDataQuality.REJECTED  # type: ignore[misc]


def test_comparison_reports_added_removed_and_changed() -> None:
    policy = GovernedMarketDataPolicy()
    before = construct_governed_market_data_package(
        request(
            policy,
            observation(),
            observation(
                observation_id="quote-bid",
                field_name="bid",
                value="125.40",
            ),
        ),
        policy,
    )
    after = construct_governed_market_data_package(
        request(
            policy,
            observation(value="126.00"),
            observation(
                observation_id="quote-ask",
                field_name="ask",
                value="126.10",
            ),
        ),
        policy,
    )

    comparison = compare_market_data_packages(before, after)

    assert comparison.added_observation_ids == ("quote-ask",)
    assert comparison.removed_observation_ids == ("quote-bid",)
    assert comparison.changed_observation_ids == ("quote-last-primary",)


def test_comparison_requires_same_instrument() -> None:
    policy = GovernedMarketDataPolicy()
    before = construct_governed_market_data_package(request(policy), policy)
    other_observation = replace(
        observation(),
        instrument_id="security-other-common",
    )
    after_request = GovernedMarketDataInput(
        instrument_id="security-other-common",
        as_of=AS_OF,
        as_of_epoch_seconds=AS_OF_EPOCH,
        observations=(other_observation,),
        policy_identity=policy.policy_identity,
    )
    after = construct_governed_market_data_package(after_request, policy)

    with pytest.raises(MarketDataValidationError, match="same instrument"):
        compare_market_data_packages(before, after)


def test_policy_rejects_invalid_age_bounds() -> None:
    with pytest.raises(MarketDataValidationError):
        GovernedMarketDataPolicy(
            maximum_age_seconds=100,
            expiration_age_seconds=99,
        )
