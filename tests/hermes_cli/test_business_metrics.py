from __future__ import annotations

import json
import sqlite3
import time

import pytest

from hermes_cli import business_metrics
from hermes_cli import finance_db
from hermes_cli import objective_adapters
from hermes_cli import objectives_db
from hermes_cli import organization_db


def test_metrics_schema_read_preserves_active_transaction(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    business_metrics.ensure_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    business_metrics.ensure_schema(conn)
    assert conn.in_transaction is True
    conn.rollback()


def _company(tmp_path, *, root: bool = True):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, ceo_id = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Measured Company",
        purpose="Learn from verified operating outcomes",
        profile_name="measured",
        charter={},
    )
    objective_id = None
    if root:
        objective = objectives_db.create_objective(
            conn,
            organization_id=organization_id,
            desired_outcome="Find a repeatable acquisition channel",
            originator="employee:ceo",
            permitted_systems=["strategy"],
            prohibited_actions=["metrics.fabricate"],
            max_spend_minor=10_000,
            currency="USD",
            expires_at=int(time.time()) + 86_400,
        )
        objectives_db.transition_objective(
            conn, objective.id, "accepted", actor="employee:ceo"
        )
        objective_id = objective.id
    return conn, organization_id, ceo_id, objective_id


def _metric(conn, organization_id: str):
    return business_metrics.register_metric(
        conn,
        organization_id=organization_id,
        metric_key="activation_rate",
        name="Activation rate",
        unit="ratio",
        preferred_direction="increase",
        source_system="product_analytics",
        verifier="analytics:signed-readback",
        idempotency_key="metric-activation-rate-0001",
        created_by="employee:ceo",
    )[0]


def test_metric_observations_are_evidence_bound_immutable_and_idempotent(tmp_path):
    conn, organization_id, _, _ = _company(tmp_path)
    metric_id = _metric(conn, organization_id)
    kwargs = {
        "organization_id": organization_id,
        "metric_id": metric_id,
        "value_scaled": 250_000,
        "observed_at": int(time.time()),
        "source_reference": "analytics-export:activation:revision-7",
        "verifier": "analytics:signed-readback",
        "evidence": {"signature": "valid", "sample_size": 100},
    }

    first_id, first_created = business_metrics.record_observation(conn, **kwargs)
    second_id, second_created = business_metrics.record_observation(conn, **kwargs)

    assert first_created is True
    assert second_created is False
    assert first_id == second_id
    with pytest.raises(PermissionError, match="different evidence"):
        business_metrics.record_observation(
            conn, **{**kwargs, "value_scaled": 900_000}
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE metric_observations SET value_scaled=900000 WHERE id=?",
            (first_id,),
        )


def test_due_target_records_off_track_evidence_and_wakes_root_once(tmp_path):
    conn, organization_id, _, objective_id = _company(tmp_path)
    metric_id = _metric(conn, organization_id)
    now = int(time.time())
    observation_id, _ = business_metrics.record_observation(
        conn,
        organization_id=organization_id,
        metric_id=metric_id,
        value_scaled=200_000,
        observed_at=now,
        source_reference="analytics-export:activation:revision-8",
        verifier="analytics:signed-readback",
        evidence={"signature": "valid", "sample_size": 200},
    )
    target_id, _ = business_metrics.define_target(
        conn,
        organization_id=organization_id,
        objective_id=objective_id,
        metric_id=metric_id,
        comparator="gte",
        target_scaled=400_000,
        review_interval_seconds=3_600,
        max_observation_age_seconds=86_400,
        first_review_at=now + 10,
        idempotency_key="target-activation-rate-0001",
        created_by="employee:ceo",
    )
    with pytest.raises(PermissionError, match="different parameters"):
        business_metrics.define_target(
            conn,
            organization_id=organization_id,
            objective_id=objective_id,
            metric_id=metric_id,
            comparator="gte",
            target_scaled=900_000,
            review_interval_seconds=3_600,
            max_observation_age_seconds=86_400,
            first_review_at=now + 10,
            idempotency_key="target-activation-rate-0001",
            created_by="employee:ceo",
        )

    first = business_metrics.dispatch_reviews(
        conn, organization_id=organization_id, now=now + 7_210
    )
    second = business_metrics.dispatch_reviews(
        conn, organization_id=organization_id, now=now + 7_210
    )

    assert first == {
        "evaluations_recorded": 1,
        "events_enqueued": 1,
        "interventions_raised": 0,
    }
    assert second == {
        "evaluations_recorded": 0,
        "events_enqueued": 0,
        "interventions_raised": 0,
    }
    evaluation = conn.execute(
        "SELECT * FROM metric_target_evaluations WHERE target_id=?", (target_id,)
    ).fetchone()
    assert evaluation["observation_id"] == observation_id
    assert evaluation["verdict"] == "off_track"
    event = conn.execute(
        """SELECT objective_id,payload_json FROM objective_inbox
            WHERE event_type='strategy.metric_target.reviewed'"""
    ).fetchone()
    payload = json.loads(event["payload_json"])
    assert event["objective_id"] == objective_id
    assert payload["verdict"] == "off_track"
    assert payload["missed_intervals"] == 2
    assert "signature" not in event["payload_json"]


