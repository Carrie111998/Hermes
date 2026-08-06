from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import stat
import sqlite3
import threading
import time

import pytest

from hermes_cli import workspace_learning as learning_module
from hermes_cli.workspace_learning import (
    LearningController,
    LearningStore,
    ProfileLearningAdapter,
)


class FakeAdapter:
    def __init__(self):
        self.value = "baseline"
        self.snapshots = {}
        self.restore_attempts = set()
        self.restore_count = 0

    def canary(self, candidate):
        return {"passed": True, "checks": ["fake"]}

    def resource_key(self, candidate):
        return "fake:shared"

    def current_digest(self, candidate):
        return hashlib.sha256(self.value.encode()).hexdigest()

    def snapshot(self, candidate):
        snapshot_id = f"snapshot-{candidate['candidate_id']}"
        self.snapshots[snapshot_id] = self.value
        return snapshot_id

    def apply(self, candidate):
        self.value = candidate["proposal"]["content"]
        return {"applied": True, "version": "v1"}

    def restore(self, candidate, snapshot_id, *, attempt_id):
        if attempt_id not in self.restore_attempts:
            self.restore_attempts.add(attempt_id)
            self.restore_count += 1
            self.value = self.snapshots[snapshot_id]
        return {"restored": True}


def _metrics(successes=2, *, safety_failures=0, latency_ms=100, cost=1.0):
    return {
        "cases": 2,
        "cost": cost,
        "latency_ms": latency_ms,
        "safety_failures": safety_failures,
        "successes": successes,
    }


def _candidate(
    store,
    *,
    content="User prefers short answers",
    kind="explicit_correction",
    destination="user_memory",
    now: float = 100,
):
    signal = store.record_signal(
        actor_id="user-1",
        content=content,
        kind=kind,
        project_id="project-1",
        provenance=[{"source": "slack", "ref": "thread-1"}],
        reusable=True,
        now=now,
    )
    return store.propose_candidate(
        destination=destination,
        proposer_id="proposer-1",
        proposal={"action": "add", "content": content, "target": "user"},
        risk="low",
        signal_ids=[signal["signal_id"]],
        now=now + 1,
    )


def _ready_for_apply(store, controller, candidate_id, *, offset: float = 0):
    store.evaluate_candidate(
        candidate_id,
        evaluator_id=f"agent-evaluator-{offset}",
        baseline=_metrics(1),
        candidate=_metrics(2),
        held_out_digest="a" * 64,
        policy_digest="b" * 64,
        now=102 + offset,
    )
    store.approve_candidate(
        candidate_id,
        approver_id=f"human-approver-{offset}",
        now=103 + offset,
    )
    controller.run_canary(
        candidate_id,
        promoter_id=f"agent-promoter-{offset}",
        metrics=_metrics(2),
        now=104 + offset,
    )
    return f"agent-promoter-{offset}"


def test_single_observation_stays_run_note_but_repeated_procedure_stages_candidate(tmp_path):
    store = LearningStore(tmp_path / "learning.db")
    first = store.record_signal(
        actor_id="agent-1",
        content="Run the release checklist before publishing",
        kind="repeated_procedure",
        project_id="project-1",
        provenance=[{"source": "session", "ref": "session-1"}],
        reusable=True,
        now=100,
    )

    with pytest.raises(ValueError, match="two independent signals"):
        store.propose_candidate(
            destination="skill",
            proposer_id="proposer-1",
            proposal={
                "action": "create",
                "content": "---\nname: release-check\ndescription: Use when releasing.\n---\n# Release\n",
                "name": "release-check",
            },
            risk="medium",
            signal_ids=[first["signal_id"]],
            now=101,
        )

    second = store.record_signal(
        actor_id="agent-2",
        content="Run the release checklist before publishing",
        kind="repeated_procedure",
        project_id="project-1",
        provenance=[{"source": "session", "ref": "session-2"}],
        reusable=True,
        now=102,
    )
    candidate = store.propose_candidate(
        destination="skill",
        proposer_id="proposer-1",
        proposal={
            "action": "create",
            "content": "---\nname: release-check\ndescription: Use when releasing.\n---\n# Release\n",
            "name": "release-check",
        },
        risk="medium",
        signal_ids=[first["signal_id"], second["signal_id"]],
        now=103,
    )

    assert candidate["status"] == "staged"
    assert len(candidate["provenance"]) == 2
    store.close()


