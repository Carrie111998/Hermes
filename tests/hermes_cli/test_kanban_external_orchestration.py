"""End-to-end contracts for durable Kanban external-run orchestration."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

import pytest
from PIL import Image

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    (home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def _attest_legacy_scratch_candidates(monkeypatch):
    """Keep legacy test scenarios while supplying the now-required receipt."""
    original_candidate = kb.create_candidate
    original_create = kb.create_task
    original_complete = kb.complete_task

    def attested(conn, task_id, *, sha, source_receipt=None):
        conn.execute(
            "UPDATE tasks SET assignee='implementer' WHERE id=? AND (assignee IS NULL OR assignee='')",
            (task_id,),
        )
        return original_candidate(conn, task_id, sha=sha, source_receipt=source_receipt or {
            "candidate_sha": sha, "subject": "test candidate", "provenance": "test coordinator receipt", "producer_profile": "implementer",
        })

    def exact_gate(conn, **kwargs):
        if kwargs.get("task_kind") in {"reviewer", "visualqa"} and kwargs.get("created_by_task_id"):
            conn.execute("UPDATE tasks SET assignee='implementer' WHERE id=? AND (assignee IS NULL OR assignee='')", (kwargs["created_by_task_id"],))
            if kwargs.get("assignee") == "implementer":
                kwargs["task_kind"] = "ordinary"
                return original_create(conn, **kwargs)
            receipt = original_get_frozen(conn, kwargs["created_by_task_id"])
            if receipt is not None:
                kwargs["gate_candidate_sha"] = receipt["sha"]
                kwargs["gate_manifest_hash"] = receipt["manifest_hash"]
                kwargs.pop("owner_kind", None)  # inferred agent avoids unrelated profile fixture admission.
        return original_create(conn, **kwargs)

    def bound_completion(conn, task_id, **kwargs):
        task = kb.get_task(conn, task_id)
        if task and task.task_kind in {"reviewer", "visualqa"}:
            kwargs.setdefault("metadata", {
                "candidate_sha": task.gate_candidate_sha,
                "manifest_hash": task.gate_manifest_hash,
            })
            claimed = kb.claim_task(conn, task_id)
            assert claimed is not None and claimed.current_run_id is not None
            kwargs.setdefault("expected_run_id", claimed.current_run_id)
            monkeypatch.setenv("HERMES_PROFILE", task.assignee)
            try:
                return original_complete(conn, task_id, **kwargs)
            finally:
                monkeypatch.setenv("HERMES_PROFILE", "coordinator")
        return original_complete(conn, task_id, **kwargs)

    original_get_frozen = kb.get_frozen_evidence_manifest
    monkeypatch.setattr(kb, "create_candidate", attested)
    monkeypatch.setattr(kb, "create_task", exact_gate)
    monkeypatch.setattr(kb, "complete_task", bound_completion)


def test_external_run_is_durable_and_redacts_sensitive_refs(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="external build", assignee="operator", owner_kind="external")
        run = kb.start_external_run(
            conn,
            task_id,
            owner="operator",
            external_id="build-42",
            pid=4242,
            phase="upload",
            current=1,
            total=3,
            log_ref="https://token:secret@example.test/log",
            result_ref="Bearer top-secret",
            max_retries=2,
        )
        assert run.owner_kind == "external"
        assert run.external_id == "build-42"
        assert run.worker_pid == 4242
        assert run.phase == "upload"
        assert (run.progress_current, run.progress_total) == (1, 3)
        assert "secret" not in (run.log_ref or "")
        assert "secret" not in (run.result_ref or "")

    # A new registry/process reopening the same SQLite database sees the run.
    with kb.connect() as reopened:
        restored = kb.get_external_run(reopened, run.id)
        assert restored is not None
        assert restored.external_id == "build-42"
        assert restored.status == "running"


def test_external_run_heartbeat_and_completion_are_idempotent(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="external deploy", assignee="operator", owner_kind="external")
        run = kb.start_external_run(
            conn, task_id, owner="operator", external_id="deploy-7", phase="build",
        )
        assert kb.heartbeat_external_run(
            conn, run.id, owner="operator", phase="publish", current=2, total=2,
        ) is True
        assert kb.finish_external_run(
            conn, run.id, owner="operator", outcome="completed", result_ref="done",
        ) is True
        # Duplicate provider webhooks must not reopen/re-complete the card.
        assert kb.finish_external_run(
            conn, run.id, owner="operator", outcome="completed", result_ref="done",
        ) is False
        assert kb.get_task(conn, task_id).status == "done"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='completed'", (task_id,)
        ).fetchone()[0] == 1


def test_create_task_rejects_unknown_inferred_agent_assignee(kanban_home, monkeypatch):
    """The DB admission boundary rejects implicit agent ownership too."""
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: False)
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="agent owner"):
            kb.create_task(conn, title="missing implicit agent", assignee="missing")
        assert kb.list_tasks(conn) == []
        external = kb.create_task(
            conn, title="intentional external", assignee="missing", owner_kind="external",
        )
        no_agent = kb.create_task(
            conn, title="intentional manual", assignee="missing", owner_kind="no_agent",
        )
    assert external and no_agent


def test_shared_kanban_fixture_does_not_admit_unlisted_agent_profile(kanban_home):
    """An arbitrary agent assignee stays absent despite shared Kanban setup."""
    import hermes_cli.profiles as profiles

    unlisted = "arbitrary-unlisted-profile"
    assert profiles.profile_exists(unlisted) is False
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="agent owner"):
            kb.create_task(conn, title="unlisted agent", assignee=unlisted)
    assert profiles.profile_exists(unlisted) is False


def test_creation_owner_and_provenance_are_immutable_at_db_boundary(kanban_home, monkeypatch):
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "worker")
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="agent owner"):
            kb.create_task(conn, title="bad", assignee="missing", owner_kind="agent")
        task_id = kb.create_task(
            conn, title="external", assignee="lane", owner_kind="external",
            task_kind="ordinary", purpose="build", creation_authority="operator",
        )
        task = kb.get_task(conn, task_id)
        assert task.owner_kind == "external"
        assert task.task_kind == "ordinary"
        assert task.creation_authority == "operator"
        with pytest.raises(ValueError, match="immutable"):
            kb.assign_task(conn, task_id, "worker", owner_kind="agent")


def test_control_plane_authority_is_bound_to_runtime_profile_not_caller_string(kanban_home, monkeypatch):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_PROFILE", "worker")
    with pytest.raises(ValueError, match="runtime HERMES_PROFILE"):
        kb._require_control_authority("coordinator")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    with pytest.raises(ValueError, match="assertion"):
        kb._require_control_authority("system")
    assert kb._require_control_authority("coordinator") == "coordinator"


def test_operator_start_external_run_requires_ready_external_task_and_immutable_owner(kanban_home, monkeypatch):
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "operator")
    with kb.connect() as conn:
        agent_task = kb.create_task(conn, title="agent", assignee="operator")
        external_task = kb.create_task(
            conn, title="external", assignee="external-lane", owner_kind="external",
        )
        with pytest.raises(ValueError, match="external run requires"):
            kb.start_external_run(conn, agent_task, owner="operator", external_id="agent-bypass")
        with pytest.raises(ValueError, match="external run requires"):
            kb.start_external_run(conn, external_task, owner="other-lane", external_id="owner-bypass")
        run = kb.start_external_run(
            conn, external_task, owner="external-lane", external_id="allowed",
        )
        assert run.owner == "external-lane"


def test_only_agent_owned_tasks_can_be_claimed_or_dispatch_spawned(kanban_home, monkeypatch):
    """An installed-profile assignee never makes an external/no_agent card dispatchable."""
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "worker")
    with kb.connect() as conn:
        agent = kb.create_task(conn, title="agent", assignee="worker", owner_kind="agent")
        external = kb.create_task(conn, title="external", assignee="worker", owner_kind="external")
        no_agent = kb.create_task(conn, title="no agent", assignee="worker", owner_kind="no_agent")

        assert kb.claim_task(conn, external) is None
        assert kb.claim_task(conn, no_agent) is None
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (external,))
        assert kb.claim_review_task(conn, external) is None
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (external,))
        assert kb.claim_task(conn, agent) is not None
        kb.complete_task(conn, agent, result="done")

        spawned = []
        result = kb.dispatch_once(conn, spawn_fn=lambda task, *_: spawned.append(task.id) or 1)
        assert spawned == []
        assert result.spawned == []
        external_task = kb.get_task(conn, external)
        no_agent_task = kb.get_task(conn, no_agent)
        assert external_task is not None and external_task.status == "ready"
        assert no_agent_task is not None and no_agent_task.status == "ready"


def test_gate_task_requires_configured_authority_at_db_boundary(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "worker")
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="runtime HERMES_PROFILE"):
            kb.create_task(conn, title="review", task_kind="reviewer", creation_authority="worker")


def test_wait_rejects_unknown_kind_and_empty_ref(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="bad wait", assignee="operator", owner_kind="no_agent")
        with pytest.raises(ValueError, match="wait_kind"):
            kb.set_task_wait(conn, task_id, kind="anything", ref="x")
        with pytest.raises(ValueError, match="wait_ref"):
            kb.set_task_wait(conn, task_id, kind="human", ref="")


def test_external_ownership_transfer_is_atomic_and_survives_registry_close(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="transfer", assignee="lane-a", owner_kind="external")
        run = kb.start_external_run(conn, task_id, owner="lane-a", external_id="job-1")
        assert kb.transfer_external_run_owner(conn, run.id, from_owner="lane-a", to_owner="lane-b")
        assert not kb.heartbeat_external_run(conn, run.id, owner="lane-a")
        assert kb.heartbeat_external_run(conn, run.id, owner="lane-b")

    # Reopening is the ProcessRegistry/AIAgent-close boundary: external owner
    # records are database-owned and therefore remain controllable.
    with kb.connect() as reopened:
        assert kb.finish_external_run(reopened, run.id, owner="lane-b", outcome="completed")
        assert kb.get_task(reopened, task_id).status == "done"
        assert reopened.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='external_owner_transferred'",
            (task_id,),
        ).fetchone()[0] == 1


def test_operator_external_pid_never_suppresses_heartbeat_expiry(kanban_home):
    """An operator-supplied PID is not trusted host-process identity evidence."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="external pid", assignee="operator", owner_kind="external",
        )
        run = kb.start_external_run(
            conn, task_id, owner="operator", external_id="operator-pid", pid=os.getpid(),
        )
        conn.execute("UPDATE task_runs SET last_heartbeat_at=0 WHERE id=?", (run.id,))
        assert kb.reconcile_stale_external_runs(conn, stale_after_seconds=1) == 1
        reconciled = kb.get_external_run(conn, run.id)
        assert reconciled is not None and reconciled.outcome == "lost"
        assert kb.get_task(conn, task_id).status == "blocked"


