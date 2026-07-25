"""Deterministic construction of governed market-data packages."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .input import GovernedMarketDataInput
from .models import (
    GovernedMarketDataPackage,
    MarketDataFreshness,
    MarketDataObservation,
    MarketDataProvenance,
    MarketDataQuality,
    MarketDataValidationError,
)
from .policy import GovernedMarketDataPolicy


def _epoch_seconds(value: str, name: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketDataValidationError(f"{name} must be ISO-8601") from exc

    if parsed.tzinfo is None:
        raise MarketDataValidationError(f"{name} must include a timezone")

    return int(parsed.astimezone(timezone.utc).timestamp())


def _validate_numeric(observation: MarketDataObservation) -> None:
    numeric_fields = {
        "bid",
        "ask",
        "last_price",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "market_cap",
        "shares_outstanding",
    }
    if observation.field_name not in numeric_fields:
        return

    try:
        value = Decimal(observation.value)
    except InvalidOperation as exc:
        raise MarketDataValidationError(
            f"{observation.field_name} must be numeric"
        ) from exc

    if value < 0:
        raise MarketDataValidationError(
            f"{observation.field_name} must be nonnegative"
        )


def _validate_request(
    request: GovernedMarketDataInput,
    policy: GovernedMarketDataPolicy,
) -> None:
    if request.policy_identity != policy.policy_identity:
        raise MarketDataValidationError("market-data request policy mismatch")

    seen: set[str] = set()
    for observation in request.observations:
        if observation.instrument_id != request.instrument_id:
            raise MarketDataValidationError(
                "observation instrument does not match request"
            )
        if not policy.permits_kind(observation.kind):
            raise MarketDataValidationError(
                f"market-data kind is not permitted: {observation.kind.value}"
            )
        if not policy.permits_source(observation.source_id):
            raise MarketDataValidationError(
                f"market-data source is not permitted: {observation.source_id}"
            )
        if (
            policy.require_evidence_references
            and not observation.evidence_references
        ):
            raise MarketDataValidationError(
                "observation evidence references are required"
            )
        if policy.reject_duplicate_observation_ids:
            if observation.observation_id in seen:
                raise MarketDataValidationError(
                    "duplicate observation_id is not permitted"
                )
            seen.add(observation.observation_id)

        observed_epoch = _epoch_seconds(
            observation.observed_at, "observation.observed_at"
        )
        received_epoch = _epoch_seconds(
            observation.received_at, "observation.received_at"
        )
        if received_epoch < observed_epoch:
            raise MarketDataValidationError(
                "received_at cannot precede observed_at"
            )
        if observed_epoch > request.as_of_epoch_seconds:
            raise MarketDataValidationError(
                "observed_at cannot be after the package as_of time"
            )
        _validate_numeric(observation)


def _freshness(
    request: GovernedMarketDataInput,
    policy: GovernedMarketDataPolicy,
) -> tuple[MarketDataFreshness, int]:
    newest = max(_epoch_seconds(item.observed_at, "observed_at") for item in request.observations)
    age = request.as_of_epoch_seconds - newest

    if age <= policy.maximum_age_seconds:
        return MarketDataFreshness.FRESH, age
    if age <= policy.expiration_age_seconds:
        return MarketDataFreshness.STALE, age
    return MarketDataFreshness.EXPIRED, age


def construct_governed_market_data_package(
    request: GovernedMarketDataInput,
    policy: GovernedMarketDataPolicy,
) -> GovernedMarketDataPackage:
    """Normalize explicit observations without browsing or executing trades."""

    _validate_request(request, policy)

    observations = tuple(
        sorted(
            request.observations,
            key=lambda item: (
                item.field_name,
                item.observed_at,
                item.source_id,
                item.observation_id,
            ),
        )
    )

    fields = {item.field_name for item in observations}
    missing_fields = tuple(
        sorted(field for field in policy.required_fields if field not in fields)
    )
    freshness, age_seconds = _freshness(request, policy)

    quality_reasons: list[str] = []
    blockers: list[str] = []

    if missing_fields:
        quality_reasons.append(
            "missing required fields: " + ", ".join(missing_fields)
        )
        blockers.extend(f"missing-required-field:{field}" for field in missing_fields)

    if freshness is MarketDataFreshness.STALE:
        quality_reasons.append(
            f"newest observation is stale at {age_seconds} seconds old"
        )
        blockers.append("stale-market-data")
    elif freshness is MarketDataFreshness.EXPIRED:
        quality_reasons.append(
            f"newest observation is expired at {age_seconds} seconds old"
        )
        blockers.append("expired-market-data")

    distinct_sources = tuple(sorted({item.source_id for item in observations}))
    if len(distinct_sources) == 1:
        quality_reasons.append("single-source observation set")

    if freshness is MarketDataFreshness.EXPIRED or missing_fields:
        quality = MarketDataQuality.REJECTED
    elif freshness is MarketDataFreshness.STALE:
        quality = MarketDataQuality.DEGRADED
    elif len(distinct_sources) >= 2:
        quality = MarketDataQuality.VERIFIED
    else:
        quality = MarketDataQuality.ACCEPTABLE

    provenance = MarketDataProvenance(
        request_identity=request.request_identity,
        policy_identity=policy.policy_identity,
        source_ids=distinct_sources,
        input_observation_identities=tuple(
            item.observation_identity for item in observations
        ),
        upstream_package_identities=request.upstream_package_identities,
    )

    return GovernedMarketDataPackage(
        instrument_id=request.instrument_id,
        as_of=request.as_of,
        observations=observations,
        quality=quality,
        freshness=freshness,
        quality_reasons=tuple(quality_reasons),
        readiness_blockers=tuple(blockers),
        provenance=provenance,
    )
