import sqlite3

import pytest

from hermes_cli import finance_db, objectives_db, organization_db
from hermes_cli.procurement_policy import (
    commit_software_purchase,
    evaluate_procurement,
    evaluate_procurement_from_state,
)


def test_foss_precedes_build_and_paid_product():
    decision = evaluate_procurement(
        case={
            "foss_fit": 0.9,
            "foss_integration_cost_minor": 100,
            "build_cost_minor": 200,
            "paid_cost_minor": 50,
            "paid_required": True,
            "paid_expected_roi": 10,
            "persistent_need": True,
        },
        available_budget_minor=1000,
    )
    assert decision.choice == "foss"


def test_build_is_preferred_when_foss_does_not_fit():
    decision = evaluate_procurement(
        case={
            "foss_fit": 0.4,
            "build_cost_minor": 300,
            "build_feasible": True,
            "paid_cost_minor": 100,
            "paid_required": True,
            "paid_expected_roi": 10,
            "persistent_need": True,
        },
        available_budget_minor=1000,
    )
    assert decision.choice == "build"


def test_paid_product_requires_budget_roi_persistence_and_failed_build_path():
    decision = evaluate_procurement(
        case={
            "foss_fit": 0,
            "build_feasible": False,
            "paid_cost_minor": 500,
            "paid_required": True,
            "paid_expected_roi": 2.0,
            "persistent_need": True,
        },
        available_budget_minor=500,
    )
    assert decision.choice == "buy"


def test_procurement_defers_instead_of_exceeding_budget():
    decision = evaluate_procurement(
        case={
            "foss_fit": 0.9,
            "foss_integration_cost_minor": 200,
            "build_cost_minor": 300,
            "paid_cost_minor": 100,
            "paid_required": True,
            "paid_expected_roi": 2,
            "persistent_need": True,
        },
        available_budget_minor=99,
    )
    assert decision.choice == "defer"


def _state(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Procurement Company",
        purpose="Buy only when warranted",
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
        desired_outcome="Obtain required software",
        originator="employee:ceo",
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor="employee:ceo"
    )
    return conn, organization_id, objective


def test_state_derived_buy_decision_binds_one_exact_payment(tmp_path):
    conn, organization_id, objective = _state(tmp_path)
    decision_id, decision = evaluate_procurement_from_state(
        conn,
        organization_id=organization_id,
        objective_id=objective.id,
        case={
            "foss_fit": 0.2,
            "build_feasible": False,
            "paid_cost_minor": 500,
            "paid_required": True,
            "paid_expected_roi": 2,
            "persistent_need": True,
        },
        source_evidence={"reference": "vendor-quote:v1"},
        idempotency_key="procurement-decision-buy-0001",
        evaluated_by="employee:ceo",
    )
    assert decision.choice == "buy"
    assert decision.committed_cost_minor == 500

    commit_software_purchase(
        conn,
        decision_id=decision_id,
        organization_id=organization_id,
        objective_id=objective.id,
        action_id="action-payment-1",
        amount_minor=500,
        currency="USD",
    )
    commit_software_purchase(
        conn,
        decision_id=decision_id,
        organization_id=organization_id,
        objective_id=objective.id,
        action_id="action-payment-1",
        amount_minor=500,
        currency="USD",
    )
    with pytest.raises(PermissionError, match="already committed"):
        commit_software_purchase(
            conn,
            decision_id=decision_id,
            organization_id=organization_id,
            objective_id=objective.id,
            action_id="action-payment-2",
            amount_minor=500,
            currency="USD",
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE procurement_decisions SET choice='build' WHERE id=?",
            (decision_id,),
        )


def test_non_buy_decision_cannot_authorize_software_payment(tmp_path):
    conn, organization_id, objective = _state(tmp_path)
    decision_id, decision = evaluate_procurement_from_state(
        conn,
        organization_id=organization_id,
        objective_id=objective.id,
        case={
            "foss_fit": 0.9,
            "foss_integration_cost_minor": 100,
            "build_cost_minor": 200,
            "paid_cost_minor": 50,
            "paid_required": True,
            "paid_expected_roi": 5,
            "persistent_need": True,
        },
        source_evidence={"reference": "options-analysis:v1"},
        idempotency_key="procurement-decision-foss-0001",
        evaluated_by="employee:ceo",
    )
    assert decision.choice == "foss"
    with pytest.raises(PermissionError, match="not a paid purchase"):
        commit_software_purchase(
            conn,
            decision_id=decision_id,
            organization_id=organization_id,
            objective_id=objective.id,
            action_id="action-payment-1",
            amount_minor=100,
            currency="USD",
        )
