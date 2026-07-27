from __future__ import annotations

import json
import time

import pytest

from hermes_cli import (
    approval_artifacts,
    objective_policy,
    objective_runtime,
    operational_control,
    organization_db,
    verification_evidence,
)
from hermes_cli import objectives_db as db


def test_approval_schema_read_preserves_active_transaction(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    approval_artifacts.ensure_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    approval_artifacts.ensure_schema(conn)
    assert conn.in_transaction is True
    conn.rollback()


def _context(tmp_path, *, risk="high"):
    conn = db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Approval Company",
        purpose="Bind approvals to exact effects",
        profile_name="default",
        charter={},
    )
    objective = db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Update the exact governed customer record",
        originator="test",
        permitted_systems=["crm"],
        max_spend_minor=1000,
        currency="USD",
    )
    db.transition_objective(conn, objective.id, "accepted", actor="test")
    plan_id = db.create_plan(
        conn,
        objective.id,
        assumptions=[],
        tasks=[],
        dependencies=[],
        risks=[],
        created_by="planner",
    )
    db.transition_objective(conn, objective.id, "planned", actor="test")
    action_id = db.propose_action(
        conn,
        objective_id=objective.id,
        plan_id=plan_id,
        action_type="crm.update",
        payload={
            "system": "crm",
            "target_resource": "customer:1",
            "idempotency_key": "approval-action-0001",
        },
        expected_outcome="customer record updated",
        required_capability="crm.write",
        verification_method="crm.readback",
        risk_class=risk,
        reversible=False,
        proposed_by="planner",
    )
    decision = objective_policy.evaluate_action(
        objective=db.objective_to_dict(conn, objective.id),
        action={
            **dict(
                conn.execute(
                    "SELECT * FROM candidate_actions WHERE id=?", (action_id,)
                ).fetchone()
            ),
            "payload": {
                "system": "crm",
                "target_resource": "customer:1",
                "idempotency_key": "approval-action-0001",
            },
        },
        charter=_charter(),
    )
    intervention_id = operational_control.raise_intervention(
        conn,
        organization_id=organization_id,
        objective_id=objective.id,
        action_id=action_id,
        category="authority_insufficient",
        summary=decision.reason,
        context={
            "policy_version": "charter-v1",
            "approval_eligible": decision.approval_eligible,
        },
        options=[
            {"id": "approve_exact_action", "label": "Approve exact action"},
            {"id": "replan", "label": "Replan"},
        ],
    )
    return conn, organization_id, objective.id, action_id, intervention_id


def _charter(**overrides):
    value = {
        "enabled": True,
        "operating_mode": "autonomous",
        "allowed_capabilities": ["crm.write"],
        "forbidden_capabilities": [],
        "allowed_systems": ["crm"],
        "approval_required_capabilities": [],
        "max_autonomous_risk": "low",
        "allow_irreversible": True,
        "max_action_spend_minor": 1000,
        "permit_ttl_seconds": 300,
    }
    value.update(overrides)
    return value


def test_artifact_binds_exact_action_scope_policy_and_evidence(tmp_path):
    conn, _, objective_id, action_id, intervention_id = _context(tmp_path)
    now = int(time.time())
    artifact_id = approval_artifacts.issue_for_intervention(
        conn,
        intervention_id=intervention_id,
        actor="human:advisor",
        evidence={"ticket": "APR-1", "expires_at": now + 300},
        now=now,
    )

    artifact = approval_artifacts.validate_for_action(
        conn,
        artifact_id=artifact_id,
        action_id=action_id,
        policy_version="charter-v1",
        now=now,
    )
    assert artifact["payload_sha256"]
    assert artifact["objective_scope_sha256"]
    assert artifact["approval_evidence_sha256"]
    with pytest.raises(
        approval_artifacts.ApprovalArtifactError,
        match="policy version",
    ):
        approval_artifacts.validate_for_action(
            conn,
            artifact_id=artifact_id,
            action_id=action_id,
            policy_version="charter-v2",
            now=now,
        )
    conn.execute(
        "UPDATE objectives SET max_spend_minor=999 WHERE id=?", (objective_id,)
    )
    conn.commit()
    with pytest.raises(
        approval_artifacts.ApprovalArtifactError,
        match="scope changed",
    ):
        approval_artifacts.validate_for_action(
            conn,
            artifact_id=artifact_id,
            action_id=action_id,
            policy_version="charter-v1",
            now=now,
        )
    with pytest.raises(Exception, match="contract is immutable"):
        conn.execute(
            """UPDATE approval_artifacts SET capability='admin'
               WHERE id=?""",
            (artifact_id,),
        )
    conn.close()