def test_stale_external_reconciliation_exhausts_retry_once(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="stale", assignee="lane", owner_kind="external")
        run = kb.start_external_run(
            conn, task_id, owner="lane", external_id="stale-1", max_retries=1,
            on_failure="retry",
        )
        conn.execute("UPDATE task_runs SET last_heartbeat_at=0 WHERE id=?", (run.id,))
        assert kb.reconcile_stale_external_runs(conn, stale_after_seconds=1) == 1
        assert kb.get_task(conn, task_id).status == "blocked"
        # The terminal run's CAS condition makes repeated reconciler ticks inert.
        assert kb.reconcile_stale_external_runs(conn, stale_after_seconds=1) == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='external_retry_exhausted'",
            (task_id,),
        ).fetchone()[0] == 1



def test_stale_external_block_policy_blocks_on_its_first_lost_run(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="stale block", assignee="lane", owner_kind="external")
        run = kb.start_external_run(
            conn, task_id, owner="lane", external_id="stale-block", max_retries=9,
            on_failure="block",
        )
        conn.execute("UPDATE task_runs SET last_heartbeat_at=0 WHERE id=?", (run.id,))
        assert kb.reconcile_stale_external_runs(conn, stale_after_seconds=1) == 1
        assert kb.get_task(conn, task_id).status == "blocked"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='external_lost'", (task_id,)
        ).fetchone()[0] == 1


