"""Strict candidate and mandatory-gate provenance contracts."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ["git", "init", "-q", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "test@example.test"],
        ["git", "-C", str(repo), "config", "user.name", "Test"],
    ):
        subprocess.run(command, check=True)
    (repo / "artifact.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "candidate"], check=True)
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    return repo, sha


def _attested_candidate(conn, task_id: str, sha: str, monkeypatch) -> dict:
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    return kb.create_candidate(
        conn, task_id, sha=sha,
        source_receipt={"candidate_sha": sha, "subject": "scratch artifact", "provenance": "trusted build receipt", "producer_profile": "implementer"},
    )


def _manifest(conn, task_id: str, sha: str, tmp_path: Path) -> dict:
    evidence = tmp_path / f"{task_id}.txt"
    evidence.write_text("evidence", encoding="utf-8")
    attachment = kb.add_attachment(conn, task_id, filename="evidence.txt", stored_path=str(evidence), content_type="text/plain", size=8)
    return kb.freeze_evidence_manifest(conn, task_id, sha=sha, manifest={"required_slots": ["evidence"], "entries": [{"attachment_id": attachment, "sha256": hashlib.sha256(b"evidence").hexdigest(), "content_type": "text/plain", "cardinality": 1, "slot": "evidence", "mode": "required"}]}, verdict="pass", authority="coordinator")


def test_gate_completion_requires_active_run_and_immutable_worker_profile(kanban_home, tmp_path, monkeypatch):
    """Metadata cannot let CLI/dashboard callers forge a mandatory gate completion."""
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in {"implementer", "reviewer"})
    sha = "c" * 40
    with kb.connect() as conn:
        monkeypatch.setenv("HERMES_PROFILE", "coordinator")
        source = kb.create_task(conn, title="source", assignee="implementer", owner_kind="agent")
        _attested_candidate(conn, source, sha, monkeypatch)
        manifest = _manifest(conn, source, sha, tmp_path)
        gate = kb.create_task(conn, title="gate", assignee="reviewer", owner_kind="agent", task_kind="reviewer", created_by_task_id=source, creation_authority="coordinator", gate_candidate_sha=sha, gate_manifest_hash=manifest["manifest_hash"])
        run = kb.claim_task(conn, gate)
        assert run is not None
        metadata = {"candidate_sha": sha, "manifest_hash": manifest["manifest_hash"]}
        assert not kb.complete_task(conn, gate, result="forged", metadata=metadata)
        assert not kb.complete_task(conn, gate, result="forged", metadata=metadata, expected_run_id=run.current_run_id)
        monkeypatch.setenv("HERMES_PROFILE", "reviewer")
        assert kb.complete_task(conn, gate, result="pass", metadata=metadata, expected_run_id=run.current_run_id)


def test_evidence_freeze_rejects_authority_spoofing_runtime_profile(kanban_home, tmp_path, monkeypatch):
    """The receipt cannot record an arbitrary authority string."""
    sha = "e" * 40
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8",
    )
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "implementer")
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("evidence", encoding="utf-8")
    with kb.connect() as conn:
        task = kb.create_task(conn, title="source", assignee="implementer", owner_kind="agent")
        monkeypatch.setenv("HERMES_PROFILE", "coordinator")
        kb.create_candidate(conn, task, sha=sha, source_receipt={
            "candidate_sha": sha, "subject": "scratch artifact", "provenance": "trusted build receipt", "producer_profile": "implementer",
        })
        attachment = kb.add_attachment(
            conn, task, filename="evidence.txt", stored_path=str(evidence),
            content_type="text/plain", size=8, uploaded_by="implementer",
        )
        monkeypatch.setenv("HERMES_PROFILE", "implementer")
        with pytest.raises(ValueError, match="assertion"):
            kb.freeze_evidence_manifest(
                conn, task, sha=sha,
                manifest={"required_slots": ["evidence"], "entries": [{
                    "attachment_id": attachment,
                    "sha256": hashlib.sha256(b"evidence").hexdigest(),
                    "content_type": "text/plain", "cardinality": 1,
                    "slot": "evidence", "mode": "required",
                }]},
                verdict="pass", authority="reviewer",
            )
        monkeypatch.setenv("HERMES_PROFILE", "reviewer")
        with pytest.raises(ValueError, match="source agent assignee or configured coordinator"):
            kb.freeze_evidence_manifest(
                conn, task, sha=sha,
                manifest={"required_slots": ["evidence"], "entries": [{
                    "attachment_id": attachment,
                    "sha256": hashlib.sha256(b"evidence").hexdigest(),
                    "content_type": "text/plain", "cardinality": 1,
                    "slot": "evidence", "mode": "required",
                }]},
                verdict="pass", authority="reviewer",
            )
        monkeypatch.delenv("HERMES_PROFILE")
        with pytest.raises(ValueError, match="requires runtime HERMES_PROFILE"):
            kb.freeze_evidence_manifest(
                conn, task, sha=sha,
                manifest={"required_slots": ["evidence"], "entries": [{
                    "attachment_id": attachment,
                    "sha256": hashlib.sha256(b"evidence").hexdigest(),
                    "content_type": "text/plain", "cardinality": 1,
                    "slot": "evidence", "mode": "required",
                }]},
                verdict="pass", authority="",
            )


def test_evidence_freeze_persists_canonical_runtime_for_producer_and_coordinator(kanban_home, tmp_path, monkeypatch):
    """Either authorized runtime identity becomes the immutable receipt authority."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8",
    )
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "implementer")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    with kb.connect() as conn:
        receipts = []
        for suffix, runtime in (("a", "implementer"), ("b", "coordinator")):
            payload = f"evidence-{suffix}".encode()
            evidence = tmp_path / f"evidence-{suffix}.txt"
            evidence.write_bytes(payload)
            task = kb.create_task(conn, title=f"source-{suffix}", assignee="implementer", owner_kind="agent")
            sha = suffix * 40
            kb.create_candidate(conn, task, sha=sha, source_receipt={
                "candidate_sha": sha, "subject": "scratch artifact", "provenance": "trusted build receipt", "producer_profile": "implementer",
            })
            attachment = kb.add_attachment(
                conn, task, filename=evidence.name, stored_path=str(evidence),
                content_type="text/plain", size=len(payload), uploaded_by="implementer",
            )
            monkeypatch.setenv("HERMES_PROFILE", runtime)
            receipt = kb.freeze_evidence_manifest(
                conn, task, sha=sha,
                manifest={"required_slots": ["evidence"], "entries": [{
                    "attachment_id": attachment, "sha256": hashlib.sha256(payload).hexdigest(),
                    "content_type": "text/plain", "cardinality": 1,
                    "slot": "evidence", "mode": "required",
                }]},
                verdict="pass", authority=runtime,
            )
            assert receipt["authority"] == runtime
            receipts.append((task, receipt))
            monkeypatch.setenv("HERMES_PROFILE", "coordinator")
        stored_authorities = []
        for task, _receipt in receipts:
            stored = kb.get_frozen_evidence_manifest(conn, task)
            assert stored is not None
            stored_authorities.append(stored["authority"])
    assert stored_authorities == ["implementer", "coordinator"]