def test_stale_target_observation_is_not_treated_as_current_evidence(tmp_path):
    conn, organization_id, _, objective_id = _company(tmp_path)
    metric_id = _metric(conn, organization_id)
    now = int(time.time())
    business_metrics.record_observation(
        conn,
        organization_id=organization_id,
        metric_id=metric_id,
        value_scaled=900_000,
        observed_at=now,
        source_reference="analytics-export:stale-success:revision-1",
        verifier="analytics:signed-readback",
        evidence={"signature": "valid"},
    )
    target_id, _ = business_metrics.define_target(
        conn,
        organization_id=organization_id,
        objective_id=objective_id,
        metric_id=metric_id,
        comparator="gte",
        target_scaled=400_000,
        review_interval_seconds=3_600,
        max_observation_age_seconds=60,
        first_review_at=now + 120,
        idempotency_key="target-stale-evidence-0001",
        created_by="employee:ceo",
    )

    business_metrics.dispatch_reviews(
        conn, organization_id=organization_id, now=now + 120
    )

    evaluation = conn.execute(
        """SELECT verdict,observation_id FROM metric_target_evaluations
            WHERE target_id=?""",
        (target_id,),
    ).fetchone()
    assert tuple(evaluation) == ("no_evidence", None)


def test_ended_experiment_requires_evidence_based_continue_revise_or_stop(
    tmp_path,
):
    conn, organization_id, _, objective_id = _company(tmp_path)
    metric_id = _metric(conn, organization_id)
    now = int(time.time())
    experiment_id, _ = business_metrics.start_experiment(
        conn,
        organization_id=organization_id,
        objective_id=objective_id,
        name="Onboarding email sequence",
        hypothesis="A shorter sequence raises activation to at least 35%",
        metric_id=metric_id,
        comparator="gte",
        success_threshold_scaled=350_000,
        starts_at=now - 3_600,
        ends_at=now + 10,
        max_spend_minor=500,
        currency="USD",
        idempotency_key="experiment-onboarding-email-0001",
        created_by="employee:ceo",
    )
    business_metrics.record_observation(
        conn,
        organization_id=organization_id,
        metric_id=metric_id,
        value_scaled=300_000,
        observed_at=now,
        source_reference="analytics-export:experiment:revision-1",
        verifier="analytics:signed-readback",
        evidence={"signature": "valid", "cohort": "experiment-1"},
    )

    result = business_metrics.dispatch_reviews(
        conn, organization_id=organization_id, now=now + 10
    )

    assert result["evaluations_recorded"] == 1
    evaluation = conn.execute(
        """SELECT verdict FROM strategy_experiment_evaluations
            WHERE experiment_id=?""",
        (experiment_id,),
    ).fetchone()
    assert evaluation["verdict"] == "not_supported"
    state = conn.execute(
        """SELECT status FROM strategy_experiment_state
            WHERE experiment_id=?""",
        (experiment_id,),
    ).fetchone()
    assert state["status"] == "awaiting_decision"
    assert business_metrics.decide_experiment(
        conn,
        organization_id=organization_id,
        experiment_id=experiment_id,
        decision="stop",
        reason="Verified activation stayed below threshold",
    ) == "stopped"