def test_direct_completion_cannot_settle_a_live_external_owned_run(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="external only", assignee="lane", owner_kind="external")
        run = kb.start_external_run(conn, task_id, owner="lane", external_id="external-only")
        assert kb.complete_task(conn, task_id, result="bypass") is False
        live = kb.get_task(conn, task_id)
        assert live is not None and live.status == "running" and live.current_run_id == run.id
        assert kb.finish_external_run(conn, run.id, owner="lane", outcome="completed") is True
        assert kb.get_task(conn, task_id).status == "done"


def test_candidate_and_manifest_bind_attachment_bytes(kanban_home, tmp_path):
    """A frozen evidence receipt is tied to one immutable candidate and blob."""
    payload = b"verified report"
    blob = tmp_path / "report.txt"
    blob.write_bytes(payload)
    digest = __import__("hashlib").sha256(payload).hexdigest()
    sha = "a" * 40
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="candidate")
        attachment_id = kb.add_attachment(
            conn, task_id, filename="report.txt", stored_path=str(blob),
            content_type="text/plain", size=len(payload), uploaded_by="test",
        )
        candidate = kb.create_candidate(conn, task_id, sha=sha)
        assert candidate["sha"] == sha
        receipt = kb.freeze_evidence_manifest(
            conn, task_id, sha=sha,
            manifest={"required_slots": ["report"], "entries": [{
                "attachment_id": attachment_id, "sha256": digest,
                "content_type": "text/plain", "decoder_valid": True,
                "decoder_metadata": {"parser": "text"}, "cardinality": 1,
                "slot": "report", "mode": "required",
            }]},
            verdict="pass", authority="coordinator",
        )
        assert receipt["sha"] == sha and receipt["manifest_hash"]



@pytest.mark.parametrize("case, message", [
    ("missing_bytes", "bytes are missing"), ("hash", "hash mismatch"),
    ("mime", "content_type"),
    ("cardinality", "cardinality"), ("slot", "slot and mode"),
    ("required_slot", "required slot"), ("candidate_task", "candidate is missing"),
])
def test_manifest_rejects_invalid_evidence_contract(kanban_home, tmp_path, case, message):
    blob = tmp_path / "evidence.bin"; blob.write_bytes(b"ok")
    with kb.connect() as conn:
        task = kb.create_task(conn, title="evidence")
        aid = kb.add_attachment(conn, task, filename="evidence.bin", stored_path=str(blob), content_type="image/png" if case == "dimensions" else "application/octet-stream", size=2)
        sha = "a" * 40
        if case == "candidate_task":
            other = kb.create_task(conn, title="other"); kb.create_candidate(conn, other, sha=sha)
        else: kb.create_candidate(conn, task, sha=sha)
        entry = {"attachment_id": aid, "sha256": hashlib.sha256(b"ok").hexdigest(), "content_type": "image/png" if case == "dimensions" else "application/octet-stream", "decoder_valid": True, "decoder_metadata": {}, "cardinality": 1, "slot": "artifact", "mode": "required"}
        manifest = {"required_slots": ["artifact"], "entries": [entry]}
        if case == "missing_bytes": blob.unlink()
        elif case == "hash": entry["sha256"] = "0" * 64
        elif case == "mime": entry["content_type"] = "text/plain"
        elif case == "decoder": entry["decoder_valid"] = False
        elif case == "metadata": entry.pop("decoder_metadata")
        elif case == "cardinality": entry["cardinality"] = 0
        elif case == "slot": entry["slot"] = ""
        elif case == "required_slot": manifest["required_slots"] = ["absent"]
        with pytest.raises(ValueError, match=message):
            kb.freeze_evidence_manifest(conn, task, sha=sha, manifest=manifest, verdict="pass", authority="coordinator")


def test_gate_verdict_rejects_stale_bindings_and_mutation(kanban_home, tmp_path, monkeypatch):
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    with kb.connect() as conn:
        task, sha, receipt = _review_wait_source(conn, tmp_path)
        gate = _review_wait_gate(conn, task)
        kb.complete_task(conn, gate, result="validated", summary="validated")
        with pytest.raises(ValueError, match="stale"):
            kb.record_gate_verdict(conn, task, gate_task_id=gate, sha="b" * 40,
                manifest_hash=receipt["manifest_hash"], verdict="pass", authority="coordinator")
        with pytest.raises(ValueError, match="stale"):
            kb.record_gate_verdict(conn, task, gate_task_id=gate, sha=sha,
                manifest_hash="0" * 64, verdict="pass", authority="coordinator")
        kb.record_gate_verdict(conn, task, gate_task_id=gate, sha=sha,
            manifest_hash=receipt["manifest_hash"], verdict="pass", authority="coordinator")
        with pytest.raises(ValueError, match="immutable"):
            kb.record_gate_verdict(conn, task, gate_task_id=gate, sha=sha,
                manifest_hash=receipt["manifest_hash"], verdict="fail", authority="coordinator")

