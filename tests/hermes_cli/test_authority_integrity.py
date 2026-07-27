from __future__ import annotations

import json
import time

import pytest

from hermes_cli import (
    authority_integrity,
    business_audit,
    operational_control,
    organization_db,
)
from hermes_cli import objectives_db as db


def _company(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Integrity Company",
        purpose="Operate from verified authority",
        profile_name="default",
        charter={},
    )
    return conn, organization_id


def test_preflight_establishes_legacy_baseline_and_records_ready_evidence(tmp_path):
    conn, organization_id = _company(tmp_path)
    policy = {"enabled": True, "policy_version": "test-v1"}

    posture = authority_integrity.run_preflight(
        conn, organization_id=organization_id, policy=policy
    )

    assert posture.ready
    assert posture.checks["database_quick_check"] is True
    assert posture.checks["foreign_key_check"] is True
    latest = authority_integrity.latest_posture(conn, organization_id)
    assert latest["id"] == posture.run_id
    assert latest["evidence_sha256"]
    baseline = conn.execute(
        "SELECT * FROM authority_policy_baselines WHERE organization_id=?",
        (organization_id,),
    ).fetchone()
    assert baseline["accepted_by"] == "system:legacy-migration"
    conn.close()


def test_explicit_policy_baselines_are_versioned_and_immutable(tmp_path):
    conn, organization_id = _company(tmp_path)
    first = authority_integrity.accept_policy_baseline(
        conn,
        organization_id=organization_id,
        policy={"enabled": True, "max_action_spend_minor": 10},
        actor="human:setup",
        reason="initial charter",
    )
    second = authority_integrity.accept_policy_baseline(
        conn,
        organization_id=organization_id,
        policy={"enabled": True, "max_action_spend_minor": 20},
        actor="human:setup",
        reason="reviewed increase",
    )

    assert first != second
    versions = conn.execute(
        """SELECT version FROM authority_policy_baselines
           WHERE organization_id=? ORDER BY version""",
        (organization_id,),
    ).fetchall()
    assert [row["version"] for row in versions] == [1, 2]
    with pytest.raises(Exception, match="immutable"):
        conn.execute(
            "UPDATE authority_policy_baselines SET reason='rewritten' WHERE id=?",
            (first,),
        )
    conn.close()


def test_policy_revision_revokes_every_unconsumed_execution_permit(tmp_path):
    conn, organization_id = _company(tmp_path)
    authority_integrity.accept_policy_baseline(
        conn,
        organization_id=organization_id,
        policy={"enabled": True, "allowed_capabilities": ["work.delegate"]},
        actor="human:setup",
        reason="initial charter",
    )
    objective = db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Bounded work",
        originator="employee:ceo",
        permitted_systems=["kanban"],
    )
    db.transition_objective(
        conn, objective.id, "accepted", actor="employee:ceo"
    )
    plan_id = db.create_plan(
        conn,
        objective.id,
        assumptions=[],
        tasks=[{"id": "task"}],
        dependencies=[],
        risks=[],
        created_by="employee:ceo",
    )
    db.transition_objective(conn, objective.id, "planned", actor="control")
    payload = {"system": "kanban", "target_resource": "default"}
    action_id = db.propose_action(
        conn,
        objective_id=objective.id,
        plan_id=plan_id,
        action_type="kanban.create_task",
        payload=payload,
        expected_outcome="task exists",
        required_capability="work.delegate",
        verification_method="kanban.task.created",
        risk_class="low",
        reversible=True,
        proposed_by="employee:ceo",
    )
    permit_id = db.issue_permit(
        conn,
        action_id,
        capability="work.delegate",
        issued_to="executor:kanban",
        policy_version="charter-v1",
        expires_at=int(time.time()) + 300,
    )

    authority_integrity.accept_policy_baseline(
        conn,
        organization_id=organization_id,
        policy={"enabled": True, "allowed_capabilities": []},
        actor="human:setup",
        reason="remove delegation authority",
    )

    permit = conn.execute(
        "SELECT * FROM permits WHERE id=?", (permit_id,)
    ).fetchone()
    action = conn.execute(
        "SELECT status FROM candidate_actions WHERE id=?", (action_id,)
    ).fetchone()
    assert permit["revoked_at"] is not None
    assert action["status"] == "expired"
    with pytest.raises(db.PermitError, match="revoked"):
        db.consume_permit(
            conn,
            permit_id,
            action_id=action_id,
            payload=payload,
            executor="executor:kanban",
        )
    conn.close()


def test_policy_drift_pauses_autonomy_and_raises_one_bounded_intervention(tmp_path):
    conn, organization_id = _company(tmp_path)
    accepted = {"enabled": True, "max_action_spend_minor": 10}
    authority_integrity.accept_policy_baseline(
        conn,
        organization_id=organization_id,
        policy=accepted,
        actor="human:setup",
        reason="initial charter",
    )
    changed = {"enabled": True, "max_action_spend_minor": 1000}

    first = authority_integrity.enforce_preflight(
        conn, organization_id=organization_id, policy=changed
    )
    second = authority_integrity.enforce_preflight(
        conn, organization_id=organization_id, policy=changed
    )

    assert not first.ready
    assert not second.ready
    assert first.checks["policy_matches_baseline"] is False
    assert operational_control.autonomy_state(conn)["mode"] == "paused"
    interventions = operational_control.list_interventions(
        conn, organization_id=organization_id
    )
    assert len(interventions) == 1
    assert interventions[0]["category"] == "authority_integrity_failure"
    assert "policy_json" not in json.dumps(interventions[0])
    assert (
        conn.execute(
            """SELECT COUNT(*) FROM authority_integrity_runs
               WHERE organization_id=?""",
            (organization_id,),
        ).fetchone()[0]
        == 1
    )
    with pytest.raises(
        operational_control.AutonomyRevokedError,
        match="integrity passes",
    ):
        operational_control.set_autonomy_mode(
            conn,
            mode="autonomous",
            actor="human:advisor",
            reason="attempted resume without remediation",
        )
    conn.close()


def test_tampered_audit_chain_fails_closed(tmp_path):
    conn, organization_id = _company(tmp_path)
    policy = {"enabled": True}
    authority_integrity.accept_policy_baseline(
        conn,
        organization_id=organization_id,
        policy=policy,
        actor="human:setup",
        reason="initial charter",
    )
    event_id = business_audit.append(
        conn,
        organization_id=organization_id,
        event_type="test",
        payload={"value": 1},
    )
    conn.execute("DROP TRIGGER business_audit_events_immutable_update")
    conn.execute(
        "UPDATE business_audit_events SET payload_json='{\"value\":2}' WHERE id=?",
        (event_id,),
    )
    conn.commit()

    posture = authority_integrity.enforce_preflight(
        conn, organization_id=organization_id, policy=policy
    )

    assert posture.checks["audit_chain"] is False
    assert operational_control.autonomy_state(conn)["mode"] == "paused"
    conn.close()


def test_integrity_run_evidence_is_immutable(tmp_path):
    conn, organization_id = _company(tmp_path)
    posture = authority_integrity.run_preflight(
        conn, organization_id=organization_id, policy={"enabled": True}
    )
    with pytest.raises(Exception, match="immutable"):
        conn.execute(
            "UPDATE authority_integrity_runs SET status='ready' WHERE id=?",
            (posture.run_id,),
        )
    conn.close()