def test_worktree_candidate_proves_clean_head_and_canonical_workspace(kanban_home, tmp_path):
    repo, sha = _git_repo(tmp_path)
    with kb.connect() as conn:
        task = kb.create_task(conn, title="implementation", workspace_kind="worktree", workspace_path=str(repo))
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, metadata, started_at, ended_at) VALUES (?, ?, 'done', 'completed', ?, 1, 2)",
            (task, "implementer", json.dumps({"candidate_sha": sha})),
        )
        candidate = kb.create_candidate(conn, task, sha=sha)
        assert candidate["source_kind"] == "git"
        assert candidate["source_provenance"] == {"head": sha, "workspace_path": str(repo.resolve())}
        (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
        with pytest.raises(ValueError, match="clean"):
            kb.create_candidate(conn, task, sha=sha)
        (repo / "dirty.txt").unlink()
        with pytest.raises(ValueError, match="HEAD"):
            kb.create_candidate(conn, task, sha="a" * 40)


def test_worktree_candidate_binds_implementer_to_exact_completed_producing_run(
    kanban_home, tmp_path, monkeypatch,
):
    """Later runs and mutable assignees cannot misattribute a reset candidate."""
    repo, sha1 = _git_repo(tmp_path)
    (repo / "artifact.txt").write_text("candidate two\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-am", "candidate two", "-q"], check=True)
    sha2 = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in {"producer-a", "producer-b"})
    with kb.connect() as conn:
        task = kb.create_task(
            conn, title="implementation", assignee="producer-a", owner_kind="agent",
            workspace_kind="worktree", workspace_path=str(repo),
        )
        first = kb.claim_task(conn, task, claimer="producer-a:first")
        assert first is not None
        assert kb.complete_task(
            conn, task, result="SHA1", summary="produced SHA1",
            metadata={"candidate_sha": sha1}, expected_run_id=first.current_run_id,
        )
        from plugins.kanban.dashboard.plugin_api import _set_status_direct
        assert _set_status_direct(conn, task, "ready")
        assert kb.assign_task(conn, task, "producer-b", owner_kind="agent")
        second = kb.claim_task(conn, task, claimer="producer-b:second")
        assert second is not None
        assert kb.complete_task(
            conn, task, result="SHA2", summary="produced SHA2",
            metadata={"candidate_sha": sha2}, expected_run_id=second.current_run_id,
        )
        subprocess.run(["git", "-C", str(repo), "reset", "--hard", sha1], check=True)
        candidate = kb.create_candidate(conn, task, sha=sha1)
        assert candidate["implementer"] == "producer-a"
        (repo / "artifact.txt").write_text("candidate three\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "commit", "-am", "candidate three", "-q"], check=True)
        sha3 = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        with pytest.raises(ValueError, match="completed producing run"):
            kb.create_candidate(conn, task, sha=sha3)


def test_scratch_candidate_requires_coordinator_attestation_and_hash(kanban_home, tmp_path, monkeypatch):
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    sha = "a" * 40
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    with kb.connect() as conn:
        task = kb.create_task(conn, title="scratch")
        with pytest.raises(ValueError, match="source_receipt"):
            kb.create_candidate(conn, task, sha=sha)
        monkeypatch.setenv("HERMES_PROFILE", "attacker")
        with pytest.raises(ValueError, match="configured coordinator"):
            kb.create_candidate(conn, task, sha=sha, source_receipt={"candidate_sha": sha, "subject": "x", "provenance": "x", "producer_profile": "implementer"})
        candidate = _attested_candidate(conn, task, sha, monkeypatch)
        assert candidate["source_kind"] == "attested"
        assert candidate["source_hash"] == hashlib.sha256(json.dumps(candidate["source_provenance"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_gate_is_created_only_with_frozen_exact_source_bindings(kanban_home, tmp_path, monkeypatch):
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in {"implementer", "reviewer"})
    sha = "b" * 40
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source", assignee="implementer", owner_kind="agent")
        _attested_candidate(conn, source, sha, monkeypatch)
        manifest = _manifest(conn, source, sha, tmp_path)
        gate = kb.create_task(conn, title="gate", assignee="reviewer", owner_kind="agent", task_kind="reviewer", created_by_task_id=source, creation_authority="coordinator", gate_candidate_sha=sha, gate_manifest_hash=manifest["manifest_hash"])
        created = kb.get_task(conn, gate)
        assert (created.gate_candidate_sha, created.gate_manifest_hash) == (sha, manifest["manifest_hash"])
        with pytest.raises(ValueError, match="frozen"):
            kb.create_task(conn, title="wrong", assignee="reviewer", owner_kind="agent", task_kind="reviewer", created_by_task_id=source, creation_authority="coordinator", gate_candidate_sha="c" * 40, gate_manifest_hash=manifest["manifest_hash"])
        with pytest.raises(ValueError, match="owner_kind"):
            kb.create_task(conn, title="external", assignee="reviewer", owner_kind="no_agent", task_kind="reviewer", created_by_task_id=source, creation_authority="coordinator", gate_candidate_sha=sha, gate_manifest_hash=manifest["manifest_hash"])


def test_verdict_requires_exact_bound_terminal_run_metadata(kanban_home, tmp_path, monkeypatch):
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in {"implementer", "reviewer"})
    sha = "d" * 40
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source", assignee="implementer", owner_kind="agent")
        _attested_candidate(conn, source, sha, monkeypatch)
        manifest = _manifest(conn, source, sha, tmp_path)
        gate = kb.create_task(conn, title="gate", assignee="reviewer", owner_kind="agent", task_kind="reviewer", created_by_task_id=source, creation_authority="coordinator", gate_candidate_sha=sha, gate_manifest_hash=manifest["manifest_hash"])
        run = kb.claim_task(conn, gate)
        assert run is not None
        monkeypatch.setenv("HERMES_PROFILE", "reviewer")
        assert kb.complete_task(conn, gate, result="pass", summary="validated", metadata={"candidate_sha": "e" * 40, "manifest_hash": manifest["manifest_hash"]}, expected_run_id=run.current_run_id)
        monkeypatch.setenv("HERMES_PROFILE", "coordinator")
        with pytest.raises(ValueError, match="run metadata"):
            kb.record_gate_verdict(conn, source, gate_task_id=gate, sha=sha, manifest_hash=manifest["manifest_hash"], verdict="pass", authority="coordinator")


def test_verdict_rejects_reopened_gate_when_latest_completed_run_has_wrong_binding(kanban_home, tmp_path, monkeypatch):
    """A prior valid pass cannot authorize a gate after its follow-up run diverges."""
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in {"implementer", "reviewer"})
    sha = "f" * 40
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source", assignee="implementer", owner_kind="agent")
        _attested_candidate(conn, source, sha, monkeypatch)
        manifest = _manifest(conn, source, sha, tmp_path)
        gate = kb.create_task(conn, title="gate", assignee="reviewer", owner_kind="agent", task_kind="reviewer", created_by_task_id=source, creation_authority="coordinator", gate_candidate_sha=sha, gate_manifest_hash=manifest["manifest_hash"])
        first = kb.claim_task(conn, gate)
        assert first is not None
        monkeypatch.setenv("HERMES_PROFILE", "reviewer")
        assert kb.complete_task(conn, gate, result="pass", summary="first pass", metadata={"candidate_sha": sha, "manifest_hash": manifest["manifest_hash"]}, expected_run_id=first.current_run_id)

        # This is the dashboard's canonical done -> ready follow-up transition.
        from plugins.kanban.dashboard.plugin_api import _set_status_direct
        assert _set_status_direct(conn, gate, "ready")
        latest = kb.claim_task(conn, gate)
        assert latest is not None
        monkeypatch.setenv("HERMES_PROFILE", "reviewer")
        assert kb.complete_task(conn, gate, result="pass", summary="follow-up", metadata={"candidate_sha": "0" * 40, "manifest_hash": manifest["manifest_hash"]}, expected_run_id=latest.current_run_id)

        monkeypatch.setenv("HERMES_PROFILE", "coordinator")
        with pytest.raises(ValueError, match="run metadata"):
            kb.record_gate_verdict(conn, source, gate_task_id=gate, sha=sha, manifest_hash=manifest["manifest_hash"], verdict="pass", authority="coordinator")


def test_release_barrier_waits_when_stored_verdict_is_stale_after_gate_reopens(kanban_home, tmp_path, monkeypatch):
    """Reconcile revalidates a stored pass against the authoritative latest gate run."""
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in {"implementer", "reviewer"})
    sha = "a" * 40
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source", assignee="implementer", owner_kind="agent")
        _attested_candidate(conn, source, sha, monkeypatch)
        manifest = _manifest(conn, source, sha, tmp_path)
        gate = kb.create_task(conn, title="gate", assignee="reviewer", owner_kind="agent", task_kind="reviewer", created_by_task_id=source, creation_authority="coordinator", gate_candidate_sha=sha, gate_manifest_hash=manifest["manifest_hash"])
        first = kb.claim_task(conn, gate)
        assert first is not None
        monkeypatch.setenv("HERMES_PROFILE", "reviewer")
        assert kb.complete_task(conn, gate, result="pass", summary="first pass", metadata={"candidate_sha": sha, "manifest_hash": manifest["manifest_hash"]}, expected_run_id=first.current_run_id)
        monkeypatch.setenv("HERMES_PROFILE", "coordinator")
        kb.create_release_barrier(conn, source, sha=sha, manifest_hash=manifest["manifest_hash"], required_gates=[gate], authority="coordinator")
        kb.record_gate_verdict(conn, source, gate_task_id=gate, sha=sha, manifest_hash=manifest["manifest_hash"], verdict="pass", authority="coordinator")
        assert kb.acquire_release_barrier_lease(conn, source, barrier="publish", owner_token="owner", ttl_seconds=60)
        kb.prepare_release_delivery_intent(conn, source, barrier="publish", owner_token="owner", target_identity="target", candidate_sha=sha, idempotency_key="stale-gate")
        kb.record_release_delivery(conn, source, barrier="publish", owner_token="owner", target_identity="target", delivered_sha=sha, idempotency_key="stale-gate")
        kb.record_release_readback(conn, source, barrier="publish", owner_token="owner", readback_sha=sha)

        from plugins.kanban.dashboard.plugin_api import _set_status_direct
        assert _set_status_direct(conn, gate, "ready")
        latest = kb.claim_task(conn, gate)
        assert latest is not None
        monkeypatch.setenv("HERMES_PROFILE", "reviewer")
        assert kb.complete_task(conn, gate, result="pass", summary="follow-up", metadata={"candidate_sha": "0" * 40, "manifest_hash": manifest["manifest_hash"]}, expected_run_id=latest.current_run_id)

        monkeypatch.setenv("HERMES_PROFILE", "coordinator")
        assert kb.reconcile_release_barrier(conn, source, barrier="publish", owner_token="owner") == "waiting"
        release_barrier = kb.get_release_barrier(conn, source)
        assert release_barrier is not None
        assert release_barrier["completed_at"] is None