def test_evidence_manifest_freezes_exact_sha_and_verdict(kanban_home, tmp_path):
    blob = tmp_path / "legacy-evidence.txt"
    blob.write_text("evidence", encoding="utf-8")
    digest = hashlib.sha256(blob.read_bytes()).hexdigest()
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="evidence", assignee="operator", owner_kind="no_agent")
        sha = "a" * 40
        attachment_id = kb.add_attachment(conn, task_id, filename="evidence.txt", stored_path=str(blob), content_type="text/plain", size=8)
        kb.create_candidate(conn, task_id, sha=sha)
        receipt = kb.freeze_evidence_manifest(
            conn, task_id, sha=sha, manifest={"required_slots": ["evidence"], "entries": [{"attachment_id": attachment_id, "sha256": digest, "content_type": "text/plain", "decoder_valid": True, "decoder_metadata": {}, "cardinality": 1, "slot": "evidence", "mode": "required"}]},
            verdict="pass", authority="coordinator",
        )
        assert receipt["sha"] == sha
        assert kb.get_frozen_evidence_manifest(conn, task_id) == receipt
        with pytest.raises(ValueError, match="candidate is missing"):
            kb.freeze_evidence_manifest(
                conn, task_id, sha="b" * 40, manifest={}, verdict="pass", authority="coordinator",
            )


def test_deterministic_release_barrier_requires_gates_lease_and_readback(kanban_home, tmp_path, monkeypatch):
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    blob = tmp_path / "release"; blob.write_bytes(b"release")
    sha = "a" * 40
    with kb.connect() as conn:
        task = kb.create_task(conn, title="release")
        child = kb.create_task(conn, title="dependent")
        kb.link_tasks(conn, parent_id=task, child_id=child)
        kb.block_task(conn, child, reason="waiting")
        aid = kb.add_attachment(conn, task, filename="release", stored_path=str(blob), content_type="application/octet-stream", size=7)
        kb.create_candidate(conn, task, sha=sha)
        manifest = kb.freeze_evidence_manifest(conn, task, sha=sha, manifest={"required_slots":["release"], "entries":[{"attachment_id":aid,"sha256":hashlib.sha256(b"release").hexdigest(),"content_type":"application/octet-stream","decoder_valid":True,"decoder_metadata":{},"cardinality":1,"slot":"release","mode":"required"}]}, verdict="pass", authority="coordinator")
        review = kb.create_task(conn, title="review", assignee="reviewer", owner_kind="no_agent", task_kind="reviewer", created_by_task_id=task, creation_authority="coordinator")
        security = kb.create_task(conn, title="security", assignee="security", owner_kind="no_agent", task_kind="reviewer", created_by_task_id=task, creation_authority="coordinator")
        kb.complete_task(conn, review, result="pass", summary="validated")
        kb.complete_task(conn, security, result="pass", summary="validated")
        barrier = kb.create_release_barrier(conn, task, sha=sha, manifest_hash=manifest["manifest_hash"], required_gates=[review, security], authority="coordinator")
        assert barrier["sha"] == sha
        assert kb.acquire_release_barrier_lease(conn, task, barrier="publish", owner_token="one", ttl_seconds=60)
    with kb.connect() as other:
        assert not kb.acquire_release_barrier_lease(other, task, barrier="publish", owner_token="two", ttl_seconds=60)
        with pytest.raises(ValueError, match="lease is not owned"):
            kb.reconcile_release_barrier(other, task, barrier="publish", owner_token="two")
        kb.record_gate_verdict(other, task, gate_task_id=review, sha=sha, manifest_hash=manifest["manifest_hash"], verdict="pass", authority="coordinator")
        kb.record_gate_verdict(other, task, gate_task_id=security, sha=sha, manifest_hash=manifest["manifest_hash"], verdict="pass", authority="coordinator")
        assert kb.reconcile_release_barrier(other, task, barrier="publish", owner_token="one") == "awaiting_receipt"
        with pytest.raises(ValueError, match="requires a recorded delivery"):
            kb.record_release_readback(
                other, task, barrier="publish", owner_token="one", readback_sha=sha,
            )
        kb.prepare_release_delivery_intent(other, task, barrier="publish", owner_token="one", target_identity="target", candidate_sha=sha, idempotency_key="release")
        kb.record_release_delivery(other, task, barrier="publish", owner_token="one", target_identity="target", delivered_sha=sha, idempotency_key="release")
        assert kb.reconcile_release_barrier(other, task, barrier="publish", owner_token="one") == "awaiting_readback"
        kb.record_release_readback(other, task, barrier="publish", owner_token="one", readback_sha=sha)
        assert kb.reconcile_release_barrier(other, task, barrier="publish", owner_token="one") == "released"
        assert kb.reconcile_release_barrier(other, task, barrier="publish", owner_token="one") == "released"
        assert other.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='release_barrier_completed'", (task,)).fetchone()[0] == 1
        assert kb.get_task(other, child).status == "ready"



