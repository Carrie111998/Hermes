from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from hermes_cli import objective_runtime as runtime
from hermes_cli import objective_policy
from hermes_cli import objectives_db as db
from hermes_cli import operational_control
from hermes_cli import organization_db
from hermes_cli import verification_evidence


class Planner:
    identity = "employee:ceo"

    def __init__(self, actions):
        self.actions = actions

    def propose(self, snapshot, event):
        return runtime.PlanProposal(
            assumptions=[f"event:{event['event_type']}"],
            tasks=[{"step": "update CRM"}],
            dependencies=[],
            risks=["stale data"],
            actions=self.actions,
            objective_complete_when_verified=True,
        )


class Executor:
    identity = "employee:revenue-ops"

    def __init__(self, status="succeeded"):
        self.status = status
        self.calls = []

    def execute(self, action_type, payload):
        self.calls.append((action_type, payload))
        return runtime.ExecutionOutcome(
            status=self.status,
            result={"read_back": payload},
            external_reference="crm-event-123",
        )


class Verifier:
    identity = "employee:internal-audit"

    def __init__(self, verdict="pass"):
        self.verdict = verdict

    def verify(self, action, execution):
        return runtime.VerificationOutcome(
            verdict=self.verdict,
            evidence=verification_evidence.build(
                observer=self.identity,
                source_kind="authoritative_database_readback",
                source_reference=str(execution.external_reference),
                facts={"read_back": execution.result["read_back"]},
            ),
        )

    def verify_objective(self, snapshot, plan, action_verifications):
        return runtime.VerificationOutcome(
            verdict=self.verdict,
            evidence=verification_evidence.build(
                observer=self.identity,
                source_kind="deterministic_check",
                source_reference=f"objective:{snapshot['id']}",
                facts={
                    "success_criteria": snapshot["success_criteria"],
                    "action_verdicts": [
                        item.verdict for item in action_verifications
                    ],
                },
            ),
        )


def charter():
    return {
        "enabled": True,
        "operating_cadence": {"enabled": False},
        "operating_mode": "autonomous",
        "allowed_capabilities": ["crm.write"],
        "forbidden_capabilities": [],
        "allowed_systems": ["crm"],
        "approval_required_capabilities": [],
        "max_autonomous_risk": "medium",
        "allow_irreversible": False,
        "max_action_spend_minor": 100,
        "permit_ttl_seconds": 300,
    }


def action(capability="crm.write"):
    return runtime.ActionProposal(
        action_type="crm.update",
        payload={"system": "crm", "target_resource": "lead:123", "stage": "qualified"},
        expected_outcome="lead is qualified",
        required_capability=capability,
        verification_method="crm read-back",
        risk_class="low",
        reversible=True,
    )


def recovery_action():
    base = action()
    return runtime.ActionProposal(
        **{
            **base.__dict__,
            "payload": {
                **base.payload,
                "idempotency_key": "crm-lead-recovery-0001",
            },
        }
    )


def accepted_objective(conn):
    objective = db.create_objective(
        conn,
        desired_outcome="Keep qualified leads current",
        originator="setup:user",
        permitted_systems=["crm"],
        success_criteria=["CRM read-back matches expected stage"],
    )
    return db.transition_objective(
        conn, objective.id, "accepted", actor="setup:user"
    )


def organization_objective(conn):
    organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Continuity Company",
        purpose="Operate continuously",
        profile_name="continuity",
        charter={},
    )
    objective = db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Prove the first operating milestone",
        originator="setup:user",
        permitted_systems=["crm", "objectives"],
        prohibited_actions=["company.delete"],
        max_spend_minor=1_000,
        currency="USD",
        expires_at=2_000_000_000,
        success_criteria=["milestone read-back passes"],
    )
    return organization_id, db.transition_objective(
        conn, objective.id, "accepted", actor="setup:user"
    )


def test_event_loop_executes_and_independently_verifies(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    objective = accepted_objective(conn)
    event_id = db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="crm.lead.changed",
        payload={"lead_id": "123"},
        dedupe_key="crm:lead:123:v2",
    )
    executor = Executor()
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=Planner([action()]),
        executor=executor,
        verifier=Verifier(),
        charter=charter(),
        policy_version="charter-v1",
        runtime_id="runtime-test",
    )

    outcome = loop.tick()

    assert outcome.event_id == event_id
    assert outcome.status == "verified"
    assert db.get_objective(conn, objective.id).status == "verified"
    assert len(executor.calls) == 1
    snapshot = db.objective_snapshot(conn, objective.id)
    assert snapshot["verifications"][0]["verifier"] == "employee:internal-audit"
    assert loop.tick().status == "idle"