def test_unowned_strategy_review_escalates_and_tenants_are_isolated(tmp_path):
    conn, organization_id, _, objective_id = _company(tmp_path, root=True)
    foreign_id = organization_db.create_organization(
        conn, name="Foreign Metrics", purpose="Remain isolated"
    )
    metric_id = _metric(conn, organization_id)
    now = int(time.time())
    business_metrics.define_target(
        conn,
        organization_id=organization_id,
        objective_id=objective_id,
        metric_id=metric_id,
        comparator="gte",
        target_scaled=500_000,
        review_interval_seconds=3_600,
        max_observation_age_seconds=86_400,
        first_review_at=now + 1,
        idempotency_key="target-tenant-isolation-0001",
        created_by="employee:ceo",
    )

    foreign = business_metrics.dispatch_reviews(
        conn, organization_id=foreign_id, now=now + 2
    )
    assert foreign["evaluations_recorded"] == 0

    objectives_db.transition_objective(
        conn, objective_id, "cancelled", actor="employee:ceo"
    )
    unowned = business_metrics.dispatch_reviews(
        conn, organization_id=organization_id, now=now + 2
    )
    repeated = business_metrics.dispatch_reviews(
        conn, organization_id=organization_id, now=now + 2
    )
    assert unowned["interventions_raised"] == 1
    assert repeated["interventions_raised"] == 0
    intervention = conn.execute(
        """SELECT organization_id,category FROM intervention_queue
            WHERE category='strategy_review_unowned'"""
    ).fetchone()
    assert intervention["organization_id"] == organization_id


def test_strategy_adapters_publish_only_governed_action_contracts(tmp_path):
    conn, organization_id, ceo_id, objective_id = _company(tmp_path)
    executor = objective_adapters.ActionExecutorRegistry(
        identity=f"employee:{ceo_id}", authority_conn=conn
    )
    verifier = objective_adapters.IndependentVerifierRegistry()
    objective_adapters.register_strategy_adapters(
        executor, verifier, authority_conn=conn
    )
    assert {
        "strategy.register_metric",
        "strategy.define_target",
        "strategy.start_experiment",
        "strategy.decide_experiment",
    }.issubset(executor.action_types)

    outcome = executor.execute_governed(
        "action-register-metric",
        objective_id,
        "strategy.register_metric",
        {
            "system": "strategy",
            "target_resource": f"organization:{organization_id}:metrics",
            "idempotency_key": "adapter-register-metric-0001",
            "metric_key": "qualified_leads",
            "name": "Qualified leads",
            "unit": "count",
            "preferred_direction": "increase",
            "source_system": "crm",
            "observation_verifier": "crm:signed-readback",
        },
    )
    assert outcome.status == "succeeded"


def test_daily_financial_metrics_are_deterministic_and_idempotent(tmp_path):
    conn, organization_id, _, _ = _company(tmp_path)
    account_id = finance_db.create_treasury_account(
        conn, organization_id=organization_id, currency="USD"
    )
    finance_db.seed_initial_capital(
        conn,
        account_id=account_id,
        amount_minor=1_000,
        currency="USD",
        actor="human:operator",
    )
    now = (int(time.time()) // 86_400) * 86_400 + 100

    first = business_metrics.sync_financial_observations(
        conn, organization_id=organization_id, now=now
    )
    repeated = business_metrics.sync_financial_observations(
        conn, organization_id=organization_id, now=now + 60
    )
    next_day = business_metrics.sync_financial_observations(
        conn, organization_id=organization_id, now=now + 86_400
    )

    assert first == {"metrics_ensured": 4, "observations_recorded": 4}
    assert repeated == {"metrics_ensured": 4, "observations_recorded": 0}
    assert next_day == {"metrics_ensured": 4, "observations_recorded": 4}
    snapshot = business_metrics.planning_snapshot(conn, organization_id)
    cash = next(
        item
        for item in snapshot["metrics"]
        if item["metric_key"] == "finance.cash_available_minor"
    )
    assert cash["latest_observation"]["value_scaled"] == (
        1_000 * business_metrics.SCALE
    )
    assert "evidence_json" not in json.dumps(snapshot)