def test_release_barrier_lease_readback_and_migration_contract(kanban_home, tmp_path, monkeypatch):
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    blob = tmp_path / "x"; blob.write_bytes(b"x"); sha = "c" * 40
    with kb.connect() as conn:
        task = kb.create_task(conn, title="barrier")
        child = kb.create_task(conn, title="child", initial_status="blocked")
        kb.link_tasks(conn, parent_id=task, child_id=child)
        aid = kb.add_attachment(conn, task, filename="x", stored_path=str(blob), content_type="application/octet-stream", size=1)
        kb.create_candidate(conn, task, sha=sha)
        m = kb.freeze_evidence_manifest(conn, task, sha=sha, manifest={"required_slots":["x"],"entries":[{"attachment_id":aid,"sha256":hashlib.sha256(b"x").hexdigest(),"content_type":"application/octet-stream","decoder_valid":True,"decoder_metadata":{},"cardinality":1,"slot":"x","mode":"r"}]}, verdict="pass", authority="coordinator")
        gate = kb.create_task(conn, title="gate", assignee="reviewer", owner_kind="no_agent", task_kind="reviewer", created_by_task_id=task, creation_authority="coordinator")
        kb.complete_task(conn, gate, result="fail", summary="validated")
        kb.create_release_barrier(conn, task, sha=sha, manifest_hash=m["manifest_hash"], required_gates=[gate], authority="coordinator")
        assert kb.acquire_release_barrier_lease(conn, task, barrier="publish", owner_token="a", ttl_seconds=1)
        assert kb.renew_release_barrier_lease(conn, task, barrier="publish", owner_token="a", ttl_seconds=1)
        assert not kb.renew_release_barrier_lease(conn, task, barrier="publish", owner_token="b", ttl_seconds=1)
        assert kb.release_release_barrier_lease(conn, task, barrier="publish", owner_token="a")
        assert kb.acquire_release_barrier_lease(conn, task, barrier="publish", owner_token="b", ttl_seconds=1)
        with pytest.raises(ValueError, match="lease is not owned"): kb.record_release_delivery(conn, task, barrier="publish", owner_token="a", target_identity="t", delivered_sha=sha, idempotency_key="x")
        kb.record_gate_verdict(conn, task, gate_task_id=gate, sha=sha, manifest_hash=m["manifest_hash"], verdict="fail", authority="coordinator")
        assert kb.reconcile_release_barrier(conn, task, barrier="publish", owner_token="b") == "rejected"
        assert kb.get_release_barrier(conn, task)["sha"] == sha


def test_release_barrier_returns_the_same_durable_receipt_once(kanban_home, monkeypatch):
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="release", assignee="operator")
        first = kb.release_barrier(conn, task_id, barrier="handoff", authority="coordinator")
        assert kb.release_barrier(conn, task_id, barrier="handoff", authority="coordinator") == first
        assert kb.read_release_barrier(conn, task_id, barrier="handoff") == first
        with pytest.raises(ValueError, match="authority"):
            kb.release_barrier(conn, task_id, barrier="handoff", authority="other")


def test_typed_wait_ends_claimed_run_and_scopes_wait_event_to_it(kanban_home):
    """Parking active work closes its authoritative attempt before it is blocked."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="wait from running", assignee="worker", owner_kind="agent",
        )
        claimed = kb.claim_task(conn, task_id, claimer="worker:one")
        assert claimed is not None
        run_id = claimed.current_run_id
        assert run_id is not None

        kb.set_task_wait(
            conn, task_id, kind="human", ref="decision:ship",
            expected_run_id=run_id,
        )

        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.current_run_id is None
        assert task.claim_lock is None
        assert task.claim_expires is None
        assert task.worker_pid is None
        run = conn.execute(
            "SELECT status, outcome, summary, metadata, ended_at, claim_lock "
            "FROM task_runs WHERE id=?", (run_id,),
        ).fetchone()
        assert run["status"] == "blocked"
        assert run["outcome"] == "blocked"
        assert "human" in run["summary"]
        assert "decision:ship" in run["summary"]
        assert {"wait_kind": "human", "wait_ref": "decision:ship"}.items() <= json.loads(run["metadata"]).items()
        assert run["ended_at"] is not None
        assert run["claim_lock"] is None
        event = conn.execute(
            "SELECT run_id, payload FROM task_events WHERE task_id=? AND kind='wait_set'",
            (task_id,),
        ).fetchone()
        assert event["run_id"] == run_id
        assert json.loads(event["payload"]) == {"kind": "human", "ref": "decision:ship"}
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND ended_at IS NULL",
            (task_id,),
        ).fetchone()[0] == 0


def test_typed_wait_records_ready_task_without_fabricating_live_ownership(kanban_home):
    """A never-claimed task gets only an ended audit record when parked."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="wait from ready", assignee="operator", owner_kind="no_agent",
        )

        assert kb.set_task_wait(conn, task_id, kind="human", ref="decision:ready")

        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "blocked"
        assert task.current_run_id is None
        assert task.claim_lock is None
        run = conn.execute(
            "SELECT status, outcome, claim_lock, worker_pid, started_at, ended_at "
            "FROM task_runs WHERE task_id=?", (task_id,),
        ).fetchone()
        assert run["status"] == run["outcome"] == "blocked"
        assert run["claim_lock"] is None and run["worker_pid"] is None
        assert run["started_at"] == run["ended_at"]
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND ended_at IS NULL", (task_id,),
        ).fetchone()[0] == 0