def test_runtime_rejects_self_supervising_verifier(tmp_path):
    conn = db.connect(tmp_path / "authority.db")

    class SelfVerifier(Verifier):
        identity = "employee:ceo"

    with pytest.raises(ValueError, match="must differ"):
        runtime.ObjectiveRuntime(
            conn,
            planner=Planner([]),
            executor=Executor(),
            verifier=SelfVerifier(),
            charter=charter(),
            policy_version="charter-v1",
            runtime_id="runtime-self-supervision",
        )
    conn.close()


def test_runtime_renews_event_and_resource_ownership_during_slow_effect(tmp_path):
    path = tmp_path / "authority.db"
    conn = db.connect(path)
    objective = accepted_objective(conn)
    db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="crm.lead.changed",
        payload={"lead_id": "123"},
        dedupe_key="crm:slow-effect",
    )

    class SlowExecutor(Executor):
        def execute(self, action_type, payload):
            time.sleep(3.2)
            contender = db.connect(path)
            try:
                assert db.claim_objective_event(
                    contender,
                    runtime_id="runtime-contender",
                    claim_ttl_seconds=3,
                ) is None
                with pytest.raises(operational_control.ResourceConflictError):
                    operational_control.acquire_resource_lease(
                        contender,
                        resource_key="lead:123",
                        owner="runtime-contender",
                        action_id="competing-action",
                        ttl_seconds=3,
                    )
            finally:
                contender.close()
            return super().execute(action_type, payload)

    bounded_charter = {
        **charter(),
        "event_claim_ttl_seconds": 3,
        "resource_lease_ttl_seconds": 3,
    }
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=Planner([action()]),
        executor=SlowExecutor(),
        verifier=Verifier(),
        charter=bounded_charter,
        policy_version="charter-v1",
        runtime_id="runtime-owner",
    )

    assert loop.tick().status == "verified"


@pytest.mark.parametrize("crash_after_effect", [False, True])
def test_interrupted_consumed_permit_replays_only_exact_idempotent_action(
    tmp_path, crash_after_effect
):
    db_path = tmp_path / "authority.db"
    conn = db.connect(db_path)
    objective = accepted_objective(conn)
    db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="crm.lead.changed",
        payload={"lead_id": "123"},
        dedupe_key=f"crm:crash:{crash_after_effect}",
    )

    class CountingPlanner(Planner):
        def __init__(self):
            super().__init__([recovery_action()])
            self.calls = 0

        def propose(self, snapshot, event):
            self.calls += 1
            proposal = super().propose(snapshot, event)
            return runtime.PlanProposal(
                **{
                    **proposal.__dict__,
                    "objective_complete_when_verified": False,
                }
            )

    class CrashOnceExecutor(Executor):
        def __init__(self):
            super().__init__()
            self.effects = set()
            self.crashed = False

        def execute(self, action_type, payload):
            self.calls.append((action_type, payload))
            key = payload["idempotency_key"]
            if not self.crashed:
                self.crashed = True
                if crash_after_effect:
                    self.effects.add(key)
                raise RuntimeError("simulated worker crash")
            self.effects.add(key)
            return runtime.ExecutionOutcome(
                "succeeded",
                {"read_back": payload},
                external_reference="crm-event-recovered",
            )

    planner = CountingPlanner()
    executor = CrashOnceExecutor()
    bounded = {
        **charter(),
        "retry_policy": {
            "max_attempts": 3,
            "base_backoff_seconds": 0,
            "max_backoff_seconds": 0,
        },
    }
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=planner,
        executor=executor,
        verifier=Verifier(),
        charter=bounded,
        policy_version="charter-v1",
        runtime_id="runtime-recovery",
    )

    assert loop.tick().status == "retry_scheduled"
    assert db.get_objective(conn, objective.id).status == "executing"
    posture = db.in_doubt_executions(conn, objective.organization_id)
    assert len(posture) == 1
    assert posture[0]["recovery_stage"] == "effect_outcome_unknown"
    assert posture[0]["has_replay_idempotency_key"] is True
    # Model a real process/container restart: the next runtime gets a new
    # authority connection and must recover from the durable permit/action
    # state rather than relying on the crashed worker's memory.
    conn.close()
    conn = db.connect(db_path)
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=planner,
        executor=executor,
        verifier=Verifier(),
        charter=bounded,
        policy_version="charter-v1",
        runtime_id="runtime-restarted",
    )
    assert loop.tick().status == "reconciliation_pending"
    posture = db.in_doubt_executions(conn, objective.organization_id)
    assert len(posture) == 1
    assert posture[0]["recovery_stage"] == "result_requires_verification"
    assert loop.tick().status == "recovered"
    assert planner.calls == 1
    assert len(executor.calls) == 2
    assert executor.effects == {"crm-lead-recovery-0001"}
    snapshot = db.objective_snapshot(conn, objective.id)
    assert len(snapshot["execution_results"]) == 1
    assert len(snapshot["verifications"]) == 1