def test_role_separated_eval_approval_canary_apply_and_rollback(tmp_path):
    store = LearningStore(tmp_path / "learning.db")
    candidate = _candidate(store)

    with pytest.raises(ValueError, match="proposer cannot evaluate"):
        store.evaluate_candidate(
            candidate["candidate_id"],
            evaluator_id="proposer-1",
            baseline=_metrics(),
            candidate=_metrics(),
            held_out_digest="a" * 64,
            policy_digest="b" * 64,
            now=102,
        )

    evaluated = store.evaluate_candidate(
        candidate["candidate_id"],
        evaluator_id="evaluator-1",
        baseline=_metrics(successes=1, latency_ms=110, cost=1.1),
        candidate=_metrics(successes=2, latency_ms=100, cost=1.0),
        held_out_digest="a" * 64,
        policy_digest="b" * 64,
        now=103,
    )
    assert evaluated["status"] == "approval_pending"

    with pytest.raises(ValueError, match="evaluator or proposer"):
        store.approve_candidate(
            candidate["candidate_id"],
            approver_id="evaluator-1",
            now=104,
        )

    approved = store.approve_candidate(
        candidate["candidate_id"],
        approver_id="user-1",
        now=104,
    )
    assert approved["approval"]["content_digest"] == candidate["content_digest"]

    adapter = FakeAdapter()
    controller = LearningController(store, {"user_memory": adapter})
    canary = controller.run_canary(
        candidate["candidate_id"],
        promoter_id="promoter-1",
        metrics=_metrics(),
        now=105,
    )
    assert canary["status"] == "canary_passed"

    applied = controller.apply_candidate(
        candidate["candidate_id"],
        promoter_id="promoter-1",
        now=106,
    )
    assert applied["status"] == "applied"
    assert adapter.value == "User prefers short answers"

    rolled_back = controller.rollback_candidate(
        candidate["candidate_id"],
        actor_id="user-1",
        reason="Regression in held-out task",
        now=107,
    )
    assert rolled_back["status"] == "quarantined"
    assert adapter.value == "baseline"
    assert rolled_back["application"]["rolled_back_at"] == 107
    store.close()


def test_crash_after_destination_write_recovers_as_uncertain_and_rolls_back(tmp_path):
    class CrashAfterWriteAdapter(FakeAdapter):
        def apply(self, candidate):
            self.value = candidate["proposal"]["content"]
            raise KeyboardInterrupt("simulated process crash")

    database = tmp_path / "learning.db"
    store = LearningStore(database)
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    candidate = _candidate(store)
    store.evaluate_candidate(
        candidate["candidate_id"],
        evaluator_id="evaluator-1",
        baseline=_metrics(successes=1),
        candidate=_metrics(successes=2),
        held_out_digest="a" * 64,
        policy_digest="b" * 64,
        now=103,
    )
    store.approve_candidate(candidate["candidate_id"], approver_id="user-1", now=104)
    adapter = CrashAfterWriteAdapter()
    controller = LearningController(store, {"user_memory": adapter})
    controller.run_canary(
        candidate["candidate_id"],
        promoter_id="promoter-1",
        metrics=_metrics(),
        now=105,
    )
    with pytest.raises(KeyboardInterrupt):
        controller.apply_candidate(
            candidate["candidate_id"],
            promoter_id="promoter-1",
            now=106,
        )
    assert adapter.value == "User prefers short answers"
    store.close()

    reopened = LearningStore(database)
    recovered = reopened.get_candidate(candidate["candidate_id"])
    assert recovered["status"] == "apply_uncertain"
    assert recovered["application"]["backup_id"]
    recovery_controller = LearningController(reopened, {"user_memory": adapter})
    rolled_back = recovery_controller.rollback_candidate(
        candidate["candidate_id"],
        actor_id="user-1",
        reason="Uncertain apply after restart",
        now=107,
    )
    assert rolled_back["status"] == "quarantined"
    assert adapter.value == "baseline"
    reopened.close()