def test_typed_wait_closes_current_run_when_task_is_in_review(kanban_home):
    """The review status is also a valid live-run wait handoff boundary."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="wait from review", assignee="worker", owner_kind="agent",
        )
        claimed = kb.claim_task(conn, task_id, claimer="worker:review")
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))

        assert kb.set_task_wait(
            conn, task_id, kind="human", ref="decision:review",
            expected_run_id=run_id,
        )

        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "blocked"
        assert task.current_run_id is None
        run = conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id=?", (run_id,),
        ).fetchone()
        assert run["status"] == "blocked"
        assert run["outcome"] == "blocked"
        assert run["ended_at"] is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND ended_at IS NULL", (task_id,),
        ).fetchone()[0] == 0


def test_typed_wait_rejects_stale_run_and_resume_claims_a_fresh_run(kanban_home):
    """A stale worker cannot park a newer run, and resumed work gets a new run."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="wait race", assignee="worker", owner_kind="agent",
        )
        first = kb.claim_task(conn, task_id, claimer="worker:first")
        assert first is not None and first.current_run_id is not None
        first_run_id = first.current_run_id
        assert kb.set_task_wait(
            conn, task_id, kind="human", ref="decision:race", expected_run_id=first_run_id,
        )
        assert kb.record_wait_receipt(
            conn, kind="human", ref="decision:race", authority="coordinator", verdict="approved",
        )
        assert kb.resume_task_wait(
            conn, task_id, authority="coordinator", receipt="decision:race")
        second = kb.claim_task(conn, task_id, claimer="worker:second")
        assert second is not None and second.current_run_id is not None
        second_run_id = second.current_run_id
        assert second_run_id != first_run_id

        assert not kb.set_task_wait(
            conn, task_id, kind="human", ref="decision:race", expected_run_id=first_run_id,
        )
        live = kb.get_task(conn, task_id)
        assert live is not None and live.current_run_id == second_run_id
        assert live.status == "running"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND ended_at IS NULL", (task_id,),
        ).fetchone()[0] == 1

        assert kb.set_task_wait(
            conn, task_id, kind="human", ref="decision:race", expected_run_id=second_run_id,
        )
        assert kb.resume_task_wait(
            conn, task_id, authority="coordinator", receipt="decision:race")
        final = kb.get_task(conn, task_id)
        assert final is not None and final.current_run_id is None and final.status == "ready"
        wait_events = conn.execute(
            "SELECT run_id FROM task_events WHERE task_id=? AND kind='wait_set' ORDER BY id",
            (task_id,),
        ).fetchall()
        assert [event["run_id"] for event in wait_events] == [first_run_id, second_run_id]
        runs = conn.execute(
            "SELECT id, status, outcome, ended_at FROM task_runs WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        assert [run["id"] for run in runs] == [first_run_id, second_run_id]
        assert all(run["status"] == run["outcome"] == "blocked" for run in runs)
        assert all(run["ended_at"] is not None for run in runs)


@pytest.mark.parametrize(
    ("kind", "ref"),
    [
        ("dependency", "missing-parent"),
        ("capability", "camera"),
        ("human", "ship"),
        ("external_process", "build-42"),
        ("timer", "2099-01-01T00:00:00"),
        ("review", "reviewer:alice"),
    ],
)
def test_typed_wait_rejects_noncanonical_or_unbound_refs(kanban_home, kind, ref):
    """A wait is not valid until its kind-specific durable target is bound."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=f"invalid {kind}", assignee="operator", owner_kind="no_agent")
        with pytest.raises(ValueError):
            kb.set_task_wait(conn, task_id, kind=kind, ref=ref)


def test_dispatch_reconciles_only_matching_typed_wait_receipts_once(kanban_home):
    """Two dispatcher ticks resume precisely eligible waits and emit one event."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="operator", owner_kind="no_agent")
        child = kb.create_task(conn, title="child", assignee="operator", owner_kind="no_agent", initial_status="running")
        kb.link_tasks(conn, parent, child)
        # A typed dependency wait may park an actively routed child; simulate
        # that pre-dispatch state without bypassing the wait API itself.
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        kb.set_task_wait(conn, child, kind="dependency", ref=parent)

        external = kb.create_task(conn, title="external", assignee="operator", owner_kind="external")
        run = kb.start_external_run(conn, external, owner="operator", external_id="build-42")
        waiter = kb.create_task(conn, title="wait external", assignee="operator", owner_kind="no_agent")
        kb.set_task_wait(conn, waiter, kind="external_process", ref=f"run:{run.id}")

        capability = kb.create_task(conn, title="capability", assignee="operator", owner_kind="no_agent")
        kb.set_task_wait(conn, capability, kind="capability", ref="cap:camera")
        assert not kb.resume_task_wait(conn, capability, authority="human", receipt="cap:camera")
        assert kb.record_wait_receipt(conn, kind="capability", ref="cap:camera", authority="coordinator", verdict="available")

        assert kb.finish_external_run(conn, run.id, owner="operator", outcome="completed")
        assert kb.complete_task(conn, parent, result="done")
        kb.dispatch_once(conn, spawn_fn=lambda *_: None)
        kb.dispatch_once(conn, spawn_fn=lambda *_: None)

        assert kb.get_task(conn, child).status == "ready"
        assert kb.get_task(conn, waiter).status == "ready"
        assert kb.get_task(conn, capability).status == "ready"
        for task_id in (child, waiter, capability):
            assert conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='wait_resumed'",
                (task_id,),
            ).fetchone()[0] == 1