def test_mandatory_gate_rejects_failed_frozen_evidence(kanban_home, tmp_path, monkeypatch):
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in {"implementer", "reviewer"})
    sha = "7" * 40
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source", assignee="implementer", owner_kind="agent")
        _attested_candidate(conn, source, sha, monkeypatch)
        evidence = tmp_path / "evidence.txt"
        evidence.write_bytes(b"evidence")
        attachment = kb.add_attachment(conn, source, filename="evidence.txt", stored_path=str(evidence), content_type="text/plain", size=8)
        receipt = kb.freeze_evidence_manifest(conn, source, sha=sha, manifest={"required_slots": ["evidence"], "entries": [{"attachment_id": attachment, "sha256": hashlib.sha256(b"evidence").hexdigest(), "content_type": "text/plain", "cardinality": 1, "slot": "evidence", "mode": "required"}]}, verdict="fail", authority="coordinator")
        with pytest.raises(ValueError, match="passing frozen manifest"):
            kb.create_task(conn, title="gate", assignee="reviewer", owner_kind="agent", task_kind="reviewer", created_by_task_id=source, creation_authority="coordinator", gate_candidate_sha=sha, gate_manifest_hash=receipt["manifest_hash"])


def test_gate_reassignment_after_execution_cannot_manufacture_independence(kanban_home, tmp_path, monkeypatch):
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in {"implementer", "reviewer", "attacker"})
    sha = "9" * 40
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source", assignee="implementer", owner_kind="agent")
        candidate = _attested_candidate(conn, source, sha, monkeypatch)
        assert candidate["implementer"] == "implementer"
        manifest = _manifest(conn, source, sha, tmp_path)
        gate = kb.create_task(conn, title="gate", assignee="reviewer", owner_kind="agent", task_kind="reviewer", created_by_task_id=source, creation_authority="coordinator", gate_candidate_sha=sha, gate_manifest_hash=manifest["manifest_hash"])
        claimed = kb.claim_task(conn, gate)
        assert claimed is not None
        monkeypatch.setenv("HERMES_PROFILE", "reviewer")
        assert kb.complete_task(conn, gate, result="pass", summary="reviewed", metadata={"candidate_sha": sha, "manifest_hash": manifest["manifest_hash"]}, expected_run_id=claimed.current_run_id)
        assert kb.assign_task(conn, gate, "attacker", owner_kind="agent")
        monkeypatch.setenv("HERMES_PROFILE", "coordinator")
        with pytest.raises(ValueError, match="terminal result"):
            kb.record_gate_verdict(conn, source, gate_task_id=gate, sha=sha, manifest_hash=manifest["manifest_hash"], verdict="pass", authority="coordinator")