def test_exact_approval_materializes_one_permit_then_consumes_on_execution(tmp_path):
    conn, _, _, action_id, intervention_id = _context(tmp_path)
    artifact_id = approval_artifacts.issue_for_intervention(
        conn,
        intervention_id=intervention_id,
        actor="human:advisor",
        evidence={"ticket": "APR-2"},
    )
    decision, permit_id = objective_policy.evaluate_and_record(
        conn,
        action_id,
        charter=_charter(),
        executor="employee:ceo",
        policy_version="charter-v1",
        approval_artifact_id=artifact_id,
    )
    assert decision.verdict == "permit"
    artifact = conn.execute(
        "SELECT * FROM approval_artifacts WHERE id=?", (artifact_id,)
    ).fetchone()
    assert artifact["status"] == "materialized"
    assert artifact["consumed_by_permit_id"] == permit_id
    permit = conn.execute("SELECT * FROM permits WHERE id=?", (permit_id,)).fetchone()
    assert permit["approval_artifact_id"] == artifact_id

    payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM candidate_actions WHERE id=?", (action_id,)
        ).fetchone()["payload_json"]
    )
    db.consume_permit(
        conn,
        permit_id,
        action_id=action_id,
        payload=payload,
        executor="employee:ceo",
    )
    assert conn.execute(
        "SELECT status FROM approval_artifacts WHERE id=?", (artifact_id,)
    ).fetchone()["status"] == "consumed"
    with pytest.raises(
        approval_artifacts.ApprovalArtifactError,
        match="already used",
    ):
        approval_artifacts.validate_for_action(
            conn,
            artifact_id=artifact_id,
            action_id=action_id,
            policy_version="charter-v1",
        )
    conn.close()


def test_materialized_approval_can_be_revoked_before_execution(tmp_path):
    conn, _, _, action_id, intervention_id = _context(tmp_path)
    artifact_id = approval_artifacts.issue_for_intervention(
        conn,
        intervention_id=intervention_id,
        actor="human:advisor",
        evidence={"ticket": "APR-3"},
    )
    _, permit_id = objective_policy.evaluate_and_record(
        conn,
        action_id,
        charter=_charter(),
        executor="employee:ceo",
        policy_version="charter-v1",
        approval_artifact_id=artifact_id,
    )
    approval_artifacts.revoke(
        conn,
        artifact_id=artifact_id,
        actor="human:advisor",
        reason="customer state changed",
    )
    assert conn.execute(
        "SELECT status FROM approval_artifacts WHERE id=?", (artifact_id,)
    ).fetchone()["status"] == "revoked"
    assert conn.execute(
        "SELECT revoked_at FROM permits WHERE id=?", (permit_id,)
    ).fetchone()["revoked_at"] is not None
    with pytest.raises(db.PermitError, match="revoked"):
        db.consume_permit(
            conn,
            permit_id,
            action_id=action_id,
            payload={
                "system": "crm",
                "target_resource": "customer:1",
                "idempotency_key": "approval-action-0001",
            },
            executor="employee:ceo",
        )
    conn.close()


def test_master_pause_revokes_all_unexecuted_approval_authority(tmp_path):
    conn, _, _, action_id, intervention_id = _context(tmp_path)
    artifact_id = approval_artifacts.issue_for_intervention(
        conn,
        intervention_id=intervention_id,
        actor="human:advisor",
        evidence={"ticket": "APR-KILL"},
    )

    operational_control.set_autonomy_mode(
        conn,
        mode="paused",
        actor="human:operator",
        reason="master kill switch",
    )

    artifact = conn.execute(
        "SELECT * FROM approval_artifacts WHERE id=?", (artifact_id,)
    ).fetchone()
    assert artifact["status"] == "revoked"
    assert artifact["revoked_by"] == "human:operator"
    assert conn.execute(
        "SELECT status FROM candidate_actions WHERE id=?", (action_id,)
    ).fetchone()["status"] == "expired"
    conn.close()