def test_manual_resume_requires_matching_explicit_receipt(kanban_home, tmp_path, monkeypatch):
    """Generic authority labels cannot resume a human or review wait."""
    with kb.connect() as conn:
        human = kb.create_task(conn, title="approval", assignee="operator", owner_kind="no_agent")
        kb.set_task_wait(conn, human, kind="human", ref="decision:ship")
        assert not kb.resume_task_wait(conn, human, authority="human", receipt="decision:other")
        assert kb.record_wait_receipt(conn, kind="human", ref="decision:ship", authority="coordinator", verdict="approved")
        assert kb.resume_task_wait(conn, human, authority="coordinator", receipt="decision:ship")

        (kanban_home / "config.yaml").write_text(
            "kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_PROFILE", "coordinator")
        review, sha, manifest = _review_wait_source(conn, tmp_path)
        gate_task = _review_wait_gate(conn, review)
        review_ref = f"gate:{gate_task}"
        kb.set_task_wait(conn, review, kind="review", ref=review_ref)
        assert not kb.resume_task_wait(conn, review, authority="bob", receipt=review_ref)
        kb.complete_task(conn, gate_task, result="approved")
        kb.record_gate_verdict(
            conn, review, gate_task_id=gate_task, sha=sha,
            manifest_hash=manifest["manifest_hash"], verdict="pass", authority="coordinator",
        )
        assert kb.resume_task_wait(conn, review, authority="coordinator", receipt=review_ref)


def _review_wait_source(conn, tmp_path):
    """Create a source with an immutable candidate/manifest for gate verdicts."""
    source = kb.create_task(
        conn, title="implementation", assignee="implementer", owner_kind="no_agent",
    )
    payload = b"review evidence"
    blob = tmp_path / f"{source}.txt"
    blob.write_bytes(payload)
    attachment = kb.add_attachment(
        conn, source, filename="review.txt", stored_path=str(blob),
        content_type="text/plain", size=len(payload),
    )
    sha = "c" * 40
    kb.create_candidate(conn, source, sha=sha)
    receipt = kb.freeze_evidence_manifest(
        conn, source, sha=sha,
        manifest={"required_slots": ["review"], "entries": [{
            "attachment_id": attachment, "sha256": hashlib.sha256(payload).hexdigest(),
            "content_type": "text/plain", "decoder_valid": True,
            "decoder_metadata": {}, "cardinality": 1, "slot": "review", "mode": "required",
        }]}, verdict="pass", authority="coordinator",
    )
    return source, sha, receipt


def _review_wait_gate(conn, source, title="review gate"):
    return kb.create_task(
        conn, title=title, assignee="independent-reviewer", owner_kind="no_agent",
        task_kind="reviewer", created_by_task_id=source,
        creation_authority="coordinator",
    )


def test_review_wait_canonical_gate_identity_stays_pending_until_exact_pass(
    kanban_home, tmp_path, monkeypatch,
):
    """A review wait binds only its independently-created gate task ID."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    with kb.connect() as conn:
        source, sha, receipt = _review_wait_source(conn, tmp_path)
        gate = _review_wait_gate(conn, source)
        ref = f"gate:{gate}"

        kb.set_task_wait(conn, source, kind="review", ref=ref)
        assert kb.get_task(conn, source).wait_ref == ref
        assert kb.reconcile_task_waits(conn) == 0
        assert kb.get_task(conn, source).status == "blocked"

        kb.complete_task(conn, gate, result="approved")
        kb.record_gate_verdict(
            conn, source, gate_task_id=gate, sha=sha,
            manifest_hash=receipt["manifest_hash"], verdict="pass", authority="coordinator",
        )
        # Two complete dispatcher ticks must consume this exact PASS only once.
        kb.dispatch_once(conn, spawn_fn=lambda *_: None)
        kb.dispatch_once(conn, spawn_fn=lambda *_: None)
        assert kb.get_task(conn, source).status == "ready"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='wait_resumed'", (source,),
        ).fetchone()[0] == 1


def test_review_wait_rejects_foreign_gate_and_ignores_wrong_gate_verdict(
    kanban_home, tmp_path, monkeypatch,
):
    """A foreign or merely different valid gate cannot release the source wait."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    with kb.connect() as conn:
        source, sha, receipt = _review_wait_source(conn, tmp_path)
        foreign = kb.create_task(
            conn, title="foreign", assignee="independent-reviewer", owner_kind="no_agent",
            task_kind="ordinary", creation_authority="coordinator",
        )
        with pytest.raises(ValueError, match="gate task"):
            kb.set_task_wait(conn, source, kind="review", ref=f"gate:{foreign}")

        expected_gate = _review_wait_gate(conn, source, "expected")
        wrong_gate = _review_wait_gate(conn, source, "wrong")
        kb.set_task_wait(conn, source, kind="review", ref=f"gate:{expected_gate}")
        kb.complete_task(conn, wrong_gate, result="approved")
        kb.record_gate_verdict(
            conn, source, gate_task_id=wrong_gate, sha=sha,
            manifest_hash=receipt["manifest_hash"], verdict="pass", authority="coordinator",
        )
        assert kb.reconcile_task_waits(conn) == 0
        assert kb.get_task(conn, source).status == "blocked"


