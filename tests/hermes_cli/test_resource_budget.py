from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from hermes_cli import (
    finance_db,
    objectives_db,
    organization_db,
    resource_budget,
)


def test_schema_reads_preserve_active_authority_transaction(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    resource_budget.ensure_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO objective_resource_usage "
        "(objective_id, cycles, actions, input_tokens, output_tokens, "
        "estimated_compute_cost_minor, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("obj_txn", 1, 0, 0, 0, 0, 1),
    )
    assert resource_budget.usage(conn, "obj_txn")["cycles"] == 1
    assert conn.in_transaction is True
    conn.rollback()


def test_objective_compute_ceiling_is_durable_and_fail_closed(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    limits = {
        "max_cycles_per_objective": 1,
        "max_actions_per_objective": 2,
        "max_input_tokens_per_objective": 100,
        "max_output_tokens_per_objective": 100,
        "max_compute_cost_minor_per_objective": 10,
    }
    resource_budget.assert_admissible(conn, "obj_1", limits)
    resource_budget.record_cycle(
        conn, objective_id="obj_1", actions=1, input_tokens=25, output_tokens=10
    )
    with pytest.raises(resource_budget.ResourceBudgetError, match="max_cycles"):
        resource_budget.assert_admissible(conn, "obj_1", limits)


def test_projected_call_cannot_overshoot_any_hard_ceiling(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    limits = {
        "max_cycles_per_objective": 2,
        "max_actions_per_objective": 2,
        "max_input_tokens_per_objective": 100,
        "max_output_tokens_per_objective": 100,
        "max_compute_cost_minor_per_objective": 10,
    }
    resource_budget.record_cycle(
        conn,
        objective_id="obj_1",
        actions=1,
        input_tokens=90,
        output_tokens=10,
        estimated_compute_cost_minor=5,
    )
    with pytest.raises(
        resource_budget.ResourceBudgetError,
        match="max_input_tokens_per_objective",
    ):
        resource_budget.assert_projected_admissible(
            conn,
            "obj_1",
            limits,
            cycles=1,
            input_tokens=11,
            output_tokens=1,
            estimated_compute_cost_minor=1,
        )
    assert resource_budget.usage(conn, "obj_1")["cycles"] == 1


def test_pre_call_reservation_accepts_only_post_plan_action_accounting(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    resource_budget.record_cycle(
        conn,
        objective_id="obj_1",
        actions=0,
        input_tokens=100,
        output_tokens=50,
        estimated_compute_cost_minor=2,
    )
    resource_budget.record_actions(
        conn, objective_id="obj_1", actions=1
    )
    usage = resource_budget.usage(conn, "obj_1")
    assert usage["cycles"] == 1
    assert usage["actions"] == 1
    with pytest.raises(
        resource_budget.ResourceBudgetError,
        match="no pre-call reservation",
    ):
        resource_budget.record_actions(
            conn, objective_id="missing", actions=1
        )


def test_company_compute_envelope_is_shared_and_reduces_spendable_cash(
    tmp_path,
):
    path = tmp_path / "authority.db"
    conn = objectives_db.connect(path)
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Bounded Company",
        purpose="Never allocate one dollar twice",
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
    objectives = [
        objectives_db.create_objective(
            conn,
            organization_id=organization_id,
            desired_outcome=f"Objective {index}",
            originator="employee:ceo",
        )
        for index in (1, 2)
    ]
    limits = {
        **resource_budget.DEFAULT_LIMITS,
        "max_compute_cost_minor_per_organization": 15,
    }
    resource_budget.reserve_planner_call(
        conn,
        objective_id=objectives[0].id,
        limits=limits,
        input_tokens=100,
        output_tokens=100,
        estimated_compute_cost_minor=10,
        enforce_treasury=True,
    )
    assert finance_db.committed_compute_balance(conn, account_id) == 10
    assert finance_db.available_balance(conn, account_id) == 990
    with pytest.raises(
        resource_budget.ResourceBudgetError,
        match="organization exhausted",
    ):
        resource_budget.reserve_planner_call(
            conn,
            objective_id=objectives[1].id,
            limits=limits,
            input_tokens=100,
            output_tokens=100,
            estimated_compute_cost_minor=10,
            enforce_treasury=True,
        )
    with pytest.raises(finance_db.BudgetError, match="insufficient"):
        finance_db.reserve_budget(
            conn,
            account_id=account_id,
            objective_id=objectives[1].id,
            action_id="action-cannot-double-allocate",
            amount_minor=991,
            currency="USD",
            expires_at=9_999_999_999,
        )
    conn.close()
    restarted = objectives_db.connect(path)
    assert finance_db.committed_compute_balance(restarted, account_id) == 10
    assert finance_db.available_balance(restarted, account_id) == 990


def test_concurrent_objectives_cannot_race_past_company_compute_limit(
    tmp_path,
):
    path = tmp_path / "authority.db"
    conn = objectives_db.connect(path)
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Concurrent Company",
        purpose="Serialize aggregate financial authority",
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
    objective_ids = [
        objectives_db.create_objective(
            conn,
            organization_id=organization_id,
            desired_outcome=f"Concurrent objective {index}",
            originator="employee:ceo",
        ).id
        for index in (1, 2)
    ]
    conn.close()
    limits = {
        **resource_budget.DEFAULT_LIMITS,
        "max_compute_cost_minor_per_organization": 15,
    }

    def reserve(objective_id):
        worker = objectives_db.connect(path)
        try:
            resource_budget.reserve_planner_call(
                worker,
                objective_id=objective_id,
                limits=limits,
                input_tokens=100,
                output_tokens=100,
                estimated_compute_cost_minor=10,
                enforce_treasury=True,
            )
            return "reserved"
        except resource_budget.ResourceBudgetError:
            return "rejected"
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, objective_ids))
    assert sorted(outcomes) == ["rejected", "reserved"]
    restarted = objectives_db.connect(path)
    assert (
        resource_budget.organization_compute_usage(
            restarted, organization_id
        )
        == 10
    )
    assert finance_db.available_balance(restarted, account_id) == 990


@pytest.mark.parametrize(
    ("status", "actual_minor", "provider_reference", "expected_available"),
    [
        ("included", 0, None, 1_000),
        ("provider_confirmed", 6, "invoice-line-1", 994),
    ],
)
def test_compute_reconciliation_releases_reservation_and_records_real_cost(
    tmp_path,
    status,
    actual_minor,
    provider_reference,
    expected_available,
):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Reconciled Company",
        purpose="Distinguish estimates from evidenced cost",
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
        desired_outcome="Use bounded compute",
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
    reconciliation_id = resource_budget.reconcile_compute_reservation(
        conn,
        reservation_id=reservation_id,
        status=status,
        actual_minor=actual_minor,
        model="planner-model",
        billing_provider="test-provider",
        provider_reference=provider_reference,
        evidence={"provider_readback": status == "provider_confirmed"},
    )
    assert finance_db.committed_compute_balance(conn, account_id) == 0
    assert finance_db.available_balance(conn, account_id) == expected_available
    assert (
        resource_budget.organization_compute_usage(conn, organization_id)
        == actual_minor
    )
    assert resource_budget.compute_reservation_posture(
        conn, organization_id
    )["unreconciled_count"] == 0
    if actual_minor:
        entry = conn.execute(
            """SELECT * FROM treasury_entries
                WHERE idempotency_key=?""",
            (f"compute-settlement:{reservation_id}",),
        ).fetchone()
        assert entry["amount_minor"] == -actual_minor
        assert entry["kind"] == "ai_compute"
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            """UPDATE planner_compute_reconciliations
                  SET actual_minor=0 WHERE id=?""",
            (reconciliation_id,),
        )


def test_housekeeping_repairs_crash_after_compute_expense_before_lineage(
    tmp_path,
):
    conn = objectives_db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Crash-Safe Compute Company",
        purpose="Reconcile every model expense exactly once",
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
        desired_outcome="Survive settlement crash",
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
    finance_db.record_entry(
        conn,
        account_id=account_id,
        kind="ai_compute",
        amount_minor=-6,
        currency="USD",
        objective_id=objective.id,
        external_reference="invoice-line-crash",
        idempotency_key=f"compute-settlement:{reservation_id}",
        evidence={
            "provider_readback": True,
            "billing_provider": "provider",
            "model": "planner-model",
        },
    )
    assert finance_db.committed_compute_balance(conn, account_id) == 10
    assert resource_budget.repair_ledger_backed_reconciliations(conn) == [
        reservation_id
    ]
    assert resource_budget.repair_ledger_backed_reconciliations(conn) == []
    assert finance_db.committed_compute_balance(conn, account_id) == 0
    assert finance_db.available_balance(conn, account_id) == 994
    assert conn.execute(
        "SELECT COUNT(*) FROM treasury_entries WHERE kind='ai_compute'"
    ).fetchone()[0] == 1