def test_non_eligible_authority_gap_cannot_be_approved_once(tmp_path):
    conn, organization_id, objective_id, action_id, first_intervention = _context(
        tmp_path
    )
    operational_control.resolve_intervention(
        conn,
        first_intervention,
        option_id="replan",
        actor="human:advisor",
        evidence={"reason": "test non-eligible path"},
        organization_id=organization_id,
    )
    intervention_id = operational_control.raise_intervention(
        conn,
        organization_id=organization_id,
        objective_id=objective_id,
        action_id=action_id,
        category="authority_insufficient",
        summary="capability outside charter",
        context={"policy_version": "charter-v1", "approval_eligible": False},
        options=[{"id": "change_charter", "label": "Change charter"}],
        dedupe_key="non-eligible-gap",
    )
    with pytest.raises(
        approval_artifacts.ApprovalArtifactError,
        match="policy change",
    ):
        approval_artifacts.issue_for_intervention(
            conn,
            intervention_id=intervention_id,
            actor="human:advisor",
            evidence={"ticket": "APR-4"},
        )
    conn.close()


def test_unused_approval_expiry_terminates_action_and_wakes_replanning(tmp_path):
    conn, _, objective_id, action_id, intervention_id = _context(tmp_path)
    artifact_id = approval_artifacts.issue_for_intervention(
        conn,
        intervention_id=intervention_id,
        actor="human:advisor",
        evidence={"ticket": "APR-EXP", "expires_at": 1100},
        now=1000,
    )

    expired = approval_artifacts.expire_due(conn, now=1101)

    assert expired == [artifact_id]
    assert conn.execute(
        "SELECT status FROM approval_artifacts WHERE id=?", (artifact_id,)
    ).fetchone()["status"] == "expired"
    assert conn.execute(
        "SELECT status FROM candidate_actions WHERE id=?", (action_id,)
    ).fetchone()["status"] == "expired"
    event = conn.execute(
        """SELECT * FROM objective_inbox
           WHERE objective_id=? AND event_type='approval.expired'""",
        (objective_id,),
    ).fetchone()
    assert event["priority_class"] == "high"
    conn.close()


def test_stale_state_and_cross_action_replay_are_rejected(tmp_path):
    conn, organization_id, objective_id, original_action_id, intervention_id = (
        _context(tmp_path)
    )
    artifact_id = approval_artifacts.issue_for_intervention(
        conn,
        intervention_id=intervention_id,
        actor="human:advisor",
        evidence={"ticket": "APR-BOUND"},
    )
    plan_id = db.create_plan(
        conn,
        objective_id,
        assumptions=[],
        tasks=[],
        dependencies=[],
        risks=[],
        created_by="planner",
    )
    other_action_id = db.propose_action(
        conn,
        objective_id=objective_id,
        plan_id=plan_id,
        action_type="crm.update",
        payload={
            "system": "crm",
            "target_resource": "customer:2",
            "idempotency_key": "approval-other-action-0001",
        },
        expected_outcome="other customer updated",
        required_capability="crm.write",
        verification_method="crm.readback",
        risk_class="high",
        reversible=False,
        proposed_by="planner",
    )
    with pytest.raises(
        approval_artifacts.ApprovalArtifactError,
        match="action or policy version",
    ):
        approval_artifacts.validate_for_action(
            conn,
            artifact_id=artifact_id,
            action_id=other_action_id,
            policy_version="charter-v1",
        )

    stale_intervention = operational_control.raise_intervention(
        conn,
        organization_id=organization_id,
        objective_id=objective_id,
        action_id=other_action_id,
        category="authority_insufficient",
        summary="high risk requires approval",
        context={"policy_version": "charter-v1", "approval_eligible": True},
        options=[{"id": "approve_exact_action", "label": "Approve exact action"}],
    )
    conn.execute("DROP TRIGGER candidate_actions_contract_immutable")
    stale_payload = {
        "system": "crm",
        "target_resource": "customer:2",
        "idempotency_key": "approval-other-action-0001",
        "observed_state_at": 1000,
        "max_state_age_seconds": 50,
        "state_evidence": {"source": "crm", "reference": "customer:2:v1"},
    }
    conn.execute(
        """UPDATE candidate_actions SET payload_json=?,payload_sha256=?
           WHERE id=?""",
        (
            json.dumps(stale_payload, separators=(",", ":"), sort_keys=True),
            db.payload_sha256(stale_payload),
            other_action_id,
        ),
    )
    conn.commit()
    with pytest.raises(
        approval_artifacts.ApprovalArtifactError,
        match="state evidence expired",
    ):
        approval_artifacts.issue_for_intervention(
            conn,
            intervention_id=stale_intervention,
            actor="human:advisor",
            evidence={"ticket": "APR-STALE"},
            now=1060,
        )
    assert original_action_id != other_action_id
    conn.close()