def test_review_wait_fail_verdict_does_not_auto_resume(kanban_home, tmp_path, monkeypatch):
    """An exact gate FAIL is terminal evidence, never an auto-resume trigger."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    with kb.connect() as conn:
        source, sha, receipt = _review_wait_source(conn, tmp_path)
        gate = _review_wait_gate(conn, source)
        kb.set_task_wait(conn, source, kind="review", ref=f"gate:{gate}")
        kb.complete_task(conn, gate, result="changes requested")
        kb.record_gate_verdict(
            conn, source, gate_task_id=gate, sha=sha,
            manifest_hash=receipt["manifest_hash"], verdict="fail", authority="coordinator",
        )
        assert kb.reconcile_task_waits(conn) == 0
        assert kb.get_task(conn, source).status == "blocked"


def test_image_evidence_is_decoded_and_canonicalized_from_stored_bytes(kanban_home, tmp_path):
    """Image MIME and dimensions come from a verified Pillow decode, never caller claims."""
    encoded = io.BytesIO()
    Image.new("RGB", (2, 3), "red").save(encoded, format="PNG")
    png = encoded.getvalue()
    sha = "a" * 40

    with kb.connect() as conn:
        task = kb.create_task(conn, title="decoded image evidence")
        kb.create_candidate(conn, task, sha=sha)
        for name, payload, declared_type, dimensions, error in (
            ("corrupt.png", b"not a png", "image/png", [2, 3], "decode"),
            ("spoofed-mime.png", png, "image/jpeg", [2, 3], "MIME"),
            ("spoofed-dimensions.png", png, "image/png", [1, 1], "dimensions"),
        ):
            blob = tmp_path / name
            blob.write_bytes(payload)
            attachment = kb.add_attachment(
                conn, task, filename=name, stored_path=str(blob),
                content_type=declared_type, size=len(payload),
            )
            manifest = {"required_slots": [name], "entries": [{
                "attachment_id": attachment,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "content_type": declared_type,
                "decoder_valid": True,
                "decoder_metadata": {"untrusted": True},
                "dimensions": dimensions,
                "cardinality": 1,
                "slot": name,
                "mode": "required",
            }]}
            with pytest.raises(ValueError, match=error):
                kb.freeze_evidence_manifest(
                    conn, task, sha=sha, manifest=manifest, verdict="pass", authority="coordinator",
                )

        valid = tmp_path / "valid.png"
        valid.write_bytes(png)
        attachment = kb.add_attachment(
            conn, task, filename="valid.png", stored_path=str(valid),
            content_type="image/png", size=len(png),
        )
        receipt = kb.freeze_evidence_manifest(
            conn, task, sha=sha,
            manifest={"required_slots": ["screenshot"], "entries": [{
                "attachment_id": attachment, "sha256": hashlib.sha256(png).hexdigest(),
                "content_type": "image/png", "decoder_valid": True,
                "decoder_metadata": {"untrusted": True}, "dimensions": [2, 3],
                "cardinality": 1, "slot": "screenshot", "mode": "required",
            }]},
            verdict="pass", authority="coordinator",
        )
        entry = receipt["manifest"]["entries"][0]
        assert entry["content_type"] == "image/png"
        assert entry["dimensions"] == [2, 3]
        assert entry["decoder_metadata"] == {"format": "PNG", "height": 3, "width": 2}


def test_gate_verdict_requires_an_independent_configured_terminal_gate_task(kanban_home, tmp_path, monkeypatch):
    """A gate is an immutable reviewer/visualqa task ID, not a caller-chosen label."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    blob = tmp_path / "report.txt"
    blob.write_bytes(b"report")
    sha = "b" * 40
    with kb.connect() as conn:
        source = kb.create_task(conn, title="implementation", assignee="implementer", owner_kind="no_agent")
        attachment = kb.add_attachment(conn, source, filename="report.txt", stored_path=str(blob), content_type="text/plain", size=6)
        kb.create_candidate(conn, source, sha=sha)
        receipt = kb.freeze_evidence_manifest(
            conn, source, sha=sha,
            manifest={"required_slots": ["report"], "entries": [{
                "attachment_id": attachment, "sha256": hashlib.sha256(b"report").hexdigest(),
                "content_type": "text/plain", "decoder_valid": True, "decoder_metadata": {},
                "cardinality": 1, "slot": "report", "mode": "required",
            }]}, verdict="pass", authority="coordinator",
        )
        foreign = kb.create_task(conn, title="foreign", assignee="reviewer", owner_kind="no_agent", task_kind="ordinary", creation_authority="coordinator")
        self_review = kb.create_task(conn, title="self", assignee="implementer", owner_kind="no_agent", task_kind="reviewer", created_by_task_id=source, creation_authority="coordinator")
        independent = kb.create_task(conn, title="valid", assignee="reviewer", owner_kind="no_agent", task_kind="visualqa", created_by_task_id=source, creation_authority="coordinator")
        kb.complete_task(conn, independent, result="validated")

        for gate_task_id, error in (("arbitrary-gate-name", "gate task"), (foreign, "gate task"), (self_review, "gate task")):
            with pytest.raises(ValueError, match=error):
                kb.record_gate_verdict(conn, source, gate_task_id=gate_task_id, sha=sha, manifest_hash=receipt["manifest_hash"], verdict="pass", authority="coordinator")
        with pytest.raises(ValueError, match="authority assertion"):
            kb.record_gate_verdict(conn, source, gate_task_id=independent, sha=sha, manifest_hash=receipt["manifest_hash"], verdict="pass", authority="spoofed")

        verdict = kb.record_gate_verdict(conn, source, gate_task_id=independent, sha=sha, manifest_hash=receipt["manifest_hash"], verdict="pass", authority="coordinator")
        assert verdict["gate_task_id"] == independent
        assert verdict["sha"] == sha
        barrier = kb.create_release_barrier(conn, source, sha=sha, manifest_hash=receipt["manifest_hash"], required_gates=[independent], authority="coordinator")
        assert barrier["required_gates"] == [independent]
        with pytest.raises(ValueError, match="authority assertion"):
            kb.create_release_barrier(conn, source, sha=sha, manifest_hash=receipt["manifest_hash"], required_gates=[independent], authority="coordinator-spoof")