def test_concurrent_evaluators_use_compare_and_set_without_poisoning_connections(tmp_path):
    database = tmp_path / "learning.db"
    first = LearningStore(database)
    candidate = _candidate(first)
    second = LearningStore(database)
    barrier = threading.Barrier(2)

    def evaluate(store, evaluator_id):
        barrier.wait()
        try:
            store.evaluate_candidate(
                candidate["candidate_id"],
                evaluator_id=evaluator_id,
                baseline=_metrics(1),
                candidate=_metrics(2),
                held_out_digest="c" * 64,
                policy_digest="d" * 64,
                now=150,
            )
            return "ok"
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: evaluate(*args),
                [(first, "evaluator-1"), (second, "evaluator-2")],
            )
        )
    assert sorted(results) == ["conflict", "ok"]
    assert first.get_candidate(candidate["candidate_id"])["status"] == "approval_pending"
    assert second.get_candidate(candidate["candidate_id"])["status"] == "approval_pending"
    first.close()
    second.close()


def test_expired_canary_cannot_apply(tmp_path):
    store = LearningStore(tmp_path / "learning.db")
    adapter = FakeAdapter()
    controller = LearningController(store, {"user_memory": adapter})
    candidate = _candidate(store)
    promoter = _ready_for_apply(store, controller, candidate["candidate_id"])
    expires_at = float(store.get_candidate(candidate["candidate_id"])["expires_at"])

    with pytest.raises(ValueError, match="expired"):
        controller.apply_candidate(
            candidate["candidate_id"],
            promoter_id=promoter,
            now=expires_at + 1,
        )
    assert store.get_candidate(candidate["candidate_id"])["status"] == "expired"
    assert adapter.value == "baseline"


def test_expiry_during_snapshot_blocks_apply_before_destination_effect(tmp_path):
    class SlowSnapshotAdapter(FakeAdapter):
        apply_calls = 0

        def snapshot(self, candidate):
            time.sleep(0.08)
            return super().snapshot(candidate)

        def apply(self, candidate):
            self.apply_calls += 1
            return super().apply(candidate)

    database = tmp_path / "learning.db"
    store = LearningStore(database)
    adapter = SlowSnapshotAdapter()
    controller = LearningController(store, {"user_memory": adapter})
    candidate = _candidate(store, now=time.time())
    promoter = _ready_for_apply(store, controller, candidate["candidate_id"], offset=time.time())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE workspace_learning_candidates SET expires_at=? WHERE candidate_id=?",
            (time.time() + 0.04, candidate["candidate_id"]),
        )

    with pytest.raises(ValueError, match="expired"):
        controller.apply_candidate(candidate["candidate_id"], promoter_id=promoter)
    assert adapter.apply_calls == 0
    assert store.get_candidate(candidate["candidate_id"])["status"] == "expired"


def test_non_finite_metrics_are_rejected_without_transition(tmp_path):
    store = LearningStore(tmp_path / "learning.db")
    candidate = _candidate(store)
    invalid = _metrics(2)
    invalid["latency_ms"] = float("nan")
    with pytest.raises(ValueError, match="invalid"):
        store.evaluate_candidate(
            candidate["candidate_id"],
            evaluator_id="evaluator",
            baseline=_metrics(1),
            candidate=invalid,
            held_out_digest="a" * 64,
            policy_digest="b" * 64,
            now=102,
        )
    assert store.get_candidate(candidate["candidate_id"])["status"] == "staged"


