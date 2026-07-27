import sqlite3

import pytest

from hermes_cli import (
    compensation,
    objective_runtime,
    objectives_db,
    verification_evidence,
)


def test_compensation_schema_read_preserves_active_transaction(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    compensation.ensure_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    compensation.ensure_schema(conn)
    assert conn.in_transaction is True
    conn.rollback()


class SagaPlanner:
    identity = "employee:ceo"

    def propose(self, snapshot, event):
        if event["event_type"] == "compensation.required":
            return objective_runtime.PlanProposal(
                assumptions=[],
                tasks=[{"step": "restore prior state"}],
                dependencies=[],
                risks=[],
                actions=[
                    objective_runtime.ActionProposal(
                        action_type="service.disable",
                        payload={
                            "system": "service",
                            "target_resource": "feature:1",
                            "idempotency_key": "rollback-feature-1",
                        },
                        expected_outcome="feature disabled",
                        required_capability="service.disable",
                        verification_method="service.readback",
                        risk_class="low",
                        reversible=False,
                    )
                ],
                objective_complete_when_verified=False,
            )
        return objective_runtime.PlanProposal(
            assumptions=[],
            tasks=[{"step": "enable feature"}, {"step": "measure outcome"}],
            dependencies=[],
            risks=[],
            actions=[
                objective_runtime.ActionProposal(
                    action_type="service.enable",
                    payload={
                        "system": "service",
                        "target_resource": "feature:1",
                        "idempotency_key": "enable-feature-1",
                    },
                    expected_outcome="feature enabled correctly",
                    required_capability="service.enable",
                    verification_method="service.readback",
                    risk_class="low",
                    reversible=True,
                    compensation={
                        "action_type": "service.disable",
                        "payload": {
                            "system": "service",
                            "target_resource": "feature:1",
                            "idempotency_key": "rollback-feature-1",
                        },
                        "required_capability": "service.disable",
                        "verification_method": "service.readback",
                    },
                )
            ],
            objective_complete_when_verified=False,
        )


class SagaExecutor:
    identity = "employee:operations"

    def __init__(self):
        self.enabled = False
        self.calls = []

    def execute(self, action_type, payload):
        self.calls.append(action_type)
        self.enabled = action_type == "service.enable"
        return objective_runtime.ExecutionOutcome(
            "succeeded", {"enabled": self.enabled},
            external_reference=f"service-revision:{len(self.calls)}",
        )


class SagaVerifier:
    identity = "control:service-readback"

    def __init__(self, executor):
        self.executor = executor

    def verify(self, action, execution):
        # The initial external mutation succeeds but violates the expected
        # state invariant; compensation independently verifies the restoration.
        passed = action.action_type == "service.disable" and not self.executor.enabled
        return objective_runtime.VerificationOutcome(
            "pass" if passed else "fail",
            verification_evidence.build(
                observer=self.identity,
                source_kind="provider_readback",
                source_reference=str(execution.external_reference),
                facts={"enabled": self.executor.enabled},
            ),
        )

    def verify_objective(self, snapshot, plan, action_verifications):
        return objective_runtime.VerificationOutcome(
            "inconclusive",
            verification_evidence.build(
                observer=self.identity,
                source_kind="deterministic_check",
                source_reference=f"objective:{snapshot['id']}",
                facts={"restored": not self.executor.enabled},
            ),
        )


def test_failed_verification_creates_exact_governed_compensation(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    objective = objectives_db.create_objective(
        conn,
        desired_outcome="Enable feature without invariant violation",
        originator="owner",
        permitted_systems=["service"],
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor="owner"
    )
    objectives_db.enqueue_objective_event(
        conn, objective_id=objective.id, event_type="objective.accepted", payload={}
    )
    charter = {
        "enabled": True,
        "operating_mode": "autonomous",
        "allowed_capabilities": ["service.enable", "service.disable"],
        "forbidden_capabilities": [],
        "allowed_systems": ["service"],
        "approval_required_capabilities": [],
        "max_autonomous_risk": "low",
        "allow_irreversible": True,
        "max_action_spend_minor": 0,
        "permit_ttl_seconds": 300,
        "security": {"require_compensation_for_reversible_actions": True},
    }
    executor = SagaExecutor()
    runtime = objective_runtime.ObjectiveRuntime(
        conn,
        planner=SagaPlanner(),
        executor=executor,
        verifier=SagaVerifier(executor),
        charter=charter,
        policy_version="v1",
        runtime_id="runtime:saga",
    )

    failed = runtime.tick()
    obligation = compensation.outstanding(conn, objective.id)
    assert failed.status == "blocked"
    assert executor.enabled is True
    assert obligation is not None

    restored = runtime.tick()
    assert restored.status == "progressed"
    assert executor.enabled is False
    assert executor.calls == ["service.enable", "service.disable"]
    assert compensation.outstanding(conn, objective.id) is None
    resolved = conn.execute(
        "SELECT * FROM compensation_obligations"
    ).fetchone()
    assert resolved["status"] == "verified"
    assert resolved["compensation_action_id"] is not None
    assert conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            """UPDATE compensation_obligations
               SET compensation_capability='system.admin' WHERE id=?""",
            (resolved["id"],),
        )