def test_runtime_resumes_original_action_without_replanning_after_exact_approval(
    tmp_path,
):
    conn = db.connect(tmp_path / "authority.db")
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Approval Runtime Company",
        purpose="Execute only the reviewed effect",
        profile_name="default",
        charter={},
    )
    objective = db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Update the approved CRM record",
        originator="test",
        permitted_systems=["crm"],
    )
    db.transition_objective(conn, objective.id, "accepted", actor="test")
    db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="crm.customer.changed",
        payload={"customer_id": "1"},
        dedupe_key="approval-runtime-start",
    )

    class Planner:
        identity = "employee:ceo"

        def __init__(self):
            self.calls = 0

        def propose(self, snapshot, event):
            self.calls += 1
            return objective_runtime.PlanProposal(
                assumptions=[],
                tasks=[{"step": "update exact CRM record"}],
                dependencies=[],
                risks=["high impact"],
                actions=[
                    objective_runtime.ActionProposal(
                        action_type="crm.update",
                        payload={
                            "system": "crm",
                            "target_resource": "customer:1",
                            "idempotency_key": "approval-runtime-action-0001",
                        },
                        expected_outcome="customer record updated",
                        required_capability="crm.write",
                        verification_method="crm.readback",
                        risk_class="high",
                        reversible=False,
                    )
                ],
            )

    class Executor:
        identity = "employee:ceo"

        def __init__(self):
            self.calls = []

        def execute(self, action_type, payload):
            self.calls.append((action_type, dict(payload)))
            return objective_runtime.ExecutionOutcome(
                "succeeded",
                {"updated": True},
                external_reference="crm:customer:1:v2",
            )

    class Verifier:
        identity = "control:verification"

        def verify(self, action, execution):
            return objective_runtime.VerificationOutcome(
                "pass",
                verification_evidence.build(
                    observer=self.identity,
                    source_kind="provider_readback",
                    source_reference=str(execution.external_reference),
                    facts={"updated": True},
                ),
            )

        def verify_objective(self, snapshot, plan, action_verifications):
            raise AssertionError("objective completion was not proposed")

    planner = Planner()
    executor = Executor()
    runtime = objective_runtime.ObjectiveRuntime(
        conn,
        planner=planner,
        executor=executor,
        verifier=Verifier(),
        charter=_charter(),
        policy_version="charter-v1",
        runtime_id="approval-runtime",
    )

    escalated = runtime.tick()
    assert escalated.status == "escalated"
    intervention = operational_control.list_interventions(
        conn, organization_id=organization_id
    )[0]
    assert intervention["context"]["approval_eligible"] is True
    assert {
        item["id"] for item in intervention["options"]
    } >= {"approve_exact_action", "change_charter", "replan"}
    original_action_id = str(intervention["action_id"])

    operational_control.resolve_intervention(
        conn,
        intervention["id"],
        option_id="approve_exact_action",
        actor="human:advisor",
        evidence={"ticket": "APR-RUNTIME"},
        organization_id=organization_id,
    )
    progressed = runtime.tick()

    assert progressed.status == "progressed", progressed
    assert progressed.action_ids == (original_action_id,)
    assert planner.calls == 1
    assert len(executor.calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM candidate_actions").fetchone()[0] == 1
    permit = conn.execute("SELECT * FROM permits").fetchone()
    assert permit["action_id"] == original_action_id
    assert permit["approval_artifact_id"]
    artifact = conn.execute("SELECT * FROM approval_artifacts").fetchone()
    assert artifact["status"] == "consumed"
    assert artifact["consumed_by_permit_id"] == permit["id"]
    approval_audit_events = [
        row["event_type"]
        for row in conn.execute(
            """SELECT event_type FROM business_audit_events
               WHERE organization_id=? AND event_type LIKE 'approval.%'
               ORDER BY sequence""",
            (organization_id,),
        ).fetchall()
    ]
    assert approval_audit_events == [
        "approval.issued",
        "approval.materialized",
        "approval.consumed",
    ]
    conn.close()
