"""Workflow v1 execution-kernel invariants for evidence-fenced Kanban leaves."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_evidence import compute_workspace_evidence

_claim_task_impl = kb.claim_task


SPEC_HASH = "a" * 64
CAPSULE_HASH = "b" * 64
LEAF_KEY = "github:veltrosecurity/veltro:issue-202:leaf-auth-contract:v1"
LEAF_FAMILY_KEY = "github:veltrosecurity/veltro:issue-202:leaf-auth-contract"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _init_repo(path: Path) -> str:
    path.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "workflow@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Workflow Test"],
        check=True,
    )
    (path / "src").mkdir()
    (path / "src" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    (path / "notes.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "baseline"],
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _create_leaf(conn, repo: Path, pin_sha: str, **overrides) -> str:
    fields = {
        "title": "atomic implementation leaf",
        "body": json.dumps(
            {"spec": {"dependencies": [], "first_evidence_seconds": 600}},
            sort_keys=True,
        ),
        "assignee": "coder",
        "workspace_kind": "worktree",
        "workspace_path": str(repo),
        "leaf_key": LEAF_KEY,
        "leaf_family_key": LEAF_FAMILY_KEY,
        "spec_hash": SPEC_HASH,
        "pin_sha": pin_sha,
        "capsule_hash": CAPSULE_HASH,
        "evidence_paths": ["src/**"],
        "lease_policy": "evidence",
    }
    fields.update(overrides)
    task_id = kb.create_task(conn, **fields)
    conn.execute(
        "UPDATE workflow_controller_state SET dispatch_enabled = 1, "
        "broker_ready = 1, status = 'healthy', controller_epoch = 'fence-test', "
        "heartbeat_at = ?, updated_at = ? WHERE singleton = 1",
        (int(time.time()), int(time.time())),
    )
    conn.commit()
    return task_id


def _claim_leaf(conn, task_id: str, **kwargs):
    task = kb.get_task(conn, task_id)
    assert task is not None and task.workspace_path
    controller = conn.execute(
        "SELECT controller_epoch FROM workflow_controller_state WHERE singleton = 1"
    ).fetchone()
    assert controller is not None and controller["controller_epoch"]
    return _claim_task_impl(
        conn,
        task_id,
        expected_controller_epoch=controller["controller_epoch"],
        expected_workspace_path=task.workspace_path,
        **kwargs,
    )


def test_evidence_leaf_requires_complete_immutable_identity(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="complete execution identity"):
            kb.create_task(
                conn,
                title="incomplete",
                assignee="coder",
                workspace_kind="worktree",
                workspace_path=str(repo),
                leaf_key=LEAF_KEY,
                lease_policy="evidence",
            )

        task_id = _create_leaf(conn, repo, pin_sha)
        task = kb.get_task(conn, task_id)
        assert task.leaf_key == LEAF_KEY
        assert task.leaf_family_key == LEAF_FAMILY_KEY
        assert task.spec_hash == SPEC_HASH
        assert task.pin_sha == pin_sha
        assert task.capsule_hash == CAPSULE_HASH
        assert task.evidence_paths == ["src/**"]
        assert task.lease_policy == "evidence"


def test_leaf_key_is_atomically_unique_under_concurrent_creation(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)

    def create_once(_: int) -> str:
        with kb.connect() as conn:
            return _create_leaf(conn, repo, pin_sha)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(create_once, range(24)))

    assert len(set(ids)) == 1
    with kb.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE leaf_key = ?", (LEAF_KEY,)
        ).fetchall()
        assert [row["id"] for row in rows] == [ids[0]]
        indexes = conn.execute("PRAGMA index_list(tasks)").fetchall()
        assert any(
            row["unique"] and row["name"] == "idx_tasks_leaf_key_unique"
            for row in indexes
        )


def test_claim_freezes_identity_and_uses_opaque_token(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = _create_leaf(conn, repo, pin_sha)
        task = _claim_leaf(conn, task_id, ttl_seconds=60, allow_execution_leaf=True)
        assert task is not None
        assert task.claim_lock is not None
        host_pid, separator, token = task.claim_lock.rpartition(":")
        assert separator == ":"
        assert ":" in host_pid
        assert len(token) >= 32

        run = kb.get_run(conn, task.current_run_id, allow_execution_leaf=True)
        assert run is not None
        assert run.claim_lock == task.claim_lock
        assert run.leaf_key == LEAF_KEY
        assert run.leaf_family_key == LEAF_FAMILY_KEY
        assert run.spec_hash == SPEC_HASH
        assert run.pin_sha == pin_sha
        assert run.capsule_hash == CAPSULE_HASH


def test_heartbeat_is_liveness_only_for_evidence_lease(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = _create_leaf(conn, repo, pin_sha)
        task = _claim_leaf(conn, task_id, ttl_seconds=60, allow_execution_leaf=True)
        assert task is not None
        original_expiry = task.claim_expires

        assert (
            kb.heartbeat_claim(
                conn,
                task_id,
                ttl_seconds=3600,
                claimer=task.claim_lock,
                expected_run_id=task.current_run_id,
            )
            is True
        )
        assert (
            kb.heartbeat_worker(
                conn,
                task_id,
                note="still thinking",
                expected_run_id=task.current_run_id,
                expected_claim_lock=task.claim_lock,
            )
            is True
        )

        current = kb.get_task(conn, task_id)
        assert current.claim_expires == original_expiry
        assert current.last_heartbeat_at is not None


def test_only_new_verified_in_scope_workspace_delta_renews_evidence_lease(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = _create_leaf(conn, repo, pin_sha)
        task = _claim_leaf(conn, task_id, ttl_seconds=10, allow_execution_leaf=True)
        assert task is not None
        run_id = task.current_run_id
        original_expiry = task.claim_expires

        empty = kb.record_workspace_evidence(
            conn,
            task_id,
            expected_run_id=run_id,
            expected_controller_epoch="fence-test",
            claim_lock=task.claim_lock,
            ttl_seconds=90,
        )
        assert empty.accepted is False
        assert empty.reason == "no_in_scope_delta"
        assert kb.get_task(conn, task_id).claim_expires == original_expiry

        (repo / "notes.txt").write_text("out of scope\n", encoding="utf-8")
        outside = kb.record_workspace_evidence(
            conn,
            task_id,
            expected_run_id=run_id,
            expected_controller_epoch="fence-test",
            claim_lock=task.claim_lock,
            ttl_seconds=90,
        )
        assert outside.accepted is False
        assert outside.reason == "no_in_scope_delta"

        (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        accepted = kb.record_workspace_evidence(
            conn,
            task_id,
            expected_run_id=run_id,
            expected_controller_epoch="fence-test",
            claim_lock=task.claim_lock,
            ttl_seconds=90,
        )
        assert accepted.accepted is True
        assert accepted.reason == "new_workspace_delta"
        assert accepted.digest
        renewed_expiry = kb.get_task(conn, task_id).claim_expires
        assert renewed_expiry > original_expiry

        repeated = kb.record_workspace_evidence(
            conn,
            task_id,
            expected_run_id=run_id,
            expected_controller_epoch="fence-test",
            claim_lock=task.claim_lock,
            ttl_seconds=900,
        )
        assert repeated.accepted is False
        assert repeated.reason == "duplicate_evidence"
        assert kb.get_task(conn, task_id).claim_expires == renewed_expiry

        stale = kb.record_workspace_evidence(
            conn,
            task_id,
            expected_run_id=run_id + 1,
            expected_controller_epoch="fence-test",
            claim_lock=task.claim_lock,
            ttl_seconds=900,
        )
        assert stale.accepted is False
        assert stale.reason == "stale_fence"

        events = kb.list_events(conn, task_id, allow_execution_leaf=True)
        evidence_events = [event for event in events if event.kind == "evidence"]
        assert len(evidence_events) == 1
        assert evidence_events[0].run_id == run_id
        assert evidence_events[0].payload["digest"] == accepted.digest
        assert evidence_events[0].payload["paths"] == ["src/feature.py"]


def test_evidence_renewal_is_epoch_bound_and_capped_by_run_deadline(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = _create_leaf(conn, repo, pin_sha, max_runtime_seconds=60)
        task = _claim_leaf(conn, task_id, ttl_seconds=10, allow_execution_leaf=True)
        assert task is not None
        run = kb.get_run(conn, task.current_run_id, allow_execution_leaf=True)
        assert run is not None
        monkeypatch.setattr(kb, "_resolve_claim_ttl_seconds", lambda: 3600)

        (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        accepted = kb.record_workspace_evidence(
            conn,
            task_id,
            expected_run_id=task.current_run_id,
            expected_controller_epoch="fence-test",
            claim_lock=task.claim_lock,
        )
        assert accepted.accepted is True
        assert kb.get_task(conn, task_id).claim_expires <= run.started_at + 60

        (repo / "src" / "feature.py").write_text("VALUE = 3\n", encoding="utf-8")
        conn.execute(
            "UPDATE workflow_controller_state SET controller_epoch = 'replacement' "
            "WHERE singleton = 1"
        )
        conn.commit()
        rejected = kb.record_workspace_evidence(
            conn,
            task_id,
            expected_run_id=task.current_run_id,
            expected_controller_epoch="fence-test",
            claim_lock=task.claim_lock,
        )
        assert rejected.accepted is False
        assert rejected.reason == "stale_controller_epoch"


def test_expired_evidence_lease_is_not_extended_just_because_pid_is_alive(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = _create_leaf(conn, repo, pin_sha)
        task = _claim_leaf(conn, task_id, ttl_seconds=10, allow_execution_leaf=True)
        assert task is not None
        kb._set_worker_pid(conn, task_id, 424242)
        expired = int(time.time()) - 1
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?", (expired, task_id)
        )
        conn.execute(
            "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
            (expired, task.current_run_id),
        )
        conn.commit()

        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *_args, **_kwargs: {
                "termination": "terminated",
                "alive_after": False,
                "terminated": True,
            },
        )

        assert kb.release_stale_claims(conn) == 1
        current = kb.get_task(conn, task_id)
        assert current.status == "ready"
        assert current.claim_lock is None
        assert not any(
            event.kind == "claim_extended"
            for event in kb.list_events(conn, task_id, allow_execution_leaf=True)
        )
        serialized_events = json.dumps(
            [
                event.payload
                for event in kb.list_events(conn, task_id, allow_execution_leaf=True)
            ],
            sort_keys=True,
        )
        assert task.claim_lock not in serialized_events
        assert current.worker_pid is None


def test_legacy_dispatch_maintenance_does_not_reclaim_execution_leaf(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = _create_leaf(conn, repo, pin_sha)
        task = _claim_leaf(conn, task_id, allow_execution_leaf=True)
        assert task is not None
        kb._set_worker_pid(conn, task_id, 999999)
        old_started = int(time.time()) - 600
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?",
            (old_started, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = ?",
            (old_started, task.current_run_id),
        )
        conn.commit()
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

        result = kb.dispatch_once(conn, spawn_fn=lambda *_args: None, max_spawn=0)

        current = kb.get_task(conn, task_id)
        assert task_id not in result.crashed
        assert current.status == "running"
        assert current.claim_lock == task.claim_lock
        assert current.worker_pid == 999999


def test_failed_reclaim_keeps_expired_worker_fenced_without_reauthorizing(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = _create_leaf(conn, repo, pin_sha)
        task = _claim_leaf(conn, task_id, ttl_seconds=10, allow_execution_leaf=True)
        assert task is not None
        kb._set_worker_pid(conn, task_id, 424242)
        expired = int(time.time()) - 1
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (expired, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
            (expired, task.current_run_id),
        )
        conn.commit()
        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *_args, **_kwargs: {
                "host_local": True,
                "termination_attempted": True,
                "terminated": False,
            },
        )

        assert kb.release_stale_claims(conn) == 0
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "running"
        assert current.claim_lock == task.claim_lock
        assert current.claim_expires == expired
        assert not kb.complete_task(
            conn,
            task_id,
            result="expired worker attempted completion",
            expected_run_id=task.current_run_id,
            expected_claim_lock=task.claim_lock,
        )
        deferred = [
            event
            for event in kb.list_events(conn, task_id, allow_execution_leaf=True)
            if event.kind == "reclaim_deferred"
        ]
        assert len(deferred) == 1
        assert "claim_lock" not in deferred[0].payload


def test_stale_run_cannot_submit_evidence_or_complete_current_attempt(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = _create_leaf(conn, repo, pin_sha)
        first = _claim_leaf(conn, task_id, ttl_seconds=10, allow_execution_leaf=True)
        assert first is not None
        first_run = first.current_run_id
        first_lock = first.claim_lock

        conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, "
            "worker_pid=NULL, current_run_id=NULL WHERE id=?",
            (task_id,),
        )
        conn.execute(
            "UPDATE task_runs SET status='released', outcome='reclaimed', ended_at=? "
            "WHERE id=?",
            (int(time.time()), first_run),
        )
        conn.commit()

        second = _claim_leaf(conn, task_id, ttl_seconds=10, allow_execution_leaf=True)
        assert second is not None
        assert second.current_run_id != first_run

        (repo / "src" / "feature.py").write_text("VALUE = 3\n", encoding="utf-8")
        rejected = kb.record_workspace_evidence(
            conn,
            task_id,
            expected_run_id=first_run,
            expected_controller_epoch="fence-test",
            claim_lock=first_lock,
        )
        assert rejected.accepted is False
        assert rejected.reason == "stale_fence"
        assert (
            kb.complete_task(
                conn,
                task_id,
                result="stale result",
                expected_run_id=first_run,
                expected_claim_lock=first_lock,
            )
            is False
        )
        assert kb.get_task(conn, task_id).status == "running"


def test_evidence_leaf_worker_cannot_create_successor_at_db_layer(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = _create_leaf(conn, repo, pin_sha)
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        with pytest.raises(
            PermissionError, match="orchestrator owns successor creation"
        ):
            kb.create_task(
                conn,
                title="invented successor",
                assignee="coder",
                parents=(task_id,),
            )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_generic_claim_path_cannot_dispatch_execution_leaf(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = _create_leaf(conn, repo, pin_sha)

        assert _claim_leaf(conn, task_id, ttl_seconds=60) is None
        assert kb.get_task(conn, task_id).status == "ready"

        claimed = _claim_leaf(conn, task_id, ttl_seconds=60, allow_execution_leaf=True)
        assert claimed is not None


def test_legacy_dispatch_selectors_ignore_protected_ready_and_review_leaves(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _profile: True)
    spawned = []

    def spawn(task, _workspace, _board):
        spawned.append(task.id)
        return 4242

    with kb.connect() as conn:
        task_id = _create_leaf(conn, repo, pin_sha, assignee=None)
        result = kb.dispatch_once(
            conn,
            spawn_fn=spawn,
            default_assignee="fallback",
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.assignee is None
        assert task.status == "ready"
        assert task_id not in result.auto_assigned_default
        assert task_id not in spawned

        conn.execute(
            "UPDATE tasks SET status = 'review', claim_lock = NULL WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        assert kb.claim_review_task(conn, task_id) is None
        result = kb.dispatch_once(conn, spawn_fn=spawn)
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "review"
        assert task.claim_lock is None
        assert task_id not in result.spawned
        assert task_id not in spawned


def test_execution_lifecycle_requires_live_run_and_claim_token(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = _create_leaf(conn, repo, pin_sha)
        task = _claim_leaf(conn, task_id, ttl_seconds=60, allow_execution_leaf=True)
        assert task is not None

        assert not kb.heartbeat_claim(conn, task_id, claimer=task.claim_lock)
        assert not kb.heartbeat_worker(
            conn, task_id, expected_run_id=task.current_run_id
        )
        assert not kb.complete_task(conn, task_id, expected_run_id=task.current_run_id)
        assert not kb.block_task(conn, task_id, expected_run_id=task.current_run_id)

        expired = int(time.time()) - 1
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?", (expired, task_id)
        )
        conn.execute(
            "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
            (expired, task.current_run_id),
        )
        conn.commit()

        assert not kb.heartbeat_claim(
            conn,
            task_id,
            claimer=task.claim_lock,
            expected_run_id=task.current_run_id,
        )
        assert not kb.heartbeat_worker(
            conn,
            task_id,
            expected_run_id=task.current_run_id,
            expected_claim_lock=task.claim_lock,
        )
        assert not kb.complete_task(
            conn,
            task_id,
            expected_run_id=task.current_run_id,
            expected_claim_lock=task.claim_lock,
        )
        assert not kb.block_task(
            conn,
            task_id,
            expected_run_id=task.current_run_id,
            expected_claim_lock=task.claim_lock,
        )
        assert kb.get_task(conn, task_id).status == "running"


def test_evidence_digest_cannot_be_replayed_within_attempt(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    feature = repo / "src" / "feature.py"
    with kb.connect() as conn:
        task_id = _create_leaf(conn, repo, pin_sha)
        task = _claim_leaf(conn, task_id, ttl_seconds=60, allow_execution_leaf=True)
        assert task is not None

        feature.write_text("VALUE = 2\n", encoding="utf-8")
        first = kb.record_workspace_evidence(
            conn,
            task_id,
            expected_run_id=task.current_run_id,
            expected_controller_epoch="fence-test",
            claim_lock=task.claim_lock,
        )
        assert first.accepted

        feature.write_text("VALUE = 3\n", encoding="utf-8")
        second = kb.record_workspace_evidence(
            conn,
            task_id,
            expected_run_id=task.current_run_id,
            expected_controller_epoch="fence-test",
            claim_lock=task.claim_lock,
        )
        assert second.accepted
        assert second.digest != first.digest

        feature.write_text("VALUE = 2\n", encoding="utf-8")
        replay = kb.record_workspace_evidence(
            conn,
            task_id,
            expected_run_id=task.current_run_id,
            expected_controller_epoch="fence-test",
            claim_lock=task.claim_lock,
        )
        assert not replay.accepted
        assert replay.reason == "duplicate_evidence"
        assert replay.digest == first.digest


def test_execution_identity_is_copied_as_canonical_json_in_created_event(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = _create_leaf(conn, repo, pin_sha)
        created = next(
            event
            for event in kb.list_events(conn, task_id, allow_execution_leaf=True)
            if event.kind == "created"
        )
        identity = created.payload["execution_identity"]
        assert json.dumps(
            identity, sort_keys=True, separators=(",", ":")
        ) == json.dumps(
            {
                "capsule_hash": CAPSULE_HASH,
                "evidence_paths": ["src/**"],
                "leaf_family_key": LEAF_FAMILY_KEY,
                "leaf_key": LEAF_KEY,
                "lease_policy": "evidence",
                "pin_sha": pin_sha,
                "spec_hash": SPEC_HASH,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


def test_generic_operator_mutations_reject_evidence_fenced_leaf(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = _create_leaf(conn, repo, pin_sha)

        guarded = (
            lambda: kb.assign_task(conn, task_id, "other"),
            lambda: kb.set_model_override(conn, task_id, "other-model"),
            lambda: kb.set_workspace_path(conn, task_id, repo / "other"),
            lambda: kb.set_branch_name(conn, task_id, "other-branch"),
            lambda: kb.schedule_task(conn, task_id),
            lambda: kb.archive_task(conn, task_id),
            lambda: kb.delete_task(conn, task_id),
        )
        for mutate in guarded:
            with pytest.raises(PermissionError, match="controller-only"):
                mutate()

        assert not kb.complete_task(conn, task_id, result="generic bypass")
        assert not kb.block_task(conn, task_id, reason="generic bypass")

        promoted, reason = kb.promote_task(
            conn,
            task_id,
            actor="generic-operator",
        )
        assert promoted is False
        assert reason and "Workflow controller" in reason

        claimed = _claim_leaf(conn, task_id, allow_execution_leaf=True)
        assert claimed is not None
        with pytest.raises(PermissionError, match="reclaim is controller-only"):
            kb.reclaim_task(conn, task_id)

        assert kb.block_task(
            conn,
            task_id,
            reason="controller quarantine",
            force_execution_admin=True,
        )
        with pytest.raises(PermissionError, match="unblock is controller-only"):
            kb.unblock_task(conn, task_id)


def test_generic_db_sibling_reads_and_subscriptions_hide_execution_leaf(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="ordinary parent", assignee="coder")
        kb.add_notify_sub(
            conn,
            task_id=parent_id,
            platform="desktop",
            chat_id="test",
        )
        assert kb.claim_task(conn, parent_id) is not None
        assert kb.complete_task(conn, parent_id, result="done")
        task_id = _create_leaf(conn, repo, pin_sha, parents=[parent_id])
        conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at, leaf_key) "
            "VALUES (?, 'running', ?, ?)",
            (task_id, int(time.time()), LEAF_KEY),
        )
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, 'controller', 'protected comment', ?)",
            (task_id, int(time.time())),
        )
        conn.commit()

        assert kb.list_comments(conn, task_id) == []
        assert kb.list_events(conn, task_id) == []
        assert kb.list_runs(conn, task_id) == []
        assert kb.list_notify_subs(conn, task_id) == []

        assert len(kb.list_comments(conn, task_id, allow_execution_leaf=True)) == 1
        assert len(kb.list_events(conn, task_id, allow_execution_leaf=True)) >= 1
        assert len(kb.list_runs(conn, task_id, allow_execution_leaf=True)) == 1
        assert kb.list_notify_subs(conn, task_id, allow_execution_leaf=True) == []


def test_protected_claim_event_redacts_token_and_completed_result_is_immutable(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = _create_leaf(conn, repo, pin_sha)
        claimed = _claim_leaf(conn, task_id, allow_execution_leaf=True)
        assert claimed is not None
        event = next(
            event
            for event in kb.list_events(conn, task_id, allow_execution_leaf=True)
            if event.kind == "claimed"
        )
        assert "lock" not in event.payload
        assert (
            event.payload["lock_digest"]
            == hashlib.sha256(claimed.claim_lock.encode("utf-8")).hexdigest()
        )

        assert kb.complete_task(
            conn,
            task_id,
            result="fenced completion",
            expected_run_id=claimed.current_run_id,
            expected_claim_lock=claimed.claim_lock,
            force_execution_admin=True,
        )
        assert not kb.edit_completed_task_result(
            conn,
            task_id,
            result="generic rewrite",
        )


def test_workspace_evidence_requires_exact_commit_pin(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exact canonical commit"):
        compute_workspace_evidence(
            repo,
            pin_sha="HEAD",
            evidence_paths=("src/**",),
        )


def test_workspace_evidence_rejects_symlinked_workspace_and_untracked_symlink(
    tmp_path,
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo, target_is_directory=True)
    with pytest.raises(ValueError, match="must not traverse symlinks"):
        compute_workspace_evidence(
            alias,
            pin_sha=pin_sha,
            evidence_paths=("src/**",),
        )

    (repo / "src" / "linked.py").symlink_to(repo / "notes.txt")
    with pytest.raises(ValueError, match="symlink evidence is not accepted"):
        compute_workspace_evidence(
            repo,
            pin_sha=pin_sha,
            evidence_paths=("src/**",),
        )


def test_workspace_evidence_enforces_file_and_byte_budgets(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    monkeypatch.setattr("hermes_cli.kanban_evidence._MAX_EVIDENCE_FILES", 2)
    for index in range(3):
        (repo / "src" / f"generated-{index}.py").write_text(
            f"VALUE = {index}\n", encoding="utf-8"
        )
    with pytest.raises(ValueError, match="too many changed files"):
        compute_workspace_evidence(
            repo,
            pin_sha=pin_sha,
            evidence_paths=("src/**",),
        )

    for path in (repo / "src").glob("generated-*.py"):
        path.unlink()
    monkeypatch.setattr("hermes_cli.kanban_evidence._MAX_UNTRACKED_BYTES", 8)
    (repo / "src" / "large.py").write_bytes(b"x" * 9)
    with pytest.raises(ValueError, match="untracked evidence exceeds byte budget"):
        compute_workspace_evidence(
            repo,
            pin_sha=pin_sha,
            evidence_paths=("src/**",),
        )