def test_crash_after_result_resumes_at_verification_without_reexecution(
    tmp_path, monkeypatch
):
    conn = db.connect(tmp_path / "authority.db")
    objective = accepted_objective(conn)
    db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="crm.lead.changed",
        payload={"lead_id": "123"},
        dedupe_key="crm:crash-after-result",
    )
    planner = Planner([recovery_action()])
    executor = Executor()
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=planner,
        executor=executor,
        verifier=Verifier(),
        charter={
            **charter(),
            "retry_policy": {
                "max_attempts": 3,
                "base_backoff_seconds": 0,
                "max_backoff_seconds": 0,
            },
        },
        policy_version="charter-v1",
        runtime_id="runtime-post-result",
    )
    original = db.record_verification
    crashed = False

    def crash_once(*args, **kwargs):
        nonlocal crashed
        if kwargs.get("action_id") and not crashed:
            crashed = True
            raise RuntimeError("crash after durable result")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "record_verification", crash_once)
    assert loop.tick().status == "retry_scheduled"
    assert len(executor.calls) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM execution_results"
    ).fetchone()[0] == 1
    posture = db.in_doubt_executions(conn, objective.organization_id)
    assert len(posture) == 1
    assert posture[0]["recovery_stage"] == "result_requires_verification"
    assert loop.tick().status == "recovered"
    assert len(executor.calls) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM verification_records"
    ).fetchone()[0] == 1


def test_interrupted_action_without_idempotency_blocks_for_readback(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    objective = accepted_objective(conn)
    db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="crm.lead.changed",
        payload={"lead_id": "123"},
        dedupe_key="crm:no-replay-authority",
    )

    class CrashExecutor(Executor):
        def execute(self, action_type, payload):
            self.calls.append((action_type, payload))
            raise RuntimeError("unknown external outcome")

    executor = CrashExecutor()
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=Planner([action()]),
        executor=executor,
        verifier=Verifier(),
        charter={
            **charter(),
            "retry_policy": {
                "max_attempts": 3,
                "base_backoff_seconds": 0,
                "max_backoff_seconds": 0,
            },
        },
        policy_version="charter-v1",
        runtime_id="runtime-no-idempotency",
    )

    assert loop.tick().status == "retry_scheduled"
    recovered = loop.tick()
    assert recovered.status == "blocked"
    assert "idempotency" in recovered.reason
    assert len(executor.calls) == 1
    intervention = operational_control.list_interventions(conn)[0]
    assert intervention["category"] == "execution_in_doubt"
    assert {
        option["id"] for option in intervention["options"]
    } == {"provider_readback", "compensate", "abandon"}


def test_pre_call_crash_resumes_existing_permit_without_replanning(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    objective = accepted_objective(conn)
    plan_id = db.create_plan(
        conn,
        objective.id,
        assumptions=[],
        tasks=[{"step": "update CRM"}],
        dependencies=[],
        risks=[],
        created_by="employee:ceo",
    )
    db.transition_objective(conn, objective.id, "planned", actor="runtime-old")
    proposal = recovery_action()
    action_id = db.propose_action(
        conn,
        objective_id=objective.id,
        plan_id=plan_id,
        action_type=proposal.action_type,
        payload=proposal.payload,
        expected_outcome=proposal.expected_outcome,
        required_capability=proposal.required_capability,
        verification_method=proposal.verification_method,
        risk_class=proposal.risk_class,
        reversible=proposal.reversible,
        proposed_by="employee:ceo",
    )
    _, permit_id = objective_policy.evaluate_and_record(
        conn,
        action_id,
        charter=charter(),
        executor=Executor.identity,
        policy_version="charter-v1",
    )
    db.transition_objective(
        conn, objective.id, "authorized", actor="runtime-old"
    )
    db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="runtime.recovered",
        payload={},
        dedupe_key="runtime:pre-call-recovery",
    )
    posture = db.in_doubt_executions(conn, objective.organization_id)
    assert len(posture) == 1
    assert posture[0]["recovery_stage"] == "permit_issued_pre_call"
    assert posture[0]["permit_id"] == permit_id

    class NoPlanner:
        identity = "employee:ceo"

        def propose(self, snapshot, event):
            raise AssertionError("planner must not run during permit recovery")

    executor = Executor()
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=NoPlanner(),
        executor=executor,
        verifier=Verifier(),
        charter=charter(),
        policy_version="charter-v1",
        runtime_id="runtime-new",
    )
    assert loop.tick().status == "reconciliation_pending"
    consumed = conn.execute(
        "SELECT consumed_at FROM permits WHERE id=?", (permit_id,)
    ).fetchone()
    assert consumed["consumed_at"] is not None
    assert len(executor.calls) == 1
    assert loop.tick().status == "recovered"
    assert len(executor.calls) == 1


