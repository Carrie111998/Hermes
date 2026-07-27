from hermes_cli import (
    finance_db,
    objectives_db,
    operational_control,
    organization_db,
)
from hermes_cli.business import build_business_snapshot


def test_business_snapshot_projects_authoritative_state(tmp_path):
    conn = objectives_db.connect(tmp_path / "business.db")
    charter = {
        "operator_role": "advisor",
        "allowed_capabilities": [],
        "allowed_systems": [],
        "forbidden_capabilities": [],
        "max_action_spend_minor": 1000,
        "finance": {"base_currency": "USD"},
    }
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Test Company",
        purpose="Test objective operations",
        profile_name="default",
        charter=charter,
    )
    account = finance_db.create_treasury_account(
        conn, organization_id=organization_id, currency="USD"
    )
    finance_db.seed_initial_capital(
        conn,
        account_id=account,
        amount_minor=10_000,
        currency="USD",
        actor="human",
    )
    objective = objectives_db.create_objective(
        conn,
        desired_outcome="Earn first revenue",
        originator="setup",
        success_criteria=[{"verifier": "revenue.readback"}],
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor="setup"
    )
    objectives_db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="strategy.metric_target.reviewed",
        payload={"verdict": "off_track"},
    )
    operational_control.raise_intervention(
        conn,
        objective_id=objective.id,
        category="ambiguous_customer_request",
        summary="Customer intent has two defensible interpretations",
        context={"message_id": "msg_1"},
        options=[
            {"id": "clarify", "label": "Ask for clarification"},
            {"id": "decline", "label": "Decline request"},
        ],
    )

    snapshot = build_business_snapshot(conn)

    assert snapshot["configured"] is True
    assert snapshot["organization"]["name"] == "Test Company"
    assert snapshot["treasury"]["available_minor"] == 10_000
    assert snapshot["treasury"]["compute_committed_minor"] == 0
    assert snapshot["accounting"]["balance_sheet"]["balanced"] is True
    assert snapshot["objectives"][0]["desired_outcome"] == "Earn first revenue"
    assert snapshot["autonomy"]["mode"] == "autonomous"
    assert snapshot["event_queue"]["pending"] == 1
    assert snapshot["event_queue"]["high_priority"] == 1
    assert snapshot["event_queue"]["admission_policy"] == (
        "deterministic_priority_with_aging"
    )
    assert snapshot["interventions"][0]["options"][0]["id"] == "clarify"
    assert snapshot["authority_store"]["backend"] == "sqlite"
    assert snapshot["authority_store"]["deployment_scope"] == "single_host"
    assert snapshot["execution_recovery"] == {
        "in_doubt": [],
        "sensitive_data_included": False,
    }
    assert snapshot["compute_reservations"] == {
        "unreconciled_count": 0,
        "unreconciled_minor": 0,
        "oldest_age_seconds": None,
        "sensitive_data_included": False,
    }
