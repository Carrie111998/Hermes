import time
from argparse import Namespace

from hermes_cli import (
    business,
    finance_db,
    objective_maintenance,
    objective_triggers,
    objectives_db,
    organization_db,
    resource_budget,
    payment_controls,
)


def test_maintenance_schema_read_preserves_active_transaction(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    objective_maintenance.ensure_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    objective_maintenance.ensure_schema(conn)
    assert conn.in_transaction is True
    conn.rollback()


def test_housekeeping_expires_dormant_authority_and_releases_capital(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    now = int(time.time())
    objective = objectives_db.create_objective(
        conn,
        desired_outcome="Time-bound campaign",
        originator="owner",
        organization_id="org_1",
        expires_at=now + 10,
        max_spend_minor=500,
        currency="USD",
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor="owner"
    )
    plan_id = objectives_db.create_plan(
        conn,
        objective.id,
        assumptions=[],
        tasks=[],
        dependencies=[],
        risks=[],
        created_by="ceo",
    )
    objectives_db.transition_objective(conn, objective.id, "planned", actor="ceo")
    action_id = objectives_db.propose_action(
        conn,
        objective_id=objective.id,
        plan_id=plan_id,
        action_type="campaign.launch",
        payload={"system": "ads"},
        expected_outcome="campaign live",
        required_capability="campaign.launch",
        verification_method="provider.readback",
        risk_class="medium",
        reversible=True,
        proposed_by="ceo",
        estimated_cost_minor=500,
    )
    permit_id = objectives_db.issue_permit(
        conn,
        action_id,
        capability="campaign.launch",
        issued_to="employee:ceo",
        policy_version="v1",
        expires_at=now + 10,
    )
    account_id = finance_db.create_treasury_account(
        conn, organization_id="org_1", currency="USD"
    )
    finance_db.seed_initial_capital(
        conn, account_id=account_id, amount_minor=1000,
        currency="USD", actor="owner",
    )
    finance_db.reserve_budget(
        conn,
        account_id=account_id,
        objective_id=objective.id,
        action_id=action_id,
        amount_minor=500,
        currency="USD",
        expires_at=now + 10,
    )
    schedule_id = objective_triggers.create_schedule(
        conn,
        organization_id="org_1",
        objective_id=objective.id,
        event_type="schedule.campaign",
        interval_seconds=60,
        next_fire_at=now + 5,
        payload={},
    )

    first = objective_maintenance.run_housekeeping(conn, now=now + 20)
    second = objective_maintenance.run_housekeeping(conn, now=now + 20)

    assert first["expired_objectives"] == [objective.id]
    assert first["revoked_permits"] == [permit_id]
    assert first["released_reservations"] == [action_id]
    assert second["expired_objectives"] == []
    assert conn.execute(
        "SELECT COUNT(*) FROM objective_maintenance_runs"
    ).fetchone()[0] == 1
    assert objectives_db.get_objective(conn, objective.id).status == "expired"
    assert conn.execute(
        "SELECT status FROM candidate_actions WHERE id=?", (action_id,)
    ).fetchone()["status"] == "expired"
    assert conn.execute(
        "SELECT status FROM budget_reservations WHERE action_id=?", (action_id,)
    ).fetchone()["status"] == "released"
    assert conn.execute(
        "SELECT status FROM objective_schedules WHERE id=?", (schedule_id,)
    ).fetchone()["status"] == "disabled"
    assert objective_triggers.dispatch_due(conn, now=now + 100) == 0


def test_housekeeping_requeues_active_objective_without_a_wakeup(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    objective = objectives_db.create_objective(
        conn,
        desired_outcome="Continue operating after a control-plane restart",
        originator="employee:ceo",
        success_criteria=[
            {"verifier": "accounting.books_balanced", "params": {}}
        ],
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor="human:advisor"
    )
    # Simulate a direct lifecycle transition whose external wake was lost.
    conn.execute("DELETE FROM objective_inbox WHERE objective_id=?", (objective.id,))
    conn.commit()

    first = objective_maintenance.run_housekeeping(conn)
    second = objective_maintenance.run_housekeeping(conn)

    assert first["requeued_objectives"] == [objective.id]
    assert second["requeued_objectives"] == []
    event = conn.execute(
        "SELECT event_type,payload_json FROM objective_inbox WHERE objective_id=?",
        (objective.id,),
    ).fetchone()
    assert event["event_type"] == "objective.accepted.reconcile"
    assert "objective_maintenance" in event["payload_json"]
    conn.close()


def test_stale_compute_cost_escalates_once_and_cli_reconciles_with_evidence(
    tmp_path,
):
    path = tmp_path / "authority.db"
    conn = objectives_db.connect(path)
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Evidence Company",
        purpose="Escalate only genuine billing ambiguity",
        profile_name="default",
        charter={"finance": {"base_currency": "USD"}},
    )
    account_id = finance_db.create_treasury_account(
        conn, organization_id=organization_id, currency="USD"
    )
    finance_db.seed_initial_capital(
        conn,
        account_id=account_id,
        amount_minor=1_000,
        currency="USD",
        actor="human",
    )
    objective = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Reconcile planner billing",
        originator="employee:ceo",
    )
    reservation_id = resource_budget.reserve_planner_call(
        conn,
        objective_id=objective.id,
        limits=resource_budget.DEFAULT_LIMITS,
        input_tokens=100,
        output_tokens=100,
        estimated_compute_cost_minor=10,
        enforce_treasury=True,
    )
    created_at = conn.execute(
        """SELECT created_at FROM planner_compute_reservations
            WHERE id=?""",
        (reservation_id,),
    ).fetchone()["created_at"]
    first = objective_maintenance.run_housekeeping(
        conn,
        now=int(created_at) + 11,
        compute_reconciliation_grace_seconds=10,
    )
    second = objective_maintenance.run_housekeeping(
        conn,
        now=int(created_at) + 12,
        compute_reconciliation_grace_seconds=10,
    )
    assert len(first["compute_reconciliation_interventions"]) == 1
    assert second["compute_reconciliation_interventions"] == []
    intervention_id = first["compute_reconciliation_interventions"][0]
    conn.close()

    assert (
        business.business_command(
            Namespace(
                business_command="compute-reconcile",
                db=path,
                reservation_id=reservation_id,
                status="included",
                actual_minor=0,
                model="subscription-model",
                billing_provider="subscription-provider",
                provider_reference=None,
                actor="human:advisor",
                evidence='{"subscription_invoice":"INV-1"}',
            )
        )
        == 0
    )
    restarted = objectives_db.connect(path)
    intervention = restarted.execute(
        "SELECT * FROM intervention_queue WHERE id=?",
        (intervention_id,),
    ).fetchone()
    assert intervention["status"] == "resolved"
    assert resource_budget.compute_reservation_posture(
        restarted, organization_id
    )["unreconciled_count"] == 0
    assert restarted.execute(
        """SELECT COUNT(*) FROM objective_inbox
            WHERE objective_id=? AND event_type='intervention.resolved'""",
        (objective.id,),
    ).fetchone()[0] == 1


def test_stale_outbound_spend_hold_escalates_without_automatic_release(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    instrument = payment_controls.register_tokenized_instrument(
        conn,
        organization_id="org_spend",
        provider="fake",
        provider_instrument_id="token-stale-hold",
        rail_type="virtual_card",
        currency="USD",
        label="Stale hold card",
    )
    payment_controls.set_spend_controls(
        conn,
        instrument_id=instrument,
        max_transaction_minor=1_000,
        max_daily_minor=1_000,
        allowed_merchant_categories=[],
        allowed_payees=[],
        policy_version="finance-v1",
    )
    payment_controls.authorize_spend(
        conn,
        instrument_id=instrument,
        provider="fake",
        amount_minor=600,
        currency="USD",
        merchant_category="software",
        payee_id="vendor-stale",
        action_id="action-stale-hold",
    )
    now = int(time.time())
    conn.execute(
        "UPDATE payment_spend_holds SET created_at=? WHERE action_id=?",
        (now - 20, "action-stale-hold"),
    )
    conn.commit()

    first = objective_maintenance.run_housekeeping(
        conn, now=now, spend_hold_reconciliation_grace_seconds=10
    )
    second = objective_maintenance.run_housekeeping(
        conn, now=now, spend_hold_reconciliation_grace_seconds=10
    )

    assert len(first["spend_hold_reconciliation_interventions"]) == 1
    assert second["spend_hold_reconciliation_interventions"] == []
    assert conn.execute(
        "SELECT status FROM payment_spend_holds WHERE action_id=?",
        ("action-stale-hold",),
    ).fetchone()["status"] == "reserved"
    intervention = conn.execute(
        "SELECT id,category FROM intervention_queue WHERE action_id=?",
        ("action-stale-hold",),
    ).fetchone()
    assert intervention["category"] == "outbound_spend_hold_unreconciled"
    from hermes_cli import operational_control

    operational_control.resolve_intervention(
        conn,
        intervention["id"],
        option_id="release",
        actor="human:advisor",
        evidence={
            "provider_status": "failed",
            "settlement_reference": "provider-failure-1",
        },
    )
    assert conn.execute(
        "SELECT status FROM payment_spend_holds WHERE action_id=?",
        ("action-stale-hold",),
    ).fetchone()["status"] == "released"