def test_crash_after_verification_commits_recovery_without_reverification(
    tmp_path, monkeypatch
):
    conn = db.connect(tmp_path / "authority.db")
    objective = accepted_objective(conn)
    db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="crm.lead.changed",
        payload={"lead_id": "123"},
        dedupe_key="crm:crash-after-verification",
    )

    class ContinuingPlanner(Planner):
        def propose(self, snapshot, event):
            proposal = super().propose(snapshot, event)
            return runtime.PlanProposal(
                **{
                    **proposal.__dict__,
                    "objective_complete_when_verified": False,
                }
            )

    class CountingVerifier(Verifier):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def verify(self, action, execution):
            self.calls += 1
            return super().verify(action, execution)

    executor = Executor()
    verifier = CountingVerifier()
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=ContinuingPlanner([recovery_action()]),
        executor=executor,
        verifier=verifier,
        charter={
            **charter(),
            "retry_policy": {
                "max_attempts": 3,
                "base_backoff_seconds": 0,
                "max_backoff_seconds": 0,
            },
        },
        policy_version="charter-v1",
        runtime_id="runtime-post-verification",
    )
    original_transition = db.transition_objective
    crashed = False

    def crash_before_planned(conn_arg, objective_id, new_status, **kwargs):
        nonlocal crashed
        if (
            new_status == "planned"
            and not crashed
            and db.get_objective(conn_arg, objective_id).status == "executing"
        ):
            crashed = True
            raise RuntimeError("crash after durable verification")
        return original_transition(
            conn_arg, objective_id, new_status, **kwargs
        )

    monkeypatch.setattr(db, "transition_objective", crash_before_planned)
    assert loop.tick().status == "retry_scheduled"
    assert verifier.calls == 1
    posture = db.in_doubt_executions(conn, objective.organization_id)
    assert len(posture) == 1
    assert posture[0]["recovery_stage"] == (
        "verification_requires_finalization"
    )
    assert loop.tick().status == "recovered"
    assert verifier.calls == 1
    assert len(executor.calls) == 1


def test_runtime_claims_only_active_ceo_organization_events(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    active_organization_id, _ = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Active Company",
        purpose="Operate the active company",
        profile_name="default",
        charter={"allowed_capabilities": ["crm.write"], "allowed_systems": ["crm"]},
    )
    foreign_organization_id = organization_db.create_organization(
        conn,
        name="Foreign Company",
        purpose="Remain isolated",
    )

    def create_accepted(organization_id, outcome):
        objective = db.create_objective(
            conn,
            organization_id=organization_id,
            desired_outcome=outcome,
            originator="setup:user",
            permitted_systems=["crm"],
            success_criteria=["CRM read-back matches expected stage"],
        )
        return db.transition_objective(
            conn, objective.id, "accepted", actor="setup:user"
        )

    foreign = create_accepted(foreign_organization_id, "Foreign objective")
    active = create_accepted(active_organization_id, "Active objective")
    foreign_event_id = db.enqueue_objective_event(
        conn,
        objective_id=foreign.id,
        event_type="crm.lead.changed",
        payload={"tenant": "foreign"},
        available_at=1,
    )
    active_event_id = db.enqueue_objective_event(
        conn,
        objective_id=active.id,
        event_type="crm.lead.changed",
        payload={"tenant": "active"},
        available_at=2,
    )
    executor = Executor()
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=Planner([action()]),
        executor=executor,
        verifier=Verifier(),
        charter=charter(),
        policy_version="charter-v1",
        runtime_id="runtime-active-company",
    )

    outcome = loop.tick()

    assert outcome.event_id == active_event_id
    assert outcome.objective_id == active.id
    assert outcome.status == "verified"
    foreign_event = conn.execute(
        "SELECT status,attempts,claimed_by FROM objective_inbox WHERE id=?",
        (foreign_event_id,),
    ).fetchone()
    assert tuple(foreign_event) == ("pending", 0, None)
    assert db.get_objective(conn, foreign.id).status == "accepted"
    assert len(executor.calls) == 1
    conn.close()


