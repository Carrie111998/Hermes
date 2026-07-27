from __future__ import annotations

import sqlite3
import time

import pytest

from hermes_cli import objectives_db as odb
from hermes_cli import verification_evidence


@pytest.fixture
def conn(tmp_path):
    db = odb.connect(tmp_path / "objectives.db")
    yield db
    db.close()


def _accepted_objective(conn):
    objective = odb.create_objective(
        conn,
        desired_outcome="Deploy a verified release",
        originator="user:mike",
        success_criteria=["live hash matches artifact"],
        permitted_systems=["git", "deployment"],
        prohibited_actions=["delete production data"],
    )
    return odb.transition_objective(
        conn, objective.id, "accepted", actor="user:mike"
    )


def test_objective_completion_is_fail_closed_without_verification(conn):
    objective = _accepted_objective(conn)
    plan_id = odb.create_plan(
        conn,
        objective.id,
        assumptions=[],
        tasks=[{"id": "deploy"}],
        dependencies=[],
        risks=["production change"],
        created_by="planner",
    )
    odb.transition_objective(conn, objective.id, "planned", actor="control")
    odb.transition_objective(conn, objective.id, "authorized", actor="control")
    odb.transition_objective(conn, objective.id, "executing", actor="worker")
    odb.transition_objective(conn, objective.id, "completed", actor="worker")

    with pytest.raises(odb.ObjectiveStateError, match="passing verification"):
        odb.transition_objective(conn, objective.id, "verified", actor="worker")

    odb.record_verification(
        conn,
        objective_id=objective.id,
        plan_id=plan_id,
        verifier="http-readback",
        method="content-hash",
        verdict="pass",
        evidence=verification_evidence.build(
            observer="http-readback",
            source_kind="provider_readback",
            source_reference="https://example.test",
            facts={"sha256": "abc"},
        ),
    )
    verified = odb.transition_objective(
        conn, objective.id, "verified", actor="control"
    )
    assert verified.status == "verified"


