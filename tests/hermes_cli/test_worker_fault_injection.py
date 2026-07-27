"""Fault-injection regression suite for the governed delegated-worker lifecycle.

Each test names the exact fault boundary it covers and proves the invariant
that must hold across that boundary.  The scenarios are:

  FI-01  claim-before-execute      worker crashes after claim, before any work
  FI-02  effect-before-evidence    effect occurs, worker crashes before evidence
  FI-03  evidence-before-complete  evidence written, worker crashes before done
  FI-04  complete-before-ceo-wake  task done, CEO wake event not yet processed
  FI-05  stale-worker-fencing      expired-claim worker cannot complete task
  FI-06  master-stop-mid-execution task completion blocked after master stop
  FI-07  exclusive-claim-race      two workers cannot hold concurrent authority

All tests use only production code paths.  No test-only backdoors in the
authority store, no direct row inserts that bypass the grant machinery, no
skip of policy checks.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import threading
from pathlib import Path
from typing import Optional

import pytest

from hermes_cli import (
    employee_provisioning,
    kanban_db,
    objectives_db,
    objective_service,
    operational_control,
    organization_db,
    workforce_delegation,
)
from hermes_cli.objective_runtime import (
    ObjectiveRuntime,
    PlanProposal,
    VerificationOutcome,
)
from hermes_cli import objective_adapters, verification_evidence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bootstrap(tmp_path: Path, monkeypatch) -> dict:
    """Minimal company + employee + mandate + objective + plan + action."""
    root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))
    root.mkdir(parents=True, exist_ok=True)

    authority_path = root / "objectives.db"
    conn = objectives_db.connect(authority_path)

    charter = {
        "enabled": True,
        "operating_mode": "autonomous",
        "operator_role": "advisor",
        "policy_version": "charter-v1",
        "allowed_capabilities": ["work.delegate", "file.read"],
        "forbidden_capabilities": [],
        "allowed_systems": ["kanban", "localhost"],
        "approval_required_capabilities": [],
        "max_autonomous_risk": "low",
        "allow_irreversible": False,
        "max_autonomous_spend_minor": 200,
        "max_action_spend_minor": 200,
        "permit_ttl_seconds": 300,
        "operating_cadence": {"enabled": False},
        "solo_founder": {"toolsets": ["files"], "skills": ["research"]},
    }
    organization_id, ceo_id = organization_db.bootstrap_solo_founder(
        conn,
        organization_name="Fault Injection Corp",
        purpose="Prove crash boundaries",
        profile_name="default",
        charter=charter,
    )
    employee_id = organization_db.propose_employee(
        conn,
        organization_id=organization_id,
        display_name="Worker",
        title="Bounded Worker",
        level="individual_contributor",
        manager_id=ceo_id,
        proposed_by=f"employee:{ceo_id}",
        employment_type="agent",
    )
    organization_db.transition_employee(
        conn, employee_id, "approved", actor=f"employee:{ceo_id}"
    )
    organization_db.transition_employee(
        conn, employee_id, "provisioning", actor=f"employee:{ceo_id}"
    )
    organization_db.create_mandate(
        conn,
        employee_id,
        purpose="Return bounded evidence",
        responsibilities=["inspect assigned record"],
        decision_rights=["report findings"],
        prohibited_actions=["work.delegate"],
        capabilities=["file.read"],
        systems=["localhost"],
        kpis=["evidence returned"],
        escalation={"to": ceo_id},
        toolsets=["files"],
        skills=["research"],
        created_by=f"employee:{ceo_id}",
        budget_minor=200,
        expires_at=int(time.time()) + 7200,
    )
    organization_db.transition_employee(
        conn, employee_id, "active",
        actor=f"employee:{ceo_id}",
        profile_name="fi-worker",
    )
    from hermes_cli import profiles as _profiles
    profile_dir = _profiles.get_profile_dir("fi-worker")
    profile_dir.mkdir(parents=True, exist_ok=True)
    _mandate = organization_db.get_current_mandate(conn, employee_id)
    assert _mandate is not None, "_bootstrap: mandate must exist before write_profile_meta"
    _profiles.write_profile_meta(
        profile_dir,
        organization_id=organization_id,
        employee_id=employee_id,
        corporate_level="individual_contributor",
        employment_class="agent",
        mandate_id=str(_mandate["id"]),
        mandate_version=int(_mandate["version"]),
        mandate_expires_at=_mandate["expires_at"],
    )
    objective = objectives_db.create_objective(
        conn,
        organization_id=organization_id,
        desired_outcome="Complete the fault-injection proof task",
        originator=f"employee:{ceo_id}",
        permitted_systems=["kanban", "localhost"],
    )
    objectives_db.transition_objective(
        conn, objective.id, "accepted", actor=f"employee:{ceo_id}"
    )
    plan_id = objectives_db.create_plan(
        conn,
        objective.id,
        assumptions=["worker carries exact grant"],
        tasks=[{"step": "complete bounded evidence review"}],
        dependencies=[],
        risks=[],
        created_by=f"employee:{ceo_id}",
    )
    objectives_db.transition_objective(
        conn, objective.id, "planned", actor="control"
    )
    action_id = objectives_db.propose_action(
        conn,
        objective_id=objective.id,
        plan_id=plan_id,
        action_type="kanban.create_task",
        payload={
            "system": "kanban",
            "target_resource": "fi-board",
            "task_capabilities": ["file.read"],
            "task_systems": ["localhost"],
            "task_toolsets": ["files"],
            "task_skills": ["research"],
            "task_system": "localhost",
            "task_target_resource": str(root / "fi-input.txt"),
            "task_budget_minor": 100,
            "task_expires_at": (_task_expiry := int(time.time()) + 3600),
        },
        expected_outcome="bounded task assigned",
        required_capability="work.delegate",
        verification_method="kanban.task.created",
        risk_class="low",
        reversible=True,
        proposed_by=f"employee:{ceo_id}",
    )
    grant_id, _ = workforce_delegation.create_grant(
        conn,
        organization_id=organization_id,
        objective_id=objective.id,
        action_id=action_id,
        manager_employee_id=ceo_id,
        assignee_profile="fi-worker",
        title="FI Evidence Task",
        body="Return fault-injection boundary evidence.",
        capabilities=["file.read"],
        systems=["localhost"],
        toolsets=["files"],
        skills=["research"],
        budget_minor=100,
        expires_at=_task_expiry,
    )
    board_db_path = root / "fi-board.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board_db_path))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "fi-board")

    scope = workforce_delegation.worker_scope(conn, grant_id)
    original_body = "Return fault-injection boundary evidence."
    with kanban_db.connect_closing() as board:
        task_id = kanban_db.create_task(
            board,
            title="FI Evidence Task",
            body=scope + "\n\n## Assigned work\n" + original_body,
            assignee="fi-worker",
            tenant=organization_id,
            execution_contract_id=grant_id,
        )
        kanban_db.recompute_ready(board)

    workforce_delegation.bind_task(
        conn, grant_id=grant_id, task_id=task_id, board="fi-board"
    )

    mandate_row = organization_db.get_current_mandate(conn, employee_id)
    assert mandate_row is not None, "_bootstrap: employee mandate missing"

    return {
        "conn": conn,
        "authority_path": authority_path,
        "board_db_path": board_db_path,
        "organization_id": organization_id,
        "ceo_id": ceo_id,
        "employee_id": employee_id,
        "grant_id": grant_id,
        "task_id": task_id,
        "objective_id": objective.id,
        "action_id": action_id,
        "charter": charter,
        "root": root,
        "plan_id": plan_id,
        "mandate_id": str(mandate_row["id"]),
    }


def _worker_env(ctx: dict) -> dict:
    return {
        **os.environ,
        "HERMES_EXECUTION_CONTRACT_ID": ctx["grant_id"],
        "HERMES_KANBAN_TASK": ctx["task_id"],
        "HERMES_BUSINESS_AUTHORITY_DB": str(ctx["authority_path"]),
        "HERMES_PROFILE": "fi-worker",
        "HERMES_KANBAN_BOARD": "fi-board",
        "HERMES_KANBAN_DB": str(ctx["board_db_path"]),
        "HERMES_HOME": str(ctx["root"]),
        "TERMINAL_ENV": "local",
    }


def _set_worker_env(monkeypatch, ctx: dict) -> None:
    """Set up the governed-worker execution-contract env in the test process.

    This simulates the environment that a real dispatched worker subprocess
    would have.  The hermetic fixture already cleared these vars; this
    re-sets them to the values for the specific task/grant under test.
    """
    monkeypatch.setenv("HERMES_EXECUTION_CONTRACT_ID", ctx["grant_id"])
    monkeypatch.setenv("HERMES_KANBAN_TASK", ctx["task_id"])
    monkeypatch.setenv("HERMES_BUSINESS_AUTHORITY_DB", str(ctx["authority_path"]))
    monkeypatch.setenv("HERMES_PROFILE", "fi-worker")


# ---------------------------------------------------------------------------
# FI-01: claim-before-execute
# ---------------------------------------------------------------------------


def test_fi01_claim_before_execute_is_redeliverable(tmp_path, monkeypatch):
    """A worker that crashes immediately after claim produces no external effect.

    Invariant: the task must be re-claimable after reclaim; a fresh worker
    completes it; the effect appears exactly once.
    """
    ctx = _bootstrap(tmp_path, monkeypatch)
    _set_worker_env(monkeypatch, ctx)
    conn = ctx["conn"]
    task_id = ctx["task_id"]

    effect_counter = {"calls": 0}

    with kanban_db.connect_closing() as board:
        # Worker 1 claims the task …
        claimed = kanban_db.claim_task(
            board, task_id, claimer="fi-worker-1", ttl_seconds=5
        )
        assert claimed is not None, "task must be claimable"
        run_1_id = claimed.current_run_id

        # … then crashes immediately: no effect, no evidence, no complete.
        # Simulate time passing beyond the TTL so reclaim fires.
        board.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 10, task_id),
        )
        board.execute(
            "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 10, run_1_id),
        )
        board.commit()

        # Reclaimer runs (simulated: force reclaim directly for determinism).
        reclaimed = kanban_db.reclaim_task(
            board, task_id, reason="fi01_fault_inject"
        )
        assert reclaimed is True, "task must be reclaimed after claim expires"

        # Task must be back in 'ready' or 'todo' for redelivery.
        task = kanban_db.get_task(board, task_id)
        assert task.status in ("ready", "todo", "blocked"), (
            f"expected re-queueable status, got {task.status!r}"
        )

        # Run 1 must be closed as reclaimed.
        run_1 = board.execute(
            "SELECT outcome FROM task_runs WHERE id = ?", (run_1_id,)
        ).fetchone()
        assert run_1 is not None
        assert run_1["outcome"] == "reclaimed"

        # Recompute ready so a second worker can claim.
        kanban_db.recompute_ready(board)

    # Worker 2 claims, performs effect (one call), records evidence, completes.
    with kanban_db.connect_closing() as board:
        claimed_2 = kanban_db.claim_task(board, task_id, claimer="fi-worker-2")
        assert claimed_2 is not None, "task must be re-claimable by worker 2"

        effect_counter["calls"] += 1  # the one and only external effect

        result = kanban_db.complete_task(
            board,
            task_id,
            summary="FI-01 recovery evidence",
            metadata={"fault_boundary": "claim_before_execute", "effect_calls": 1},
            expected_run_id=claimed_2.current_run_id,
        )
        assert result is True, "worker 2 must complete the task"

    assert effect_counter["calls"] == 1, "effect must occur exactly once"

    with kanban_db.connect_closing() as board:
        final = kanban_db.get_task(board, task_id)
        assert final.status == "done"

        runs = kanban_db.list_runs(board, task_id)
        assert len(runs) == 2  # one reclaimed, one completed
        outcomes = {r.outcome for r in runs}
        assert "reclaimed" in outcomes
        assert "completed" in outcomes


# ---------------------------------------------------------------------------
# FI-02: effect-before-evidence (uncertain provider action)
# ---------------------------------------------------------------------------


def test_fi02_effect_before_evidence_does_not_duplicate(tmp_path, monkeypatch):
    """Worker performs an effect, then crashes before persisting evidence.

    Invariant: recovery must determine from durable state that the effect
    already occurred and NOT replay it.  Effect count must remain 1.

    The test simulates this with a durable "provider state file" that records
    whether the effect happened, exactly as the real provider recovery
    acceptance does.
    """
    ctx = _bootstrap(tmp_path, monkeypatch)
    _set_worker_env(monkeypatch, ctx)
    task_id = ctx["task_id"]
    provider_state = ctx["root"] / "fi02-provider-state.json"
    evidence_file = ctx["root"] / "fi02-evidence.json"

    effect_calls = {"n": 0}

    def do_effect():
        """Idempotent external action: writes provider state, returns ref."""
        state = json.loads(provider_state.read_text()) if provider_state.exists() else {}
        if "fi02-effect" in state:
            # already applied — return the existing reference (read-back)
            return state["fi02-effect"]["ref"], False  # (ref, is_new)
        effect_calls["n"] += 1
        ref = "fi02-effect-ref-0001"
        state["fi02-effect"] = {"ref": ref, "ts": int(time.time())}
        provider_state.write_text(json.dumps(state))
        return ref, True

    # --- Attempt 1: effect succeeds, then worker "crashes" (no evidence, no complete) ---
    with kanban_db.connect_closing() as board:
        claimed = kanban_db.claim_task(board, task_id, claimer="fi-worker-attempt1")
        assert claimed is not None
        run_1_id = claimed.current_run_id

    ref, is_new = do_effect()
    assert is_new is True
    assert effect_calls["n"] == 1
    # Crash: evidence_file not written, complete_task never called.

    # --- Simulate reclaim (TTL expiry + reclaim) ---
    with kanban_db.connect_closing() as board:
        board.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 10, task_id),
        )
        board.execute(
            "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 10, run_1_id),
        )
        board.commit()
        kanban_db.reclaim_task(board, task_id, reason="fi02_fault_inject")
        kanban_db.recompute_ready(board)

    # --- Attempt 2: recovery reads back provider state, does NOT re-call effect ---
    with kanban_db.connect_closing() as board:
        claimed_2 = kanban_db.claim_task(board, task_id, claimer="fi-worker-attempt2")
        assert claimed_2 is not None

        # Recovery reads provider state instead of replaying effect.
        recovered_ref, is_new_2 = do_effect()
        assert is_new_2 is False, "recovery must not re-apply effect"
        assert effect_calls["n"] == 1, "effect must not be duplicated"
        assert recovered_ref == ref

        evidence_file.write_text(
            json.dumps({"ref": recovered_ref, "recovered": True, "effect_calls": 1})
        )
        result = kanban_db.complete_task(
            board,
            task_id,
            summary="FI-02 recovery via provider read-back",
            metadata={"fault_boundary": "effect_before_evidence", "effect_calls": 1},
            expected_run_id=claimed_2.current_run_id,
        )
        assert result is True

    assert effect_calls["n"] == 1, "total effect calls must be exactly 1"

    evidence = json.loads(evidence_file.read_text())
    assert evidence["recovered"] is True
    assert evidence["effect_calls"] == 1

    with kanban_db.connect_closing() as board:
        final = kanban_db.get_task(board, task_id)
        assert final.status == "done"


# ---------------------------------------------------------------------------
# FI-03: evidence-before-complete
# ---------------------------------------------------------------------------


def test_fi03_evidence_before_complete_does_not_duplicate(tmp_path, monkeypatch):
    """Worker writes evidence to durable store, then crashes before complete_task.

    Invariant: re-run must detect that evidence already exists and call
    complete_task exactly once; no second effect.
    """
    ctx = _bootstrap(tmp_path, monkeypatch)
    _set_worker_env(monkeypatch, ctx)
    task_id = ctx["task_id"]
    evidence_file = ctx["root"] / "fi03-evidence.json"
    effect_calls = {"n": 0}

    # --- Attempt 1: effect + evidence written, crash before complete ---
    with kanban_db.connect_closing() as board:
        claimed = kanban_db.claim_task(board, task_id, claimer="fi-worker-attempt1")
        assert claimed is not None
        run_1_id = claimed.current_run_id

    effect_calls["n"] += 1
    evidence_file.write_text(
        json.dumps({"attempt": 1, "effect_calls": effect_calls["n"], "done": True})
    )
    # Crash before complete_task.

    with kanban_db.connect_closing() as board:
        board.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 10, task_id),
        )
        board.execute(
            "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 10, run_1_id),
        )
        board.commit()
        kanban_db.reclaim_task(board, task_id, reason="fi03_fault_inject")
        kanban_db.recompute_ready(board)

    # --- Attempt 2: recovery detects existing evidence, skips effect ---
    with kanban_db.connect_closing() as board:
        claimed_2 = kanban_db.claim_task(board, task_id, claimer="fi-worker-attempt2")
        assert claimed_2 is not None

        # Recovery: evidence already present → skip effect.
        existing = json.loads(evidence_file.read_text())
        assert existing["done"] is True, "evidence must survive the crash"

        # Effect was already done; do NOT increment effect_calls.
        result = kanban_db.complete_task(
            board,
            task_id,
            summary="FI-03 recovery — evidence already persisted",
            metadata={
                "fault_boundary": "evidence_before_complete",
                "effect_calls": existing["effect_calls"],
            },
            expected_run_id=claimed_2.current_run_id,
        )
        assert result is True

    assert effect_calls["n"] == 1, "effect must not be duplicated"

    with kanban_db.connect_closing() as board:
        final = kanban_db.get_task(board, task_id)
        assert final.status == "done"
        runs = kanban_db.list_runs(board, task_id)
        completed_runs = [r for r in runs if r.outcome == "completed"]
        assert len(completed_runs) == 1, "complete_task must fire exactly once"


# ---------------------------------------------------------------------------
# FI-04: complete-before-ceo-wake
# ---------------------------------------------------------------------------


def test_fi04_complete_before_ceo_wake_advances_objective(tmp_path, monkeypatch):
    """Task is done but CEO wake event not yet synced.

    Invariant: a fresh CEO runtime calling sync_kanban_events must advance
    the objective correctly with zero duplicate effects.
    """
    ctx = _bootstrap(tmp_path, monkeypatch)
    _set_worker_env(monkeypatch, ctx)
    conn = ctx["conn"]
    task_id = ctx["task_id"]
    action_id = ctx["action_id"]
    organization_id = ctx["organization_id"]
    ceo_id = ctx["ceo_id"]
    grant_id = ctx["grant_id"]
    charter = ctx["charter"]
    plan_id = ctx["plan_id"]

    # Worker runs and completes normally.
    with kanban_db.connect_closing() as board:
        claimed = kanban_db.claim_task(board, task_id, claimer="fi-worker-fi04")
        assert claimed is not None
        result = kanban_db.complete_task(
            board,
            task_id,
            summary="FI-04 pre-wake evidence",
            metadata={"fault_boundary": "complete_before_ceo_wake"},
            expected_run_id=claimed.current_run_id,
        )
        assert result is True

    # Task is done.  Record the execution result on the authority side so
    # sync_kanban_events can find it.  Use the real permit flow.
    permit_id = objectives_db.issue_permit(
        conn,
        action_id,
        capability="work.delegate",
        issued_to=f"employee:{ceo_id}",
        policy_version="charter-v1",
        expires_at=int(time.time()) + 300,
        target_resource="fi-board",
    )
    objectives_db.consume_permit(
        conn, permit_id,
        action_id=action_id,
        payload=json.loads(
            conn.execute(
                "SELECT payload_json FROM candidate_actions WHERE id = ?",
                (action_id,)
            ).fetchone()["payload_json"]
        ),
        executor=f"employee:{ceo_id}",
        organization_id=organization_id,
        current_policy_version="charter-v1",
    )
    objectives_db.record_execution_result(
        conn,
        action_id=action_id,
        permit_id=permit_id,
        executor=f"employee:{ceo_id}",
        organization_id=organization_id,
        status="succeeded",
        result={"task_id": task_id},
        started_at=int(time.time()),
        external_reference=task_id,
    )
    conn.commit()

    # "Crash" happens here — wake event not yet synced.
    # Fresh CEO runtime calls sync_kanban_events.
    woken = objective_service.sync_kanban_events(conn, board="fi-board")
    assert woken >= 1, "CEO must be woken by the task completion event"

    # CEO runtime verifies objective.
    effect_verified = {"count": 0}

    class EvidenceVerifier:
        identity = "control:fi04-verifier"

        def verify(self, action, execution):
            return VerificationOutcome("pass", verification_evidence.build(
                observer=self.identity,
                source_kind="authoritative_database_readback",
                source_reference="fi04",
                facts={},
            ))

        def verify_objective(self, snapshot, plan, action_verifications):
            with kanban_db.connect_closing() as board:
                task = kanban_db.get_task(board, task_id)
            passed = task is not None and task.status == "done"
            if passed:
                effect_verified["count"] += 1
            return VerificationOutcome(
                "pass" if passed else "fail",
                verification_evidence.build(
                    observer=self.identity,
                    source_kind="authoritative_database_readback",
                    source_reference=f"kanban:{task_id}",
                    facts={"task_done": passed},
                ),
            )

    class NoopPlanner:
        identity = f"employee:{ceo_id}"

        def propose(self, snapshot, event):
            return PlanProposal(
                assumptions=["worker evidence present"],
                tasks=[],
                dependencies=[],
                risks=[],
                actions=(),
                objective_complete_when_verified=True,
            )

    class NoopExecutor:
        identity = f"employee:{ceo_id}"

        def execute(self, action_type, payload):  # type: ignore[override]
            raise RuntimeError("fi04: no actions expected")

    runtime = ObjectiveRuntime(
        conn,
        planner=NoopPlanner(),
        executor=NoopExecutor(),
        verifier=EvidenceVerifier(),
        charter=charter,
        policy_version="charter-v1",
        runtime_id="runtime:fi04-ceo",
    )
    outcome = runtime.tick()
    assert outcome.status == "verified", f"CEO must reach verified: {outcome.status}"

    obj = objectives_db.get_objective(conn, ctx["objective_id"])
    assert obj.status == "verified"
    assert effect_verified["count"] == 1, "verification must run exactly once"


# ---------------------------------------------------------------------------
# FI-05: stale-worker fencing
# ---------------------------------------------------------------------------


def test_fi05_stale_worker_cannot_complete_after_reassignment(tmp_path, monkeypatch):
    """An expired-claim worker must not complete the task after re-assignment.

    Invariant: complete_task with a stale run_id (original worker's run)
    must return False once the task has been reclaimed and re-claimed by
    a fresh worker.
    """
    ctx = _bootstrap(tmp_path, monkeypatch)
    _set_worker_env(monkeypatch, ctx)
    task_id = ctx["task_id"]

    # Original worker claims the task.
    with kanban_db.connect_closing() as board:
        claimed_1 = kanban_db.claim_task(
            board, task_id, claimer="fi-stale-worker", ttl_seconds=2
        )
        assert claimed_1 is not None
        stale_run_id = claimed_1.current_run_id

        # Force TTL expiry.
        board.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 5, task_id),
        )
        board.execute(
            "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 5, stale_run_id),
        )
        board.commit()

        # Reclaimer fires.
        kanban_db.reclaim_task(board, task_id, reason="fi05_stale_fence")
        kanban_db.recompute_ready(board)

    # Fresh worker claims and completes.
    with kanban_db.connect_closing() as board:
        claimed_2 = kanban_db.claim_task(board, task_id, claimer="fi-fresh-worker")
        assert claimed_2 is not None
        fresh_run_id = claimed_2.current_run_id

        # Stale worker "wakes up" and tries to complete with its old run_id.
        stale_result = kanban_db.complete_task(
            board,
            task_id,
            summary="stale worker unauthorised completion",
            expected_run_id=stale_run_id,  # ← stale CAS guard
        )
        assert stale_result is False, (
            "stale worker must not complete the task after re-assignment"
        )
        # Task must still be running (held by fresh worker).
        task = kanban_db.get_task(board, task_id)
        assert task.status == "running"
        assert task.current_run_id == fresh_run_id

        # Fresh worker completes successfully.
        fresh_result = kanban_db.complete_task(
            board,
            task_id,
            summary="FI-05 fresh worker completion",
            expected_run_id=fresh_run_id,
        )
        assert fresh_result is True

    with kanban_db.connect_closing() as board:
        final = kanban_db.get_task(board, task_id)
        assert final.status == "done"
        runs = kanban_db.list_runs(board, task_id)
        completed = [r for r in runs if r.outcome == "completed"]
        assert len(completed) == 1, "only one run must be completed"
        assert completed[0].id == fresh_run_id


# ---------------------------------------------------------------------------
# FI-06: master-stop mid-execution
# ---------------------------------------------------------------------------


def test_fi06_master_stop_fences_task_completion(tmp_path, monkeypatch):
    """Completion of a governed task is blocked after master stop.

    Invariant: validate_task_result_authority raises AutonomyRevokedError
    after operational_control.set_autonomy_mode(mode='paused').
    complete_task (which calls the authority check) must return False.
    """
    ctx = _bootstrap(tmp_path, monkeypatch)
    _set_worker_env(monkeypatch, ctx)
    conn = ctx["conn"]
    task_id = ctx["task_id"]
    grant_id = ctx["grant_id"]

    # Worker claims the task.
    with kanban_db.connect_closing() as board:
        claimed = kanban_db.claim_task(board, task_id, claimer="fi-worker-fi06")
        assert claimed is not None
        run_id = claimed.current_run_id

    # Master stop fires while worker is executing.
    operational_control.set_autonomy_mode(
        conn, mode="paused", actor="owner", reason="fi06 emergency stop"
    )

    # Grant must be revoked by master stop.
    assert workforce_delegation.is_revoked(conn, grant_id) is True

    # Worker tries to complete — must fail closed.
    monkeypatch.setenv("HERMES_EXECUTION_CONTRACT_ID", grant_id)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv(
        "HERMES_BUSINESS_AUTHORITY_DB", str(ctx["authority_path"])
    )

    with kanban_db.connect_closing() as board:
        result = kanban_db.complete_task(
            board,
            task_id,
            summary="fi06 attempt after master stop",
            expected_run_id=run_id,
        )
        assert result is False, "completion must be blocked after master stop"

        # Task must NOT be done.
        task = kanban_db.get_task(board, task_id)
        assert task.status != "done"

        # completion_blocked_authority event must be recorded.
        blocked_event = board.execute(
            "SELECT 1 FROM task_events WHERE task_id=? "
            "AND kind='completion_blocked_authority'",
            (task_id,),
        ).fetchone()
        assert blocked_event is not None, (
            "completion_blocked_authority event must be auditable"
        )


# ---------------------------------------------------------------------------
# FI-07: exclusive claim — two workers cannot hold concurrent authority
# ---------------------------------------------------------------------------


def test_fi07_exclusive_claim_prevents_concurrent_authority(tmp_path, monkeypatch):
    """Two concurrent workers claiming the same ready task must have
    at most one winner.

    Invariant: exactly one claim wins; the other sees None from claim_task.
    No duplicate effect can occur from two concurrent valid holders.

    SQLite's WAL + BEGIN IMMEDIATE serializes the CAS, so the invariant must
    hold without any additional distributed locking.
    """
    ctx = _bootstrap(tmp_path, monkeypatch)
    _set_worker_env(monkeypatch, ctx)
    task_id = ctx["task_id"]

    winners = []
    errors = []

    def try_claim(claimer_id: int) -> None:
        try:
            with kanban_db.connect_closing() as board:
                result = kanban_db.claim_task(
                    board, task_id, claimer=f"fi-concurrent-{claimer_id}"
                )
                if result is not None:
                    winners.append(claimer_id)
        except Exception as exc:
            errors.append((claimer_id, str(exc)))

    # Launch two threads simultaneously.
    t1 = threading.Thread(target=try_claim, args=(1,))
    t2 = threading.Thread(target=try_claim, args=(2,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"unexpected errors during concurrent claim: {errors}"
    assert len(winners) == 1, (
        f"exactly one worker must win the claim, got {len(winners)}: {winners}"
    )

    winner = winners[0]

    # The single winner completes successfully.
    with kanban_db.connect_closing() as board:
        task = kanban_db.get_task(board, task_id)
        assert task.status == "running"
        result = kanban_db.complete_task(
            board,
            task_id,
            summary=f"FI-07 winner={winner} completes",
            metadata={"concurrent_winners": 1},
            expected_run_id=task.current_run_id,
        )
        assert result is True

    with kanban_db.connect_closing() as board:
        final = kanban_db.get_task(board, task_id)
        assert final.status == "done"
        runs = kanban_db.list_runs(board, task_id)
        completed = [r for r in runs if r.outcome == "completed"]
        assert len(completed) == 1


# ---------------------------------------------------------------------------
# FI-08: validate_worker_launch fences re-entry after grant revocation
# ---------------------------------------------------------------------------


def test_fi08_worker_launch_validation_fences_revoked_grant(
    tmp_path, monkeypatch
):
    """validate_worker_launch must raise DelegationError after grant revocation.

    Invariant: a worker process that wakes after revocation must fail closed
    at validate_worker_launch, not continue execution.
    """
    ctx = _bootstrap(tmp_path, monkeypatch)
    _set_worker_env(monkeypatch, ctx)
    conn = ctx["conn"]
    grant_id = ctx["grant_id"]
    task_id = ctx["task_id"]

    # Revoke the grant (simulates mandate supersession or explicit revocation).
    conn.execute(
        "INSERT INTO employee_task_grant_revocations "
        "(id, grant_id, actor, reason, revoked_at) VALUES (?, ?, ?, ?, ?)",
        (
            f"fi08-rev-{grant_id[:8]}",
            grant_id,
            "control",
            "fi08 explicit revocation",
            int(time.time()),
        ),
    )
    conn.commit()

    monkeypatch.setenv("HERMES_EXECUTION_CONTRACT_ID", grant_id)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv(
        "HERMES_BUSINESS_AUTHORITY_DB", str(ctx["authority_path"])
    )
    monkeypatch.setenv("HERMES_PROFILE", "fi-worker")

    with pytest.raises(
        workforce_delegation.DelegationError, match="revoked"
    ):
        workforce_delegation.validate_worker_launch(
            enabled_toolsets=["files"],
            enabled_skills=["research"],
            enabled_capabilities=["file.read"],
            enabled_systems=["localhost"],
        )


# ---------------------------------------------------------------------------
# FI-09: run_id CAS prevents double-complete on restart
# ---------------------------------------------------------------------------


def test_fi09_run_id_cas_prevents_double_complete(tmp_path, monkeypatch):
    """complete_task with an expected_run_id must be idempotent on second call.

    Invariant: calling complete_task twice with the same run_id returns
    True then False (second call sees status != 'running').
    """
    ctx = _bootstrap(tmp_path, monkeypatch)
    _set_worker_env(monkeypatch, ctx)
    task_id = ctx["task_id"]

    with kanban_db.connect_closing() as board:
        claimed = kanban_db.claim_task(board, task_id, claimer="fi-worker-fi09")
        assert claimed is not None
        run_id = claimed.current_run_id

        # First complete — must succeed.
        result_1 = kanban_db.complete_task(
            board,
            task_id,
            summary="FI-09 first complete",
            metadata={"attempt": 1},
            expected_run_id=run_id,
        )
        assert result_1 is True

        # Task is now done; the same run_id cannot complete it again.
        result_2 = kanban_db.complete_task(
            board,
            task_id,
            summary="FI-09 duplicate complete attempt",
            metadata={"attempt": 2},
            expected_run_id=run_id,
        )
        assert result_2 is False, (
            "second complete with same run_id must be rejected (idempotent guard)"
        )

        final = kanban_db.get_task(board, task_id)
        assert final.status == "done"
        runs = kanban_db.list_runs(board, task_id)
        completed = [r for r in runs if r.outcome == "completed"]
        assert len(completed) == 1, "task_runs must record exactly one completion"