def test_out_of_charter_action_escalates_without_execution(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    objective = accepted_objective(conn)
    db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="cash.transfer.requested",
        payload={},
    )
    executor = Executor()
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=Planner([action(capability="bank.transfer")]),
        executor=executor,
        verifier=Verifier(),
        charter=charter(),
        policy_version="charter-v1",
        runtime_id="runtime-test",
    )

    outcome = loop.tick()

    assert outcome.status == "escalated"
    assert db.get_objective(conn, objective.id).status == "blocked"
    assert executor.calls == []
    interventions = operational_control.list_interventions(conn)
    assert len(interventions) == 1
    assert interventions[0]["category"] == "authority_insufficient"
    assert interventions[0]["action_id"] == outcome.action_ids[0]
    assert {item["id"] for item in interventions[0]["options"]} == {
        "change_charter",
        "replan",
    }
    conn.close()


def test_execution_failure_enqueues_replanning_event(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    objective = accepted_objective(conn)
    db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="crm.lead.changed",
        payload={},
    )
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=Planner([action()]),
        executor=Executor(status="failed"),
        verifier=Verifier(),
        charter=charter(),
        policy_version="charter-v1",
        runtime_id="runtime-test",
    )

    outcome = loop.tick()

    assert outcome.status == "blocked"
    pending = conn.execute(
        """
        SELECT event_type, status FROM objective_inbox
         WHERE event_type = 'execution.failed'
        """
    ).fetchone()
    assert tuple(pending) == ("execution.failed", "pending")
    interventions = operational_control.list_interventions(conn)
    assert len(interventions) == 1
    assert interventions[0]["category"] == "execution_failed"
    assert interventions[0]["context"]["execution_result_id"]
    conn.close()


def test_event_dedupe_and_stale_claim_recovery(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "authority.db")
    objective = accepted_objective(conn)
    first = db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="metric.changed",
        payload={"v": 1},
        dedupe_key="metric:revenue:2026-07",
    )
    second = db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="metric.changed",
        payload={"v": 1},
        dedupe_key="metric:revenue:2026-07",
    )
    assert first == second
    with pytest.raises(ValueError, match="different semantics"):
        db.enqueue_objective_event(
            conn,
            objective_id=objective.id,
            event_type="metric.changed",
            payload={"v": 2},
            dedupe_key="metric:revenue:2026-07",
        )
    claimed = db.claim_objective_event(
        conn, runtime_id="dead-runtime", claim_ttl_seconds=1
    )
    conn.execute(
        "UPDATE objective_inbox SET claim_expires = 0 WHERE id = ?", (claimed["id"],)
    )
    conn.commit()
    recovered = db.claim_objective_event(
        conn, runtime_id="live-runtime", claim_ttl_seconds=30
    )
    assert recovered["id"] == first
    assert recovered["attempts"] == 2
    conn.close()


def test_verified_intermediate_action_replans_without_closing_objective(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    objective = accepted_objective(conn)
    db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="crm.lead.changed",
        payload={},
    )
    planner = Planner([action()])

    original_propose = planner.propose

    def nonterminal(snapshot, event):
        proposal = original_propose(snapshot, event)
        return runtime.PlanProposal(
            assumptions=proposal.assumptions,
            tasks=proposal.tasks,
            dependencies=proposal.dependencies,
            risks=proposal.risks,
            actions=proposal.actions,
            objective_complete_when_verified=False,
        )

    planner.propose = nonterminal
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=planner,
        executor=Executor(),
        verifier=Verifier(),
        charter=charter(),
        policy_version="charter-v1",
        runtime_id="runtime-test",
    )

    outcome = loop.tick()

    assert outcome.status == "progressed"
    assert db.get_objective(conn, objective.id).status == "planned"
    followup = conn.execute(
        "SELECT status FROM objective_inbox WHERE event_type = 'cycle.actions_verified'"
    ).fetchone()
    assert followup["status"] == "pending"
    conn.close()


def test_no_action_terminal_plan_still_requires_objective_verifier(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    objective = accepted_objective(conn)
    db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="external.state.changed",
        payload={},
    )
    planner = Planner([])

    def terminal(snapshot, event):
        return runtime.PlanProposal(
            assumptions=[],
            tasks=[],
            dependencies=[],
            risks=[],
            actions=[],
            objective_complete_when_verified=True,
        )

    planner.propose = terminal
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=planner,
        executor=Executor(),
        verifier=Verifier(verdict="pass"),
        charter=charter(),
        policy_version="charter-v1",
        runtime_id="runtime-test",
    )
    outcome = loop.tick()
    assert outcome.status == "verified"
    assert db.get_objective(conn, objective.id).status == "verified"
    conn.close()