def test_release_reconcile_waits_for_live_source_then_clears_lifecycle_state(kanban_home, tmp_path, monkeypatch):
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in {"implementer", "reviewer"})
    sha = "8" * 40
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source", assignee="implementer", owner_kind="agent")
        child = kb.create_task(conn, title="child", assignee="implementer", owner_kind="agent", initial_status="blocked")
        kb.link_tasks(conn, parent_id=source, child_id=child)
        _attested_candidate(conn, source, sha, monkeypatch)
        manifest = _manifest(conn, source, sha, tmp_path)
        gate = kb.create_task(conn, title="gate", assignee="reviewer", owner_kind="agent", task_kind="reviewer", created_by_task_id=source, creation_authority="coordinator", gate_candidate_sha=sha, gate_manifest_hash=manifest["manifest_hash"])
        gate_run = kb.claim_task(conn, gate)
        assert gate_run is not None
        monkeypatch.setenv("HERMES_PROFILE", "reviewer")
        assert kb.complete_task(conn, gate, result="pass", summary="reviewed", metadata={"candidate_sha": sha, "manifest_hash": manifest["manifest_hash"]}, expected_run_id=gate_run.current_run_id)
        monkeypatch.setenv("HERMES_PROFILE", "coordinator")
        kb.record_gate_verdict(conn, source, gate_task_id=gate, sha=sha, manifest_hash=manifest["manifest_hash"], verdict="pass", authority="coordinator")
        kb.create_release_barrier(conn, source, sha=sha, manifest_hash=manifest["manifest_hash"], required_gates=[gate], authority="coordinator")
        assert kb.acquire_release_barrier_lease(conn, source, barrier="publish", owner_token="owner", ttl_seconds=60)
        kb.prepare_release_delivery_intent(conn, source, barrier="publish", owner_token="owner", target_identity="target", candidate_sha=sha, idempotency_key="stale-gate")
        kb.record_release_delivery(conn, source, barrier="publish", owner_token="owner", target_identity="target", delivered_sha=sha, idempotency_key="stale-gate")
        kb.record_release_readback(conn, source, barrier="publish", owner_token="owner", readback_sha=sha)
        live = kb.claim_task(conn, source)
        assert live is not None
        assert kb.reconcile_release_barrier(conn, source, barrier="publish", owner_token="owner") == "waiting"
        assert kb.get_task(conn, source).status == "running"
        assert kb.set_task_wait(conn, source, kind="human", ref="decision:release", expected_run_id=live.current_run_id)
        assert kb.reconcile_release_barrier(conn, source, barrier="publish", owner_token="owner") == "released"
        released = kb.get_task(conn, source)
        assert released is not None and released.status == "done" and released.completed_at is not None
        assert released.current_run_id is None and released.claim_lock is None and released.claim_expires is None and released.worker_pid is None
        assert released.wait_kind is None and released.wait_ref is None
        assert kb.get_task(conn, child).status == "ready"
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='release_barrier_completed'", (source,)).fetchone()[0] == 1


