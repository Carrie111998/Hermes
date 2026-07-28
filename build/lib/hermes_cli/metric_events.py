"""Normalize authenticated provider measurements into immutable KPI evidence."""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional

from hermes_cli import business_metrics


class MetricEventError(ValueError):
    pass


def ingest_authenticated_observation(
    conn,
    *,
    organization_id: str,
    expected_metric_id: str,
    expected_verifier: str,
    payload: Mapping[str, Any],
    delivery_id: str,
    route_name: str,
    authentication_evidence: Mapping[str, Any],
    max_event_age_seconds: int = 86_400,
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Record one route-bound observation without exposing it as instructions."""
    current = int(time.time()) if now is None else int(now)
    if not organization_id or not expected_metric_id or not expected_verifier:
        raise MetricEventError("metric route binding is incomplete")
    if not delivery_id or not route_name or not authentication_evidence:
        raise MetricEventError("authenticated delivery identity is required")
    if max_event_age_seconds <= 0:
        raise MetricEventError("metric event age limit must be positive")
    try:
        value_scaled = payload["value_scaled"]
        observed_at = payload["observed_at"]
        source_reference = payload["source_reference"]
    except KeyError as exc:
        raise MetricEventError(
            f"metric observation missing field: {exc.args[0]}"
        ) from exc
    if isinstance(value_scaled, bool) or not isinstance(value_scaled, int):
        raise MetricEventError("metric value_scaled must be an integer")
    if isinstance(observed_at, bool) or not isinstance(observed_at, int):
        raise MetricEventError("metric observed_at must be an integer")
    if not isinstance(source_reference, str) or not source_reference.strip():
        raise MetricEventError("metric source_reference must be non-empty")
    if observed_at > current + 300:
        raise MetricEventError("metric observation is too far in the future")
    if observed_at < current - max_event_age_seconds:
        raise MetricEventError("metric observation exceeds route age limit")
    claimed_metric = payload.get("metric_id")
    if claimed_metric is not None and str(claimed_metric) != expected_metric_id:
        raise MetricEventError("payload metric_id differs from route binding")
    claimed_organization = payload.get("organization_id")
    if (
        claimed_organization is not None
        and str(claimed_organization) != organization_id
    ):
        raise MetricEventError("payload organization_id differs from route binding")
    provider_evidence = payload.get("evidence") or {}
    if not isinstance(provider_evidence, Mapping):
        raise MetricEventError("metric evidence must be an object")
    try:
        observation_id, created = business_metrics.record_observation(
            conn,
            organization_id=organization_id,
            metric_id=expected_metric_id,
            value_scaled=value_scaled,
            observed_at=observed_at,
            source_reference=source_reference,
            verifier=expected_verifier,
            evidence={
                "provider": dict(provider_evidence),
                "authenticated_route": route_name,
            },
        )
    except (KeyError, ValueError, PermissionError) as exc:
        raise MetricEventError(str(exc)) from exc
    try:
        receipt_id, receipt_created = business_metrics.record_ingestion_receipt(
            conn,
            organization_id=organization_id,
            observation_id=observation_id,
            route_name=route_name,
            delivery_id=delivery_id,
            authentication_evidence=dict(authentication_evidence),
            received_at=current,
        )
    except (KeyError, ValueError, PermissionError) as exc:
        raise MetricEventError(str(exc)) from exc
    reviews = business_metrics.dispatch_reviews(
        conn,
        organization_id=organization_id,
        now=current,
    )
    return {
        "observation_id": observation_id,
        "created": created,
        "ingestion_receipt_id": receipt_id,
        "ingestion_receipt_created": receipt_created,
        "reviews": reviews,
    }