def test_applied_candidate_does_not_absorb_new_evidence_or_reproposal(tmp_path):
    store = LearningStore(tmp_path / "learning.db")
    adapter = FakeAdapter()
    controller = LearningController(store, {"user_memory": adapter})
    first = _candidate(store, content="A")
    promoter = _ready_for_apply(store, controller, first["candidate_id"])
    controller.apply_candidate(first["candidate_id"], promoter_id=promoter, now=105)
    original_evidence = list(store.get_candidate(first["candidate_id"])["provenance"])

    second = _candidate(store, content="A", now=200)
    assert second["candidate_id"] != first["candidate_id"]
    assert store.get_candidate(first["candidate_id"])["provenance"] == original_evidence
    rolled_back = controller.rollback_candidate(
        first["candidate_id"],
        actor_id="human",
        reason="replace with a fresh proposal",
    )
    assert rolled_back["status"] == "quarantined"
    assert store.get_candidate(second["candidate_id"])["status"] == "staged"
    assert adapter.value == "baseline"


def test_resource_order_prevents_older_rollback_from_clobbering_newer_apply(tmp_path):
    store = LearningStore(tmp_path / "learning.db")
    adapter = FakeAdapter()
    controller = LearningController(store, {"user_memory": adapter})
    first = _candidate(store, content="A")
    promoter_one = _ready_for_apply(store, controller, first["candidate_id"])
    controller.apply_candidate(first["candidate_id"], promoter_id=promoter_one, now=105)
    second = _candidate(store, content="B", now=200)
    promoter_two = _ready_for_apply(store, controller, second["candidate_id"], offset=100)
    controller.apply_candidate(second["candidate_id"], promoter_id=promoter_two, now=305)

    with pytest.raises(ValueError, match="newer learning candidate"):
        controller.rollback_candidate(first["candidate_id"], actor_id="human", reason="old")
    controller.rollback_candidate(second["candidate_id"], actor_id="human", reason="undo B")
    assert adapter.value == "A"
    controller.rollback_candidate(first["candidate_id"], actor_id="human", reason="undo A")
    assert adapter.value == "baseline"


def test_interrupted_rollback_becomes_uncertain_and_can_retry(tmp_path):
    class FlakyRestoreAdapter(FakeAdapter):
        fail_restore = True

        def restore(self, candidate, snapshot_id, *, attempt_id):
            if self.fail_restore:
                self.fail_restore = False
                raise RuntimeError("restore interrupted")
            return super().restore(candidate, snapshot_id, attempt_id=attempt_id)

    store = LearningStore(tmp_path / "learning.db")
    adapter = FlakyRestoreAdapter()
    controller = LearningController(store, {"user_memory": adapter})
    candidate = _candidate(store)
    promoter = _ready_for_apply(store, controller, candidate["candidate_id"])
    controller.apply_candidate(candidate["candidate_id"], promoter_id=promoter, now=105)

    with pytest.raises(RuntimeError, match="restore interrupted"):
        controller.rollback_candidate(candidate["candidate_id"], actor_id="human", reason="retry")
    assert store.get_candidate(candidate["candidate_id"])["status"] == "rollback_uncertain"
    rolled_back = controller.rollback_candidate(
        candidate["candidate_id"], actor_id="human", reason="retry"
    )
    assert rolled_back["status"] == "quarantined"
    assert adapter.value == "baseline"


def test_crash_after_restore_reuses_rollback_attempt_id(tmp_path):
    class CrashAfterRestoreAdapter(FakeAdapter):
        crash_once = True

        def restore(self, candidate, snapshot_id, *, attempt_id):
            result = super().restore(candidate, snapshot_id, attempt_id=attempt_id)
            if self.crash_once:
                self.crash_once = False
                raise KeyboardInterrupt("simulated process exit after restore")
            return result

    store = LearningStore(tmp_path / "learning.db")
    adapter = CrashAfterRestoreAdapter()
    controller = LearningController(store, {"user_memory": adapter})
    candidate = _candidate(store)
    promoter = _ready_for_apply(store, controller, candidate["candidate_id"])
    controller.apply_candidate(candidate["candidate_id"], promoter_id=promoter, now=105)

    with pytest.raises(KeyboardInterrupt):
        controller.rollback_candidate(candidate["candidate_id"], actor_id="human", reason="crash")
    rolling = store.get_candidate(candidate["candidate_id"])
    original_attempt = rolling["application"]["rollback"]["attempt_id"]
    assert rolling["status"] == "rolling_back"
    recovery_time = time.time() + 1_000
    assert store.recover_stale_applications(now=recovery_time) == 1
    retried = controller.rollback_candidate(
        candidate["candidate_id"],
        actor_id="human",
        reason="retry after crash",
        now=recovery_time + 1,
    )
    assert retried["status"] == "quarantined"
    assert retried["application"]["rollback"]["attempt_id"] == original_attempt
    assert adapter.restore_count == 1