def test_agent_assignment_unassigns_into_non_dispatchable_manual_lane(kanban_home, monkeypatch):
    """No public unassign can leave a persisted agent task ownerless."""
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "worker")
    with kb.connect() as conn:
        task = kb.create_task(conn, title="owned", assignee="worker", owner_kind="agent")
        for missing in (None, "", "   "):
            assert kb.assign_task(conn, task, missing)
            current = kb.get_task(conn, task)
            assert current is not None
            assert (current.assignee, current.owner_kind, current.owner_kind_explicit) == (None, "no_agent", True)


def test_gate_verdict_binds_immutable_latest_terminal_run_id(kanban_home, tmp_path, monkeypatch):
    """A same-metadata re-run still invalidates the prior verdict by run identity."""
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in {"implementer", "reviewer"})
    sha = "1" * 40
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source", assignee="implementer", owner_kind="agent")
        _attested_candidate(conn, source, sha, monkeypatch)
        manifest = _manifest(conn, source, sha, tmp_path)
        gate = kb.create_task(conn, title="gate", assignee="reviewer", owner_kind="agent", task_kind="reviewer", created_by_task_id=source, creation_authority="coordinator", gate_candidate_sha=sha, gate_manifest_hash=manifest["manifest_hash"])
        first = kb.claim_task(conn, gate)
        assert first is not None
        monkeypatch.setenv("HERMES_PROFILE", "reviewer")
        assert kb.complete_task(conn, gate, result="pass", summary="first", metadata={"candidate_sha": sha, "manifest_hash": manifest["manifest_hash"]}, expected_run_id=first.current_run_id)
        monkeypatch.setenv("HERMES_PROFILE", "coordinator")
        verdict = kb.record_gate_verdict(conn, source, gate_task_id=gate, sha=sha, manifest_hash=manifest["manifest_hash"], verdict="pass", authority="coordinator")
        assert verdict["gate_run_id"] == first.current_run_id
        kb.create_release_barrier(conn, source, sha=sha, manifest_hash=manifest["manifest_hash"], required_gates=[gate], authority="coordinator")
        assert kb.acquire_release_barrier_lease(conn, source, barrier="publish", owner_token="owner", ttl_seconds=60)
        from plugins.kanban.dashboard.plugin_api import _set_status_direct
        assert _set_status_direct(conn, gate, "ready")
        second = kb.claim_task(conn, gate)
        assert second is not None
        monkeypatch.setenv("HERMES_PROFILE", "reviewer")
        assert kb.complete_task(conn, gate, result="pass", summary="second", metadata={"candidate_sha": sha, "manifest_hash": manifest["manifest_hash"]}, expected_run_id=second.current_run_id)
        monkeypatch.setenv("HERMES_PROFILE", "coordinator")
        assert kb.reconcile_release_barrier(conn, source, barrier="publish", owner_token="owner") == "waiting"
        with pytest.raises(ValueError, match="immutable"):
            kb.record_gate_verdict(conn, source, gate_task_id=gate, sha=sha, manifest_hash=manifest["manifest_hash"], verdict="pass", authority="coordinator")


