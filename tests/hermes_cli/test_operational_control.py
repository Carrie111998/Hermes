import sqlite3
import time

import pytest

from hermes_cli import objectives_db, operational_control


def test_authority_schema_check_preserves_active_transaction(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    operational_control.ensure_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE autonomy_control SET reason=? WHERE singleton=1",
        ("uncommitted authority transition",),
    )
    assert operational_control.autonomy_state(conn)["reason"] == (
        "uncommitted authority transition"
    )
    assert conn.in_transaction is True
    conn.rollback()


def _conn(tmp_path):
    return objectives_db.connect(tmp_path / "authority.db")


def test_master_pause_revokes_unconsumed_permits_and_blocks_leases(tmp_path):
    conn = _conn(tmp_path)
    operational_control.ensure_schema(conn)
    operational_control.set_autonomy_mode(
        conn, mode="paused", actor="owner", reason="company sale"
    )
    with pytest.raises(operational_control.AutonomyRevokedError):
        operational_control.acquire_resource_lease(
            conn, resource_key="crm:account:1", owner="worker", action_id="a1"
        )


def test_resource_lease_deterministically_excludes_second_worker(tmp_path):
    conn = _conn(tmp_path)
    first_token = operational_control.acquire_resource_lease(
        conn, resource_key="crm:account:1", owner="sales", action_id="a1"
    )
    with pytest.raises(operational_control.ResourceConflictError):
        operational_control.acquire_resource_lease(
            conn, resource_key="crm:account:1", owner="operations", action_id="a2"
        )
    operational_control.release_resource_lease(
        conn, resource_key="crm:account:1", owner="sales",
        action_id="a1", fence_token=first_token,
    )
    operational_control.acquire_resource_lease(
        conn, resource_key="crm:account:1", owner="operations", action_id="a2"
    )


def test_expired_worker_cannot_release_or_validate_successor_lease(
    tmp_path, monkeypatch
):
    conn = _conn(tmp_path)
    monkeypatch.setattr(operational_control.time, "time", lambda: 1_000)
    stale_token = operational_control.acquire_resource_lease(
        conn, resource_key="crm:account:1", owner="worker-1",
        action_id="a1", ttl_seconds=10,
    )
    monkeypatch.setattr(operational_control.time, "time", lambda: 1_011)
    current_token = operational_control.acquire_resource_lease(
        conn, resource_key="crm:account:1", owner="worker-2",
        action_id="a2", ttl_seconds=10,
    )
    assert current_token > stale_token

    operational_control.release_resource_lease(
        conn, resource_key="crm:account:1", owner="worker-1",
        action_id="a1", fence_token=stale_token,
    )
    operational_control.assert_resource_lease(
        conn, resource_key="crm:account:1", owner="worker-2",
        action_id="a2", fence_token=current_token,
    )
    with pytest.raises(operational_control.ResourceConflictError, match="stale"):
        operational_control.assert_resource_lease(
            conn, resource_key="crm:account:1", owner="worker-1",
            action_id="a1", fence_token=stale_token,
        )


def test_same_worker_cannot_overwrite_active_lease_for_another_action(tmp_path):
    conn = _conn(tmp_path)
    operational_control.acquire_resource_lease(
        conn, resource_key="crm:account:1", owner="worker", action_id="a1"
    )
    with pytest.raises(operational_control.ResourceConflictError):
        operational_control.acquire_resource_lease(
            conn, resource_key="crm:account:1", owner="worker", action_id="a2"
        )


def test_resource_keeper_prevents_takeover_during_long_provider_call(tmp_path):
    path = tmp_path / "authority.db"
    conn = objectives_db.connect(path)
    token = operational_control.acquire_resource_lease(
        conn,
        resource_key="payments:vendor:1",
        owner="runtime-1",
        action_id="action-1",
        ttl_seconds=3,
    )
    with operational_control.ResourceLeaseKeeper(
        conn,
        resource_key="payments:vendor:1",
        owner="runtime-1",
        action_id="action-1",
        fence_token=token,
        ttl_seconds=3,
    ) as keeper:
        time.sleep(3.2)
        contender = objectives_db.connect(path)
        try:
            with pytest.raises(operational_control.ResourceConflictError):
                operational_control.acquire_resource_lease(
                    contender,
                    resource_key="payments:vendor:1",
                    owner="runtime-2",
                    action_id="action-2",
                    ttl_seconds=3,
                )
        finally:
            contender.close()
        keeper.assert_owned()