def test_concurrent_rollback_has_single_destination_writer(tmp_path):
    class BlockingRestoreAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def restore(self, candidate, snapshot_id, *, attempt_id):
            self.started.set()
            assert self.release.wait(timeout=5)
            return super().restore(candidate, snapshot_id, attempt_id=attempt_id)

    store = LearningStore(tmp_path / "learning.db")
    adapter = BlockingRestoreAdapter()
    controller = LearningController(store, {"user_memory": adapter})
    candidate = _candidate(store)
    promoter = _ready_for_apply(store, controller, candidate["candidate_id"])
    controller.apply_candidate(candidate["candidate_id"], promoter_id=promoter, now=105)

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            controller.rollback_candidate,
            candidate["candidate_id"],
            actor_id="human-one",
            reason="first",
        )
        assert adapter.started.wait(timeout=5)
        with pytest.raises(ValueError, match="rollback-actionable"):
            controller.rollback_candidate(
                candidate["candidate_id"], actor_id="human-two", reason="second"
            )
        adapter.release.set()
        assert first.result(timeout=5)["status"] == "quarantined"
    assert adapter.restore_count == 1


def test_dead_apply_owner_recovers_immediately_before_lease_expiry(tmp_path):
    database = tmp_path / "learning.db"
    store = LearningStore(database)
    adapter = FakeAdapter()
    controller = LearningController(store, {"user_memory": adapter})
    now = time.time()
    candidate = _candidate(store, now=now)
    promoter = _ready_for_apply(store, controller, candidate["candidate_id"], offset=now)
    snapshot_id = adapter.snapshot(candidate)
    now += 105
    store.begin_application(
        candidate["candidate_id"],
        promoter_id=promoter,
        application={
            "attempt_id": "attempt_" + "a" * 32,
            "backup_id": snapshot_id,
            "heartbeat_at": now,
            "lease_expires_at": now + 10_000,
            "owner_instance_id": "dead-owner",
            "owner_pid": 999_999,
            "post_digest": None,
            "pre_digest": adapter.current_digest(candidate),
            "resource_key": adapter.resource_key(candidate),
            "started_at": now,
            "version_id": "learning-dead-owner",
        },
        now=now,
    )
    store.close()

    reopened = LearningStore(database)
    assert reopened.get_candidate(candidate["candidate_id"])["status"] == "apply_uncertain"
    reopened.close()


def test_renewed_heartbeat_wins_recovery_compare_and_set(tmp_path, monkeypatch):
    database = tmp_path / "learning.db"
    first_store = LearningStore(database)
    second_store = LearningStore(database)
    adapter = FakeAdapter()
    controller = LearningController(first_store, {"user_memory": adapter})
    now = time.time()
    candidate = _candidate(first_store, now=now)
    promoter = _ready_for_apply(first_store, controller, candidate["candidate_id"], offset=now)
    now += 105
    attempt_id = "attempt_" + "b" * 32
    first_store.begin_application(
        candidate["candidate_id"],
        promoter_id=promoter,
        application={
            "attempt_id": attempt_id,
            "backup_id": adapter.snapshot(candidate),
            "heartbeat_at": now - 120,
            "lease_expires_at": now - 1,
            "owner_instance_id": learning_module._PROCESS_INSTANCE_ID,
            "owner_pid": os.getpid(),
            "post_digest": None,
            "pre_digest": adapter.current_digest(candidate),
            "resource_key": adapter.resource_key(candidate),
            "started_at": now - 120,
            "version_id": "learning-heartbeat-race",
        },
        now=now,
    )
    entered = threading.Event()
    proceed = threading.Event()
    original = learning_module._attempt_is_stale

    def paused_stale_check(attempt, timestamp):
        stale = original(attempt, timestamp)
        if stale:
            entered.set()
            assert proceed.wait(timeout=5)
        return stale

    monkeypatch.setattr(learning_module, "_attempt_is_stale", paused_stale_check)
    with ThreadPoolExecutor(max_workers=1) as executor:
        recovery = executor.submit(first_store.recover_stale_applications, now=now)
        assert entered.wait(timeout=5)
        assert second_store.heartbeat_operation(
            candidate["candidate_id"],
            attempt_id=attempt_id,
            operation="apply",
            now=now + 1,
        )
        proceed.set()
        assert recovery.result(timeout=5) == 0

    assert first_store.get_candidate(candidate["candidate_id"])["status"] == "applying"
    first_store.close()
    second_store.close()