def test_final_business_root_stays_active_until_successor_exists(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    _, objective = organization_objective(conn)
    db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="milestone.changed",
        payload={},
    )
    governed_charter = {
        **charter(),
        "operating_cadence": {"enabled": True},
        "allowed_capabilities": ["crm.write", "objectives.create"],
        "allowed_systems": ["crm", "objectives"],
    }
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=Planner([action()]),
        executor=Executor(),
        verifier=Verifier(),
        charter=governed_charter,
        policy_version="charter-v1",
        runtime_id="runtime-continuity",
    )

    outcome = loop.tick()

    assert outcome.status == "continuity_required"
    assert db.get_objective(conn, objective.id).status == "planned"
    event = conn.execute(
        """SELECT status,payload_json FROM objective_inbox
            WHERE event_type='objective.successor.required'"""
    ).fetchone()
    assert event["status"] == "pending"
    assert '"objectives.create_successor"' in event["payload_json"]
    assert operational_control.list_interventions(conn) == []


def test_final_root_without_successor_authority_escalates_instead_of_closing(
    tmp_path,
):
    conn = db.connect(tmp_path / "authority.db")
    _, objective = organization_objective(conn)
    db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="milestone.changed",
        payload={},
    )
    governed_charter = {
        **charter(),
        "operating_cadence": {"enabled": True},
    }
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=Planner([action()]),
        executor=Executor(),
        verifier=Verifier(),
        charter=governed_charter,
        policy_version="charter-v1",
        runtime_id="runtime-continuity-no-authority",
    )

    outcome = loop.tick()

    assert outcome.status == "escalated"
    assert db.get_objective(conn, objective.id).status == "blocked"
    handoff = operational_control.list_interventions(conn)[0]
    assert handoff["category"] == "business_continuity_authority_insufficient"
    assert handoff["context"]["required_capability"] == "objectives.create"


def test_no_admissible_action_creates_one_deduplicated_advisor_handoff(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    objective = accepted_objective(conn)
    planner = Planner([])

    def nonterminal(snapshot, event):
        return runtime.PlanProposal(
            assumptions=[],
            tasks=[{"step": "requires advisor input"}],
            dependencies=[],
            risks=[],
            actions=[],
            objective_complete_when_verified=False,
        )

    planner.propose = nonterminal
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=planner,
        executor=Executor(),
        verifier=Verifier(),
        charter=charter(),
        policy_version="charter-v1",
        runtime_id="runtime-test",
    )
    for version in (1, 2):
        db.enqueue_objective_event(
            conn,
            objective_id=objective.id,
            event_type="advisor.input.required",
            payload={"version": version},
            dedupe_key=f"advisor-input:{version}",
        )
        outcome = loop.tick()
        assert outcome.status == "escalated"

    interventions = operational_control.list_interventions(conn)
    assert len(interventions) == 1
    assert interventions[0]["category"] == "no_admissible_action"
    assert interventions[0]["objective_id"] == objective.id
    assert interventions[0]["context"]["tasks"] == [
        {"step": "requires advisor input"}
    ]

    operational_control.resolve_intervention(
        conn,
        interventions[0]["id"],
        option_id="advise",
        actor="human:advisor",
        evidence={"guidance": "Prioritize the oldest qualified lead"},
    )
    wake = conn.execute(
        """SELECT event_type,payload_json,status FROM objective_inbox
           WHERE dedupe_key=?""",
        (f"intervention-resolution:{interventions[0]['id']}",),
    ).fetchone()
    assert wake["event_type"] == "intervention.resolved"
    assert wake["status"] == "pending"
    assert "Prioritize the oldest qualified lead" in wake["payload_json"]
    conn.close()


def test_proposed_objective_creates_acceptance_handoff(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    objective = db.create_objective(
        conn,
        desired_outcome="Accept only with explicit evidence",
        originator="external:advisor",
    )
    event_id = db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="objective.proposed",
        payload={"source": "advisor"},
    )
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=Planner([]),
        executor=Executor(),
        verifier=Verifier(),
        charter=charter(),
        policy_version="charter-v1",
        runtime_id="runtime-proposed-acceptance",
    )

    outcome = loop.tick()

    assert outcome.event_id == event_id
    assert outcome.status == "escalated"
    handoffs = operational_control.list_interventions(conn)
    assert len(handoffs) == 1
    assert handoffs[0]["category"] == "objective_acceptance_required"
    assert db.get_objective(conn, objective.id).status == "proposed"

    operational_control.resolve_intervention(
        conn,
        handoffs[0]["id"],
        option_id="accept",
        actor="human:advisor",
        evidence={"accepted_scope": "initial operating portfolio"},
    )
    assert db.get_objective(conn, objective.id).status == "accepted"
    wake = conn.execute(
        "SELECT event_type,status FROM objective_inbox "
        "WHERE objective_id=? AND event_type='objective.accepted'",
        (objective.id,),
    ).fetchone()
    assert tuple(wake) == ("objective.accepted", "pending")