def test_authority_store_uses_durable_and_concurrent_safe_pragmas(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 30_000


def test_replan_invalidates_older_passing_verification(conn):
    objective = _accepted_objective(conn)
    plan_v1 = odb.create_plan(
        conn,
        objective.id,
        assumptions=[],
        tasks=[],
        dependencies=[],
        risks=[],
        created_by="planner",
    )
    odb.record_verification(
        conn,
        objective_id=objective.id,
        plan_id=plan_v1,
        verifier="test",
        method="fixture",
        verdict="pass",
        evidence=verification_evidence.build(
            observer="test",
            source_kind="deterministic_check",
            source_reference="fixture",
            facts={"ok": True},
        ),
    )
    odb.create_plan(
        conn,
        objective.id,
        assumptions=["new state"],
        tasks=[],
        dependencies=[],
        risks=[],
        created_by="planner",
    )
    odb.transition_objective(conn, objective.id, "planned", actor="control")
    odb.transition_objective(conn, objective.id, "authorized", actor="control")
    odb.transition_objective(conn, objective.id, "executing", actor="worker")
    odb.transition_objective(conn, objective.id, "completed", actor="worker")

    with pytest.raises(odb.ObjectiveStateError, match="current plan"):
        odb.transition_objective(conn, objective.id, "verified", actor="control")


def test_authority_database_rejects_unstructured_self_attestation(conn):
    objective = _accepted_objective(conn)
    plan_id = odb.create_plan(
        conn,
        objective.id,
        assumptions=[],
        tasks=[],
        dependencies=[],
        risks=[],
        created_by="planner",
    )
    with pytest.raises(ValueError, match="verification evidence missing"):
        odb.record_verification(
            conn,
            objective_id=objective.id,
            plan_id=plan_id,
            verifier="model:self",
            method="model says done",
            verdict="pass",
            evidence={"looks_good": True},
        )


def test_permit_is_single_use_and_bound_to_payload_and_executor(conn):
    objective = _accepted_objective(conn)
    plan_id = odb.create_plan(
        conn,
        objective.id,
        assumptions=[],
        tasks=[],
        dependencies=[],
        risks=[],
        created_by="planner",
    )
    odb.transition_objective(conn, objective.id, "planned", actor="control")
    payload = {"path": "/tmp/artifact", "sha256": "abc"}
    action_id = odb.propose_action(
        conn,
        objective_id=objective.id,
        plan_id=plan_id,
        action_type="artifact.write",
        payload=payload,
        expected_outcome="artifact exists",
        required_capability="filesystem.write",
        verification_method="read-back hash",
        risk_class="low",
        reversible=True,
        proposed_by="planner",
    )
    permit_id = odb.issue_permit(
        conn,
        action_id,
        capability="filesystem.write",
        issued_to="worker-1",
        policy_version="test-v1",
        expires_at=int(time.time()) + 60,
    )

    with pytest.raises(odb.PermitError, match="payload"):
        odb.consume_permit(
            conn,
            permit_id,
            action_id=action_id,
            payload={**payload, "sha256": "changed"},
            executor="worker-1",
        )
    with pytest.raises(odb.PermitError, match="different executor"):
        odb.consume_permit(
            conn,
            permit_id,
            action_id=action_id,
            payload=payload,
            executor="worker-2",
        )

    odb.consume_permit(
        conn,
        permit_id,
        action_id=action_id,
        payload=payload,
        executor="worker-1",
    )
    with pytest.raises(odb.PermitError, match="already consumed"):
        odb.consume_permit(
            conn,
            permit_id,
            action_id=action_id,
            payload=payload,
            executor="worker-1",
        )


def test_permit_rejects_action_from_superseded_plan(conn):
    objective = _accepted_objective(conn)
    plan_v1 = odb.create_plan(
        conn, objective.id, assumptions=[], tasks=[], dependencies=[], risks=[], created_by="planner"
    )
    odb.transition_objective(conn, objective.id, "planned", actor="control")
    action_id = odb.propose_action(
        conn, objective_id=objective.id, plan_id=plan_v1,
        action_type="artifact.write", payload={"path": "artifact"},
        expected_outcome="artifact exists", required_capability="filesystem.write",
        verification_method="artifact.readback", risk_class="low", reversible=True,
        proposed_by="planner",
    )
    odb.create_plan(
        conn, objective.id, assumptions=["replanned"], tasks=[], dependencies=[],
        risks=[], created_by="planner",
    )
    with pytest.raises(odb.PermitError, match="superseded plan"):
        odb.issue_permit(
            conn, action_id, capability="filesystem.write", issued_to="worker",
            policy_version="test-v1", expires_at=int(time.time()) + 60,
        )


def test_permit_consumption_rejects_stale_policy_version(conn):
    objective = _accepted_objective(conn)
    plan_id = odb.create_plan(
        conn, objective.id, assumptions=[], tasks=[], dependencies=[], risks=[], created_by="planner"
    )
    odb.transition_objective(conn, objective.id, "planned", actor="control")
    payload = {"path": "artifact"}
    action_id = odb.propose_action(
        conn, objective_id=objective.id, plan_id=plan_id,
        action_type="artifact.write", payload=payload,
        expected_outcome="artifact exists", required_capability="filesystem.write",
        verification_method="artifact.readback", risk_class="low", reversible=True,
        proposed_by="planner",
    )
    permit_id = odb.issue_permit(
        conn, action_id, capability="filesystem.write", issued_to="worker",
        policy_version="policy-v1", expires_at=int(time.time()) + 60,
    )
    with pytest.raises(odb.PermitError, match="policy version is stale"):
        odb.consume_permit(
            conn, permit_id, action_id=action_id, payload=payload,
            executor="worker", current_policy_version="policy-v2",
        )


def test_permit_consumption_rejects_cancelled_objective(conn):
    objective = _accepted_objective(conn)
    plan_id = odb.create_plan(
        conn, objective.id, assumptions=[], tasks=[], dependencies=[], risks=[], created_by="planner"
    )
    odb.transition_objective(conn, objective.id, "planned", actor="control")
    payload = {"path": "artifact"}
    action_id = odb.propose_action(
        conn, objective_id=objective.id, plan_id=plan_id,
        action_type="artifact.write", payload=payload,
        expected_outcome="artifact exists", required_capability="filesystem.write",
        verification_method="artifact.readback", risk_class="low", reversible=True,
        proposed_by="planner",
    )
    permit_id = odb.issue_permit(
        conn, action_id, capability="filesystem.write", issued_to="worker",
        policy_version="policy-v1", expires_at=int(time.time()) + 60,
    )
    odb.transition_objective(conn, objective.id, "cancelled", actor="human:advisor")
    with pytest.raises(odb.PermitError, match="no longer admits execution"):
        odb.consume_permit(
            conn, permit_id, action_id=action_id, payload=payload, executor="worker",
            current_policy_version="policy-v1",
        )


def test_claim_skips_events_for_terminal_objectives(conn):
    objective = _accepted_objective(conn)
    plan_id = odb.create_plan(
        conn, objective.id, assumptions=[], tasks=[], dependencies=[], risks=[], created_by="ceo"
    )
    odb.transition_objective(conn, objective.id, "planned", actor="ceo")
    odb.transition_objective(conn, objective.id, "authorized", actor="ceo")
    odb.transition_objective(conn, objective.id, "executing", actor="ceo")
    odb.transition_objective(conn, objective.id, "completed", actor="ceo")
    odb.record_verification(
        conn, objective_id=objective.id, plan_id=plan_id, verifier="test",
        method="fixture", verdict="pass",
        evidence=verification_evidence.build(
            observer="test", source_kind="deterministic_check",
            source_reference="fixture", facts={"ok": True},
        ),
    )
    odb.transition_objective(conn, objective.id, "verified", actor="ceo")
    odb.enqueue_objective_event(
        conn, objective_id=objective.id, event_type="late.event", payload={}
    )
    assert odb.claim_objective_event(conn, runtime_id="runtime") is None


def test_expired_objective_cannot_continue(conn):
    objective = odb.create_objective(
        conn,
        desired_outcome="Time-bounded operation",
        originator="user:mike",
        expires_at=int(time.time()) - 1,
    )
    with pytest.raises(odb.ObjectiveStateError, match="expired"):
        odb.transition_objective(conn, objective.id, "accepted", actor="control")

    expired = odb.transition_objective(
        conn, objective.id, "expired", actor="scheduler"
    )
    assert expired.status == "expired"


def test_plan_versions_are_immutable_and_monotonic(conn):
    objective = _accepted_objective(conn)
    first = odb.create_plan(
        conn,
        objective.id,
        assumptions=["v1"],
        tasks=[],
        dependencies=[],
        risks=[],
        created_by="planner",
    )
    second = odb.create_plan(
        conn,
        objective.id,
        assumptions=["v2"],
        tasks=[],
        dependencies=[],
        risks=[],
        created_by="planner",
    )
    rows = conn.execute(
        "SELECT id, version, supersedes_id FROM plans ORDER BY version"
    ).fetchall()
    assert [(row["id"], row["version"]) for row in rows] == [
        (first, 1),
        (second, 2),
    ]
    assert rows[1]["supersedes_id"] == first


def test_authority_contracts_and_evidence_are_database_immutable(conn):
    objective = _accepted_objective(conn)
    plan_id = odb.create_plan(
        conn, objective.id, assumptions=[], tasks=[], dependencies=[],
        risks=[], created_by="planner",
    )
    odb.transition_objective(conn, objective.id, "planned", actor="control")
    payload = {"resource": "customer:1", "value": "active"}
    action_id = odb.propose_action(
        conn, objective_id=objective.id, plan_id=plan_id,
        action_type="customer.update", payload=payload,
        expected_outcome="customer is active",
        required_capability="crm.write",
        verification_method="crm.readback", risk_class="low",
        reversible=True, proposed_by="planner",
    )
    permit_id = odb.issue_permit(
        conn, action_id, capability="crm.write", issued_to="worker",
        policy_version="v1", expires_at=int(time.time()) + 60,
    )
    odb.consume_permit(
        conn, permit_id, action_id=action_id, payload=payload, executor="worker",
    )
    result_id = odb.record_execution_result(
        conn, action_id=action_id, permit_id=permit_id, executor="worker",
        status="succeeded", result={"provider_id": "change-1"},
        started_at=int(time.time()),
    )
    verification_id = odb.record_verification(
        conn, objective_id=objective.id, plan_id=plan_id, action_id=action_id,
        execution_result_id=result_id, verifier="crm-reader",
        method="crm.readback", verdict="pass",
        evidence=verification_evidence.build(
            observer="crm-reader", source_kind="provider_readback",
            source_reference="customer:1", facts={"status": "active"},
        ),
    )

    attempts = (
        ("UPDATE plans SET tasks_json='[]' WHERE id=?", plan_id),
        ("UPDATE candidate_actions SET payload_json='{}' WHERE id=?", action_id),
        ("UPDATE permits SET capability='admin' WHERE id=?", permit_id),
        ("UPDATE execution_results SET result_json='{}' WHERE id=?", result_id),
        (
            "UPDATE verification_records SET verdict='fail' WHERE id=?",
            verification_id,
        ),
        (
            "UPDATE objective_events SET actor='rewriter' WHERE objective_id=?",
            objective.id,
        ),
    )
    for statement, record_id in attempts:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(statement, (record_id,))

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM execution_results WHERE id=?", (result_id,))


def test_claim_keeper_prevents_split_brain_reclaim_during_long_work(tmp_path):
    path = tmp_path / "objectives.db"
    owner = odb.connect(path)
    objective = _accepted_objective(owner)
    event_id = odb.enqueue_objective_event(
        owner,
        objective_id=objective.id,
        event_type="operating.review",
        payload={"reason": "long provider call"},
        dedupe_key="long-provider-call",
    )
    event = odb.claim_objective_event(
        owner,
        runtime_id="runtime-owner",
        claim_ttl_seconds=3,
    )
    assert event["id"] == event_id

    with odb.ObjectiveClaimKeeper(
        owner,
        event_id=event_id,
        runtime_id="runtime-owner",
        claim_ttl_seconds=3,
    ) as keeper:
        time.sleep(3.2)
        contender = odb.connect(path)
        try:
            assert odb.claim_objective_event(
                contender,
                runtime_id="runtime-contender",
                claim_ttl_seconds=3,
            ) is None
        finally:
            contender.close()
        keeper.assert_owned()

    odb.finish_objective_event(
        owner,
        event_id,
        runtime_id="runtime-owner",
        status="completed",
    )


def test_claim_keeper_detects_ownership_loss(tmp_path):
    path = tmp_path / "objectives.db"
    owner = odb.connect(path)
    objective = _accepted_objective(owner)
    event_id = odb.enqueue_objective_event(
        owner,
        objective_id=objective.id,
        event_type="operating.review",
        payload={},
        dedupe_key="ownership-loss",
    )
    odb.claim_objective_event(
        owner,
        runtime_id="runtime-owner",
        claim_ttl_seconds=3,
    )

    with pytest.raises(odb.ObjectiveStateError, match="ownership was lost"):
        with odb.ObjectiveClaimKeeper(
            owner,
            event_id=event_id,
            runtime_id="runtime-owner",
            claim_ttl_seconds=3,
        ):
            attacker = odb.connect(path)
            try:
                attacker.execute(
                    """UPDATE objective_inbox SET claimed_by='runtime-other'
                        WHERE id=?""",
                    (event_id,),
                )
                attacker.commit()
            finally:
                attacker.close()
            time.sleep(1.2)