def test_release_delivery_intent_is_immutable_across_expired_lease_takeover(
    kanban_home, tmp_path, monkeypatch,
):
    """A second coordinator reuses one durable external-publish identity after a crash."""
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in {"implementer", "reviewer"})
    sha = "3" * 40
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source", assignee="implementer", owner_kind="agent")
        _attested_candidate(conn, source, sha, monkeypatch)
        manifest = _manifest(conn, source, sha, tmp_path)
        gate = kb.create_task(conn, title="gate", assignee="reviewer", owner_kind="agent", task_kind="reviewer", created_by_task_id=source, creation_authority="coordinator", gate_candidate_sha=sha, gate_manifest_hash=manifest["manifest_hash"])
        claimed = kb.claim_task(conn, gate)
        monkeypatch.setenv("HERMES_PROFILE", "reviewer")
        assert claimed and kb.complete_task(conn, gate, result="pass", summary="pass", metadata={"candidate_sha": sha, "manifest_hash": manifest["manifest_hash"]}, expected_run_id=claimed.current_run_id)
        monkeypatch.setenv("HERMES_PROFILE", "coordinator")
        kb.record_gate_verdict(conn, source, gate_task_id=gate, sha=sha, manifest_hash=manifest["manifest_hash"], verdict="pass", authority="coordinator")
        kb.create_release_barrier(conn, source, sha=sha, manifest_hash=manifest["manifest_hash"], required_gates=[gate], authority="coordinator")
        assert kb.acquire_release_barrier_lease(conn, source, barrier="publish", owner_token="one", ttl_seconds=60)
        intent = kb.prepare_release_delivery_intent(conn, source, barrier="publish", owner_token="one", target_identity="target-a", candidate_sha=sha, idempotency_key="caller-stable-key")
        assert intent["idempotency_key"] == "caller-stable-key"
        conn.execute("UPDATE task_release_barriers SET lease_expires_at=0 WHERE task_id=?", (source,))
    with kb.connect() as takeover:
        assert kb.acquire_release_barrier_lease(takeover, source, barrier="publish", owner_token="two", ttl_seconds=60)
        reused = kb.prepare_release_delivery_intent(takeover, source, barrier="publish", owner_token="two", target_identity="target-a", candidate_sha=sha)
        assert reused == intent
        with pytest.raises(ValueError, match="immutable"):
            kb.prepare_release_delivery_intent(takeover, source, barrier="publish", owner_token="two", target_identity="target-b", candidate_sha=sha)
        with pytest.raises(ValueError, match="prepared intent"):
            kb.record_release_delivery(takeover, source, barrier="publish", owner_token="two", target_identity="target-a", delivered_sha=sha, idempotency_key="wrong-key")
        kb.record_release_delivery(takeover, source, barrier="publish", owner_token="two", target_identity="target-a", delivered_sha=sha, idempotency_key="caller-stable-key")