def test_resource_keeper_cannot_revive_lease_after_master_pause(tmp_path):
    path = tmp_path / "authority.db"
    conn = objectives_db.connect(path)
    token = operational_control.acquire_resource_lease(
        conn,
        resource_key="payments:vendor:1",
        owner="runtime-1",
        action_id="action-1",
        ttl_seconds=3,
    )
    with pytest.raises(
        operational_control.ResourceConflictError, match="lost during execution"
    ):
        with operational_control.ResourceLeaseKeeper(
            conn,
            resource_key="payments:vendor:1",
            owner="runtime-1",
            action_id="action-1",
            fence_token=token,
            ttl_seconds=3,
        ):
            control = objectives_db.connect(path)
            try:
                operational_control.set_autonomy_mode(
                    control,
                    mode="paused",
                    actor="owner",
                    reason="emergency stop",
                )
            finally:
                control.close()
            time.sleep(1.2)


def test_restart_recovery_creates_one_evidenced_handoff_and_never_replays(tmp_path):
    conn = _conn(tmp_path)
    objective = objectives_db.create_objective(
        conn, desired_outcome="Settle vendor", originator="owner"
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
    objectives_db.transition_objective(
        conn, objective.id, "planned", actor="ceo"
    )
    action_id = objectives_db.propose_action(
        conn,
        objective_id=objective.id,
        plan_id=plan_id,
        action_type="payment.send",
        payload={"system": "payments", "target_resource": "vendor:1"},
        expected_outcome="vendor paid",
        required_capability="payment.send",
        verification_method="provider.readback",
        risk_class="high",
        reversible=False,
        proposed_by="ceo",
    )
    permit_id = objectives_db.issue_permit(
        conn,
        action_id,
        capability="payment.send",
        issued_to="employee:ceo",
        policy_version="v1",
        expires_at=2_000_000_000,
    )
    objectives_db.consume_permit(
        conn,
        permit_id,
        action_id=action_id,
        payload={"system": "payments", "target_resource": "vendor:1"},
        executor="employee:ceo",
    )

    first = operational_control.recover_incomplete_executions(conn)
    second = operational_control.recover_incomplete_executions(conn)
    assert first == second
    open_items = operational_control.list_interventions(conn)
    assert len(open_items) == 1
    assert open_items[0]["category"] == "uncertain_external_effect"

    intervention_id = open_items[0]["id"]
    with pytest.raises(ValueError, match="recorded options"):
        operational_control.resolve_intervention(
            conn,
            intervention_id,
            option_id="blind_retry",
            actor="owner",
            evidence={"reviewed": True},
        )
    operational_control.resolve_intervention(
        conn,
        intervention_id,
        option_id="reconcile",
        actor="owner",
        evidence={"provider_readback": "not_settled"},
    )
    assert operational_control.list_interventions(conn) == []
    assert operational_control.list_interventions(
        conn, status="resolved"
    )[0]["resolution"]["option_id"] == "reconcile"


def test_abandon_resolution_terminates_blocked_objective_without_replanning(
    tmp_path,
):
    conn = _conn(tmp_path)
    objective = objectives_db.create_objective(
        conn, desired_outcome="Pursue a bounded opportunity", originator="owner"
    )
    objectives_db.transition_objective(conn, objective.id, "accepted", actor="owner")
    objectives_db.transition_objective(conn, objective.id, "planned", actor="ceo")
    objectives_db.transition_objective(
        conn, objective.id, "blocked", actor="control", reason="authority insufficient"
    )
    intervention_id = operational_control.raise_intervention(
        conn,
        objective_id=objective.id,
        category="authority_insufficient",
        summary="Action is outside the charter",
        context={"capability": "contracts.sign"},
        options=[
            {"id": "replan", "label": "Replan"},
            {"id": "abandon", "label": "Abandon"},
        ],
    )

    operational_control.resolve_intervention(
        conn,
        intervention_id,
        option_id="abandon",
        actor="human:advisor",
        evidence={"decision": "Expected value no longer justifies expansion"},
    )

    assert objectives_db.get_objective(conn, objective.id).status == "abandoned"
    assert conn.execute(
        "SELECT COUNT(*) FROM objective_inbox WHERE objective_id=?",
        (objective.id,),
    ).fetchone()[0] == 0
