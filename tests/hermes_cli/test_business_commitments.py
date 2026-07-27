from __future__ import annotations

import time

import pytest

from hermes_cli import (
    business_commitments,
    objective_service,
    operational_control,
    organization_db,
    verification_evidence,
)
from hermes_cli import objectives_db as db


def _context(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Commitment Company",
        purpose="Keep promises from authoritative state",
        profile_name="default",
        charter={},
    )
    objective = db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Deliver contracted customer outcome",
        originator="test",
    )
    db.transition_objective(conn, objective.id, "accepted", actor="test")
    return conn, organization_id, objective.id


def _create(conn, organization_id, objective_id, **overrides):
    values = {
        "organization_id": organization_id,
        "objective_id": objective_id,
        "kind": "customer_delivery",
        "title": "Deliver customer export",
        "description": "Provide the contracted export artifact",
        "counterparty_type": "customer",
        "counterparty_reference": "crm:customer-1",
        "source_system": "crm",
        "source_reference": "contract:revision-7",
        "due_at": 2_000,
        "grace_seconds": 100,
        "required_verifier": "delivery.provider_readback",
        "financial_exposure_minor": 5000,
        "currency": "USD",
        "idempotency_key": "commitment-key-1",
        "created_by": "employee:ceo",
        "now": 1_000,
    }
    values.update(overrides)
    return business_commitments.create_commitment(conn, **values)


def _verification(conn, objective_id, *, method, verdict="pass"):
    plan_id = db.create_plan(
        conn,
        objective_id,
        assumptions=[],
        tasks=[],
        dependencies=[],
        risks=[],
        created_by="planner",
    )
    return db.record_verification(
        conn,
        objective_id=objective_id,
        plan_id=plan_id,
        verifier="control:delivery",
        method=method,
        verdict=verdict,
        evidence=verification_evidence.build(
            observer="control:delivery",
            source_kind="provider_readback",
            source_reference="delivery:customer-1",
            facts={"delivered": verdict == "pass"},
        ),
    )


def test_commitment_contract_is_idempotent_immutable_and_hash_bound(tmp_path):
    conn, organization_id, objective_id = _context(tmp_path)
    commitment_id, created = _create(conn, organization_id, objective_id)
    repeated, created_again = _create(conn, organization_id, objective_id)

    assert repeated == commitment_id
    assert created is True
    assert created_again is False
    with pytest.raises(ValueError, match="different terms"):
        _create(
            conn,
            organization_id,
            objective_id,
            title="Different promise",
        )
    with pytest.raises(Exception, match="contract is immutable"):
        conn.execute(
            "UPDATE business_commitments SET due_at=9999 WHERE id=?",
            (commitment_id,),
        )
    with pytest.raises(Exception, match="cannot be deleted"):
        conn.execute(
            "DELETE FROM business_commitments WHERE id=?", (commitment_id,)
        )
    conn.close()


def test_fulfillment_requires_matching_passing_independent_verification(tmp_path):
    conn, organization_id, objective_id = _context(tmp_path)
    commitment_id, _ = _create(conn, organization_id, objective_id)
    failed = _verification(
        conn, objective_id, method="delivery.provider_readback", verdict="fail"
    )
    wrong_method = _verification(conn, objective_id, method="other.readback")

    with pytest.raises(ValueError, match="matching passing"):
        business_commitments.fulfill_commitment(
            conn,
            commitment_id=commitment_id,
            verification_id=failed,
            actor="employee:ceo",
            now=1900,
        )
    with pytest.raises(ValueError, match="matching passing"):
        business_commitments.fulfill_commitment(
            conn,
            commitment_id=commitment_id,
            verification_id=wrong_method,
            actor="employee:ceo",
            now=1900,
        )
    passed = _verification(
        conn, objective_id, method="delivery.provider_readback"
    )
    planning = business_commitments.planning_snapshot(conn, organization_id)
    assert planning["open"][0]["eligible_fulfillment_evidence"][0][
        "verification_id"
    ] == passed
    business_commitments.fulfill_commitment(
        conn,
        commitment_id=commitment_id,
        verification_id=passed,
        actor="employee:ceo",
        now=1900,
    )
    row = conn.execute(
        "SELECT * FROM business_commitments WHERE id=?", (commitment_id,)
    ).fetchone()
    assert row["status"] == "fulfilled"
    assert row["fulfilment_verification_id"] == passed
    with pytest.raises(Exception, match="invalid business commitment transition"):
        conn.execute(
            "UPDATE business_commitments SET status='active' WHERE id=?",
            (commitment_id,),
        )
    second, _ = _create(
        conn,
        organization_id,
        objective_id,
        idempotency_key="commitment-key-2",
        due_at=3000,
    )
    with pytest.raises(ValueError, match="already bound"):
        business_commitments.fulfill_commitment(
            conn,
            commitment_id=second,
            verification_id=passed,
            actor="employee:ceo",
            now=2000,
        )
    conn.close()