def test_ceo_originated_objective_is_accepted_without_advisor_dispatch(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    organization_id, ceo_id = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Autonomous Company",
        purpose="Operate without routine human dispatch",
        profile_name="default",
        charter=charter(),
    )
    objective = db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Advance the next CEO-owned operating milestone",
        originator=f"employee:{ceo_id}",
        permitted_systems=["crm"],
        success_criteria=["CRM read-back matches expected stage"],
    )
    db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="objective.proposed",
        payload={"source": "ceo"},
    )
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=Planner([]),
        executor=Executor(),
        verifier=Verifier(),
        charter=charter(),
        policy_version="charter-v1",
        runtime_id="runtime-ceo-acceptance",
    )

    outcome = loop.tick()

    assert outcome.status != "escalated"
    assert db.get_objective(conn, objective.id).status != "proposed"
    assert not [
        item for item in operational_control.list_interventions(conn)
        if item["category"] == "objective_acceptance_required"
    ]
    accepted = conn.execute(
        """SELECT actor,payload_json FROM objective_events
           WHERE objective_id=? AND kind='transitioned' AND next_status='accepted'""",
        (objective.id,),
    ).fetchone()
    assert accepted is not None
    assert accepted["actor"] == "runtime-ceo-acceptance"
    assert "standing organizational authority" in accepted["payload_json"]


def test_objective_admission_rejects_unknown_enterprise_organization(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Tenant Company",
        purpose="Enforce tenant binding",
        profile_name="default",
        charter=charter(),
    )
    with pytest.raises(ValueError, match="organization is not configured"):
        db.create_objective(
            conn,
            organization_id="org-not-configured",
            desired_outcome="Must not cross tenant boundaries",
            originator="external:test",
        )


def test_stale_objective_intent_blocks_until_reaffirmed(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    objective = accepted_objective(conn)
    conn.execute(
        "UPDATE objectives SET reaffirmed_at=1 WHERE id=?", (objective.id,)
    )
    conn.commit()
    event_id = db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="objective.cadence",
        payload={"source": "scheduler"},
    )
    governed = {**charter(), "reaffirmation_ttl_seconds": 10}
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=Planner([]),
        executor=Executor(),
        verifier=Verifier(),
        charter=governed,
        policy_version="charter-v1",
        runtime_id="runtime-stale-intent",
    )

    outcome = loop.tick()

    assert outcome.event_id == event_id
    assert outcome.status == "escalated"
    assert db.get_objective(conn, objective.id).status == "blocked"
    handoff = operational_control.list_interventions(conn)[0]
    assert handoff["category"] == "objective_reaffirmation_required"
    with pytest.raises(ValueError, match="substantive decision basis"):
        operational_control.resolve_intervention(
            conn,
            handoff["id"],
            option_id="reaffirm",
            actor="human:advisor",
            evidence={"reason": "ok"},
        )
    operational_control.resolve_intervention(
        conn,
        handoff["id"],
        option_id="reaffirm",
        actor="human:advisor",
        evidence={"reason": "quarterly strategy still applies"},
    )
    assert db.get_objective(conn, objective.id).status == "planned"
    wake = conn.execute(
        "SELECT status FROM objective_inbox "
        "WHERE objective_id=? AND event_type='objective.reaffirmed'",
        (objective.id,),
    ).fetchone()
    assert wake["status"] == "pending"

def test_inconclusive_action_verification_creates_evidence_handoff(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    objective = accepted_objective(conn)
    db.enqueue_objective_event(
        conn,
        objective_id=objective.id,
        event_type="crm.lead.changed",
        payload={},
    )
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=Planner([action()]),
        executor=Executor(),
        verifier=Verifier(verdict="inconclusive"),
        charter=charter(),
        policy_version="charter-v1",
        runtime_id="runtime-test",
    )

    outcome = loop.tick()

    assert outcome.status == "blocked"
    interventions = operational_control.list_interventions(conn)
    assert len(interventions) == 1
    assert interventions[0]["category"] == "action_evidence_insufficient"
    assert interventions[0]["context"]["verification_id"]
    assert interventions[0]["context"]["verdict"] == "inconclusive"
    conn.close()