def test_release_delivery_is_first_write_immutable_and_exactly_idempotent(kanban_home, tmp_path, monkeypatch):
    """Delivery proof is a first-write CAS before and after a barrier completes."""
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in {"implementer", "reviewer"})
    sha = "2" * 40
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source", assignee="implementer", owner_kind="agent")
        _attested_candidate(conn, source, sha, monkeypatch)
        manifest = _manifest(conn, source, sha, tmp_path)
        gate = kb.create_task(conn, title="gate", assignee="reviewer", owner_kind="agent", task_kind="reviewer", created_by_task_id=source, creation_authority="coordinator", gate_candidate_sha=sha, gate_manifest_hash=manifest["manifest_hash"])
        claimed = kb.claim_task(conn, gate)
        monkeypatch.setenv("HERMES_PROFILE", "reviewer")
        assert claimed and kb.complete_task(conn, gate, result="pass", summary="pass", metadata={"candidate_sha": sha, "manifest_hash": manifest["manifest_hash"]}, expected_run_id=claimed.current_run_id)
        monkeypatch.setenv("HERMES_PROFILE", "coordinator")
        kb.record_gate_verdict(conn, source, gate_task_id=gate, sha=sha, manifest_hash=manifest["manifest_hash"], verdict="pass", authority="coordinator")
        kb.create_release_barrier(conn, source, sha=sha, manifest_hash=manifest["manifest_hash"], required_gates=[gate], authority="coordinator")
        assert kb.acquire_release_barrier_lease(conn, source, barrier="publish", owner_token="owner", ttl_seconds=60)
        kb.prepare_release_delivery_intent(conn, source, barrier="publish", owner_token="owner", target_identity="target-a", candidate_sha=sha, idempotency_key="first-write")
        kb.record_release_delivery(conn, source, barrier="publish", owner_token="owner", target_identity="target-a", delivered_sha=sha, idempotency_key="first-write")
        kb.record_release_delivery(conn, source, barrier="publish", owner_token="owner", target_identity="target-a", delivered_sha=sha, idempotency_key="first-write")
        with pytest.raises(ValueError, match="prepared intent"):
            kb.record_release_delivery(conn, source, barrier="publish", owner_token="owner", target_identity="target-b", delivered_sha=sha, idempotency_key="first-write")
        kb.record_release_readback(conn, source, barrier="publish", owner_token="owner", readback_sha=sha)
        assert kb.reconcile_release_barrier(conn, source, barrier="publish", owner_token="owner") == "released"
        kb.record_release_delivery(conn, source, barrier="publish", owner_token="owner", target_identity="target-a", delivered_sha=sha, idempotency_key="first-write")
        with pytest.raises(ValueError, match="prepared intent"):
            kb.record_release_delivery(conn, source, barrier="publish", owner_token="owner", target_identity="target-b", delivered_sha=sha, idempotency_key="first-write")