def test_deadline_scan_wakes_owner_then_marks_breach_once(tmp_path):
    conn, organization_id, objective_id = _context(tmp_path)
    commitment_id, _ = _create(conn, organization_id, objective_id)

    approaching = business_commitments.dispatch_due(
        conn,
        organization_id=organization_id,
        horizon_seconds=500,
        now=1600,
    )
    assert approaching["approaching"] == 1
    event = conn.execute(
        """SELECT * FROM objective_inbox
           WHERE objective_id=? AND event_type='commitment.deadline.approaching'""",
        (objective_id,),
    ).fetchone()
    assert event["priority_class"] == "high"
    assert event["deadline_at"] == 2000

    overdue = business_commitments.dispatch_due(
        conn,
        organization_id=organization_id,
        horizon_seconds=500,
        now=2050,
    )
    assert overdue["approaching"] == 1
    phases = conn.execute(
        """SELECT COUNT(*) FROM objective_inbox
           WHERE objective_id=? AND event_type='commitment.deadline.approaching'""",
        (objective_id,),
    ).fetchone()[0]
    assert phases == 2

    breached = business_commitments.dispatch_due(
        conn,
        organization_id=organization_id,
        horizon_seconds=500,
        now=2201,
    )
    repeated = business_commitments.dispatch_due(
        conn,
        organization_id=organization_id,
        horizon_seconds=500,
        now=2300,
    )
    assert breached["breached"] == 1
    assert repeated["breached"] == 0
    row = conn.execute(
        "SELECT status FROM business_commitments WHERE id=?", (commitment_id,)
    ).fetchone()
    assert row["status"] == "breached"
    breach_event = conn.execute(
        """SELECT * FROM objective_inbox
           WHERE objective_id=? AND event_type='commitment.breached'""",
        (objective_id,),
    ).fetchone()
    assert breach_event["priority_class"] == "critical"
    assert breach_event["priority"] == 98
    conn.close()


def test_terminal_owner_escalates_without_consuming_commitment(tmp_path):
    conn, organization_id, objective_id = _context(tmp_path)
    commitment_id, _ = _create(conn, organization_id, objective_id)
    db.transition_objective(conn, objective_id, "cancelled", actor="test")

    result = business_commitments.dispatch_due(
        conn,
        organization_id=organization_id,
        horizon_seconds=500,
        now=1600,
    )

    assert result["unowned"] == 1
    interventions = operational_control.list_interventions(
        conn, organization_id=organization_id
    )
    assert interventions[0]["category"] == "business_commitment_unowned"
    assert interventions[0]["context"]["commitment_id"] == commitment_id
    conn.close()


def test_supersession_is_atomic_and_preserves_both_contracts(tmp_path):
    conn, organization_id, objective_id = _context(tmp_path)
    first, _ = _create(conn, organization_id, objective_id)
    second, _ = _create(
        conn,
        organization_id,
        objective_id,
        idempotency_key="commitment-key-2",
        due_at=3000,
        supersedes_id=first,
    )
    statuses = {
        row["id"]: row["status"]
        for row in conn.execute(
            "SELECT id,status FROM business_commitments"
        ).fetchall()
    }
    assert statuses == {first: "superseded", second: "active"}
    conn.close()


def test_commitment_action_contracts_are_exposed_only_by_exact_charter(tmp_path):
    conn, organization_id, _ = _context(tmp_path)
    runtime = objective_service.build_runtime(
        conn,
        {
            "agentic": {
                "allowed_capabilities": ["commitments.manage"],
                "allowed_systems": ["commitments"],
            }
        },
    )
    assert runtime.planner.action_types == [
        "commitments.cancel",
        "commitments.create",
        "commitments.fulfill",
    ]
    assert runtime.planner.verification_methods == [
        "commitment.cancelled.readback",
        "commitment.fulfillment.readback",
        "commitment.record.readback",
    ]
    context = runtime.planner.context_provider()
    assert context["commitments"]["open_count"] == 0
    assert context["commitments"]["counterparty_details_included"] is False
    conn.close()