def test_unhandled_planner_failure_is_backed_off_instead_of_lost(tmp_path):
    conn = db.connect(tmp_path / "authority.db")
    objective = accepted_objective(conn)
    event_id = db.enqueue_objective_event(
        conn, objective_id=objective.id, event_type="provider.throttled", payload={}
    )
    planner = Planner([])

    def fail(snapshot, event):
        raise RuntimeError("HTTP 429")

    planner.propose = fail
    configured = charter()
    configured["retry_policy"] = {
        "max_attempts": 3,
        "base_backoff_seconds": 10,
        "max_backoff_seconds": 60,
    }
    loop = runtime.ObjectiveRuntime(
        conn, planner=planner, executor=Executor(), verifier=Verifier(),
        charter=configured, policy_version="charter-v1", runtime_id="runtime-test",
    )
    outcome = loop.tick()
    row = conn.execute(
        "SELECT * FROM objective_inbox WHERE id=?", (event_id,)
    ).fetchone()
    assert outcome.status == "retry_scheduled"
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["available_at"] >= row["updated_at"] + 10
    conn.close()


def test_rate_limited_planner_recovers_after_durable_backoff_without_duplicate_action(
    tmp_path,
):
    conn = db.connect(tmp_path / "authority.db")
    objective = accepted_objective(conn)
    event_id = db.enqueue_objective_event(
        conn, objective_id=objective.id, event_type="provider.throttled", payload={}
    )

    class RateLimited(RuntimeError):
        retry_after = 0

    class RetryOncePlanner(Planner):
        def __init__(self):
            super().__init__([recovery_action()])
            self.calls = 0

        def propose(self, snapshot, event):
            self.calls += 1
            if self.calls == 1:
                raise RateLimited("LLM HTTP 429 rate limit")
            return super().propose(snapshot, event)

    planner = RetryOncePlanner()
    executor = Executor()
    configured = {
        **charter(),
        "retry_policy": {
            "max_attempts": 3,
            "base_backoff_seconds": 0,
            "max_backoff_seconds": 60,
        },
    }
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=planner,
        executor=executor,
        verifier=Verifier(),
        charter=configured,
        policy_version="charter-v1",
        runtime_id="runtime-rate-limit-recovery",
    )

    first = loop.tick()
    row = conn.execute(
        "SELECT * FROM objective_inbox WHERE id=?", (event_id,)
    ).fetchone()
    assert first.status == "retry_scheduled"
    assert row["status"] == "pending"
    assert row["last_error"].startswith("rate_limited:")
    assert row["attempts"] == 1

    conn.close()
    conn = db.connect(tmp_path / "authority.db")
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=planner,
        executor=executor,
        verifier=Verifier(),
        charter=configured,
        policy_version="charter-v1",
        runtime_id="runtime-rate-limit-recovery-after-restart",
    )
    second = loop.tick()
    assert second.status == "verified"
    assert planner.calls == 2
    assert len(executor.calls) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM execution_results"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT status FROM objective_inbox WHERE id=?", (event_id,)
    ).fetchone()["status"] == "completed"
    conn.close()


def test_rate_limit_retry_hint_accepts_provider_headers():
    error = RuntimeError("provider throttled")
    error.response = SimpleNamespace(headers={"Retry-After": "7"})

    assert runtime._rate_limit_retry_after(error) == 7

    reset = str(int(time.time()) + 9)
    error.response = SimpleNamespace(
        headers={"x-ratelimit-reset-requests": reset}
    )
    assert 0 <= runtime._rate_limit_retry_after(error) <= 9


def test_multiple_effects_from_one_observation_are_preserved_but_not_executed(
    tmp_path,
):
    conn = db.connect(tmp_path / "authority.db")
    objective = accepted_objective(conn)
    db.enqueue_objective_event(
        conn, objective_id=objective.id, event_type="crm.lead.changed", payload={}
    )
    executor = Executor()
    loop = runtime.ObjectiveRuntime(
        conn,
        planner=Planner([action(), action()]),
        executor=executor,
        verifier=Verifier(),
        charter=charter(),
        policy_version="charter-v1",
        runtime_id="runtime-test",
    )
    outcome = loop.tick()
    snapshot = db.objective_snapshot(conn, objective.id)
    assert outcome.status == "escalated"
    assert "at most 1" in outcome.reason
    assert executor.calls == []
    assert snapshot["plans"][0]["tasks"] == [{"step": "update CRM"}]
    assert snapshot["actions"] == []
    assert db.get_objective(conn, objective.id).status == "blocked"