def test_failed_eval_blocks_apply_and_quarantines_candidate(tmp_path):
    store = LearningStore(tmp_path / "learning.db")
    candidate = _candidate(store)
    failed = store.evaluate_candidate(
        candidate["candidate_id"],
        evaluator_id="evaluator-1",
        baseline=_metrics(),
        candidate=_metrics(successes=1, safety_failures=1),
        held_out_digest="a" * 64,
        policy_digest="b" * 64,
        now=103,
    )
    assert failed["status"] == "quarantined"
    controller = LearningController(store, {"user_memory": FakeAdapter()})
    with pytest.raises(ValueError, match="canary"):
        controller.apply_candidate(candidate["candidate_id"], promoter_id="promoter-1")
    store.close()


def test_secrets_and_pii_are_redacted_and_task_progress_never_promotes(tmp_path):
    store = LearningStore(tmp_path / "learning.db")
    fake_key = "sk-" + ("A" * 32)
    signal = store.record_signal(
        actor_id="user-1",
        content=f"Email me at person@example.com using key {fake_key}",
        kind="explicit_correction",
        project_id=None,
        provenance=[{"source": "chat", "ref": "message-1"}],
        reusable=True,
        now=100,
    )
    assert "person@example.com" not in signal["content"]
    assert "sk-" not in signal["content"]
    assert signal["redactions"] >= 2

    progress = store.record_signal(
        actor_id="agent-1",
        content="PR #123 completed at commit abcdef1234567890",
        kind="task_progress",
        project_id="project-1",
        provenance=[{"source": "session", "ref": "session-1"}],
        reusable=False,
        now=101,
    )
    with pytest.raises(ValueError, match="task progress"):
        store.propose_candidate(
            destination="memory",
            proposer_id="proposer-1",
            proposal={"action": "add", "content": progress["content"], "target": "memory"},
            risk="low",
            signal_ids=[progress["signal_id"]],
            now=102,
        )
    store.close()


def test_project_and_notion_destinations_cannot_bypass_normal_workflow(tmp_path):
    store = LearningStore(tmp_path / "learning.db")
    candidate = _candidate(store, destination="project_doc")
    store.evaluate_candidate(
        candidate["candidate_id"],
        evaluator_id="evaluator-1",
        baseline=_metrics(),
        candidate=_metrics(),
        held_out_digest="a" * 64,
        policy_digest="b" * 64,
        now=103,
    )
    store.approve_candidate(candidate["candidate_id"], approver_id="user-1", now=104)
    controller = LearningController(store, {})
    with pytest.raises(ValueError, match="normal project or external workflow"):
        controller.run_canary(
            candidate["candidate_id"],
            promoter_id="promoter-1",
            metrics=_metrics(),
            now=105,
        )
    assert store.get_candidate(candidate["candidate_id"])["status"] == "approved"
    store.close()


