from __future__ import annotations

import time

import pytest

from hermes_cli import business_metrics
from hermes_cli import metric_events
from hermes_cli import objectives_db
from hermes_cli import organization_db


def _state(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Metric Event Company",
        purpose="Ingest authenticated measurements",
        profile_name="metric-event",
        charter={},
    )
    objective = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Raise activation",
        originator="employee:ceo",
        permitted_systems=["strategy"],
        expires_at=int(time.time()) + 86_400,
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor="employee:ceo"
    )
    metric_id, _ = business_metrics.register_metric(
        conn,
        organization_id=organization_id,
        metric_key="activation",
        name="Activation",
        unit="ratio",
        preferred_direction="increase",
        source_system="analytics",
        verifier="analytics:hmac-route",
        idempotency_key="metric-event-activation-0001",
        created_by="employee:ceo",
    )
    return conn, organization_id, objective.id, metric_id


def test_authenticated_metric_event_is_route_bound_and_durably_replay_safe(
    tmp_path,
):
    conn, organization_id, _, metric_id = _state(tmp_path)
    now = int(time.time())
    kwargs = {
        "organization_id": organization_id,
        "expected_metric_id": metric_id,
        "expected_verifier": "analytics:hmac-route",
        "payload": {
            "metric_id": metric_id,
            "organization_id": organization_id,
            "value_scaled": 420_000,
            "observed_at": now,
            "source_reference": "analytics:activation:revision-9",
            "evidence": {"sample_size": 500},
        },
        "route_name": "analytics-activation",
        "authentication_evidence": {
            "method": "webhook_hmac",
            "signature_version": "v2",
        },
        "max_event_age_seconds": 3_600,
        "now": now,
    }
    first = metric_events.ingest_authenticated_observation(
        conn, delivery_id="delivery-1", **kwargs
    )
    retry = metric_events.ingest_authenticated_observation(
        conn, delivery_id="delivery-2", **kwargs
    )

    assert first["created"] is True
    assert retry["created"] is False
    assert first["observation_id"] == retry["observation_id"]
    assert first["ingestion_receipt_id"] != retry["ingestion_receipt_id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM metric_observations"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM metric_ingestion_receipts"
    ).fetchone()[0] == 2


def test_metric_event_rejects_tenant_metric_and_temporal_spoofing(tmp_path):
    conn, organization_id, _, metric_id = _state(tmp_path)
    now = int(time.time())
    base = {
        "organization_id": organization_id,
        "expected_metric_id": metric_id,
        "expected_verifier": "analytics:hmac-route",
        "delivery_id": "delivery-1",
        "route_name": "analytics-activation",
        "authentication_evidence": {"method": "webhook_hmac"},
        "max_event_age_seconds": 60,
        "now": now,
    }
    payload = {
        "metric_id": "metric_foreign",
        "value_scaled": 1,
        "observed_at": now,
        "source_reference": "provider:1",
    }
    with pytest.raises(metric_events.MetricEventError, match="metric_id differs"):
        metric_events.ingest_authenticated_observation(
            conn, payload=payload, **base
        )
    with pytest.raises(metric_events.MetricEventError, match="age limit"):
        metric_events.ingest_authenticated_observation(
            conn,
            payload={
                **payload,
                "metric_id": metric_id,
                "observed_at": now - 61,
            },
            **base,
        )
    with pytest.raises(metric_events.MetricEventError, match="future"):
        metric_events.ingest_authenticated_observation(
            conn,
            payload={
                **payload,
                "metric_id": metric_id,
                "observed_at": now + 301,
            },
            **base,
        )


def test_metric_event_immediately_dispatches_due_target_review(tmp_path):
    conn, organization_id, objective_id, metric_id = _state(tmp_path)
    now = int(time.time())
    business_metrics.define_target(
        conn,
        organization_id=organization_id,
        objective_id=objective_id,
        metric_id=metric_id,
        comparator="gte",
        target_scaled=500_000,
        review_interval_seconds=3_600,
        max_observation_age_seconds=60,
        first_review_at=now + 1,
        idempotency_key="metric-event-due-target-0001",
        created_by="employee:ceo",
    )

    result = metric_events.ingest_authenticated_observation(
        conn,
        organization_id=organization_id,
        expected_metric_id=metric_id,
        expected_verifier="analytics:hmac-route",
        payload={
            "value_scaled": 300_000,
            "observed_at": now + 1,
            "source_reference": "analytics:activation:revision-10",
        },
        delivery_id="delivery-review",
        route_name="analytics-activation",
        authentication_evidence={"method": "webhook_hmac"},
        max_event_age_seconds=60,
        now=now + 1,
    )

    assert result["reviews"]["evaluations_recorded"] == 1
    event = conn.execute(
        """SELECT event_type FROM objective_inbox
            WHERE event_type='strategy.metric_target.reviewed'"""
    ).fetchone()
    assert event["event_type"] == "strategy.metric_target.reviewed"