def test_real_memory_adapter_closed_loop_is_versioned_and_reversible(tmp_path, monkeypatch):
    profile_home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    store = LearningStore(profile_home / "learning.db")
    candidate = _candidate(store, content="User prefers verified examples")
    store.evaluate_candidate(
        candidate["candidate_id"],
        evaluator_id="evaluator-1",
        baseline=_metrics(successes=1),
        candidate=_metrics(successes=2),
        held_out_digest="a" * 64,
        policy_digest="b" * 64,
        now=103,
    )
    store.approve_candidate(candidate["candidate_id"], approver_id="user-1", now=104)
    adapter = ProfileLearningAdapter(profile_home)
    controller = LearningController(
        store,
        {"memory": adapter, "skill": adapter, "user_memory": adapter},
    )
    controller.run_canary(
        candidate["candidate_id"],
        promoter_id="promoter-1",
        metrics=_metrics(),
        now=105,
    )
    applied = controller.apply_candidate(
        candidate["candidate_id"],
        promoter_id="promoter-1",
        now=106,
    )
    memory_file = profile_home / "memories" / "USER.md"
    assert "User prefers verified examples" in memory_file.read_text()
    assert applied["application"]["version_id"]

    controller.rollback_candidate(
        candidate["candidate_id"],
        actor_id="user-1",
        reason="Canary regression",
        now=107,
    )
    assert not memory_file.exists() or "User prefers verified examples" not in memory_file.read_text()
    store.close()


def test_repeated_procedure_skill_closed_loop_applies_and_rolls_back(tmp_path, monkeypatch):
    profile_home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    store = LearningStore(profile_home / "learning.db")
    signal_ids = []
    for index in (1, 2):
        signal = store.record_signal(
            actor_id=f"agent-{index}",
            content="Run the release checklist before publishing",
            kind="repeated_procedure",
            project_id="project-1",
            provenance=[{"source": "session", "ref": f"session-{index}"}],
            reusable=True,
            now=100 + index,
        )
        signal_ids.append(signal["signal_id"])
    skill_content = (
        "---\n"
        "name: release-check\n"
        "description: Use when preparing a release. Validate before publishing.\n"
        "---\n\n"
        "# Release Check\n\n"
        "1. Run deterministic tests.\n"
        "2. Review the diff.\n"
        "3. Require approval before publishing.\n"
    )
    candidate = store.propose_candidate(
        destination="skill",
        proposer_id="proposer-1",
        proposal={"action": "create", "content": skill_content, "name": "release-check"},
        risk="medium",
        signal_ids=signal_ids,
        now=103,
    )
    store.evaluate_candidate(
        candidate["candidate_id"],
        evaluator_id="evaluator-1",
        baseline=_metrics(successes=1),
        candidate=_metrics(successes=2),
        held_out_digest="a" * 64,
        policy_digest="b" * 64,
        now=104,
    )
    store.approve_candidate(candidate["candidate_id"], approver_id="user-1", now=105)
    adapter = ProfileLearningAdapter(profile_home)
    controller = LearningController(store, {"skill": adapter})
    controller.run_canary(
        candidate["candidate_id"],
        promoter_id="promoter-1",
        metrics=_metrics(),
        now=106,
    )
    controller.apply_candidate(
        candidate["candidate_id"],
        promoter_id="promoter-1",
        now=107,
    )
    skill_file = profile_home / "skills" / "release-check" / "SKILL.md"
    assert skill_file.read_text().strip() == skill_content.strip()

    rolled_back = controller.rollback_candidate(
        candidate["candidate_id"],
        actor_id="user-1",
        reason="Regression",
        now=108,
    )
    assert rolled_back["status"] == "quarantined"
    assert not skill_file.exists()
    store.close()


def test_store_survives_restart_and_deduplicates_active_candidates(tmp_path):
    database = tmp_path / "learning.db"
    store = LearningStore(database)
    candidate = _candidate(store)
    store.close()

    reopened = LearningStore(database)
    assert reopened.get_candidate(candidate["candidate_id"])["content_digest"] == candidate["content_digest"]
    signal = reopened.record_signal(
        actor_id="user-2",
        content="User prefers short answers",
        kind="explicit_correction",
        project_id="project-1",
        provenance=[{"source": "chat", "ref": "message-2"}],
        reusable=True,
        now=200,
    )
    same = reopened.propose_candidate(
        destination="user_memory",
        proposer_id="proposer-2",
        proposal={"action": "add", "content": "User prefers short answers", "target": "user"},
        risk="low",
        signal_ids=[signal["signal_id"]],
        now=201,
    )
    assert same["candidate_id"] == candidate["candidate_id"]
    reopened.close()
