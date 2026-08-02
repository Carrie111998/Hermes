"""Targeted capsule, readiness, and restart reconciliation for Workflow v1."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_execution as execution
from hermes_cli import kanban_evidence as evidence
from hermes_cli.kanban_execution import (
    ContextCapsule,
    LeafSpec,
    WorkflowControlConflict,
    WorkflowControlUnavailable,
    begin_workflow_controller_epoch,
    claim_execution_leaf,
    get_workflow_controller_state,
    reconcile_execution_leaves,
    register_execution_leaf,
    run_workflow_controller_tick,
    set_workflow_broker_ready,
    set_workflow_dispatch_enabled,
    supersede_execution_leaf,
    validate_execution_readiness,
)

_claim_execution_leaf_impl = claim_execution_leaf


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


def _spec(
    pin_sha: str,
    *,
    version: int = 1,
    dependencies: tuple[str, ...] = (),
    leaf_id: str = "auth-contract",
    first_evidence_seconds: int = 600,
    wall_clock_budget_seconds: int = 1500,
) -> LeafSpec:
    return LeafSpec(
        repository="veltrosecurity/veltro",
        campaign_issue="202",
        leaf_id=leaf_id,
        version=version,
        objective="Add the bounded authority contract.",
        exclusions=("No release or deployment.",),
        allowed_paths=("src/**",),
        dependencies=dependencies,
        acceptance_checks=("python -m pytest tests/test_feature.py",),
        hazards=("Fail closed on malformed authority.",),
        human_gates=(),
        pin_sha=pin_sha,
        first_evidence_seconds=first_evidence_seconds,
        wall_clock_budget_seconds=wall_clock_budget_seconds,
    )


def _capsule() -> ContextCapsule:
    return ContextCapsule(
        relevant_files=("src/feature.py",),
        symbols=("AuthorityContract",),
        governing_decisions=("ADR-007 fail-closed authority",),
        base_assumptions=("The pinned source has no producer cutover.",),
        output_schema=("summary", "files", "checks", "blocker"),
    )


def _enable_test_dispatch(conn):
    state = get_workflow_controller_state(conn)
    if state.status != "healthy":
        epoch = f"workflow-test-{state.version + 1}"
        begin_workflow_controller_epoch(conn, controller_epoch=epoch)
        run_workflow_controller_tick(
            conn,
            controller_epoch=epoch,
            process_alive=lambda _pid: False,
        )
        state = get_workflow_controller_state(conn)
    state = set_workflow_broker_ready(
        conn,
        ready=True,
        expected_version=state.version,
        actor="workflow-test",
        reason="isolated test broker",
    )
    return set_workflow_dispatch_enabled(
        conn,
        enabled=True,
        expected_version=state.version,
        actor="workflow-test",
        reason="isolated claim test",
    )


def _claim_execution_leaf(conn, task_id: str, **kwargs):
    kwargs.setdefault(
        "expected_controller_epoch",
        get_workflow_controller_state(conn).controller_epoch,
    )
    return _claim_execution_leaf_impl(conn, task_id, **kwargs)


def test_remote_controller_defaults_paused_and_requires_broker(kanban_home):
    with kb.connect() as conn:
        state = get_workflow_controller_state(conn)
        assert state.version == 0
        assert state.dispatch_enabled is False
        assert state.broker_ready is False
        assert state.status == "stopped"

        with pytest.raises(WorkflowControlUnavailable, match="broker"):
            set_workflow_dispatch_enabled(
                conn,
                enabled=True,
                expected_version=state.version,
                actor="desktop:test",
                reason="attempted premature resume",
            )

        state = _enable_test_dispatch(conn)
        assert state.dispatch_enabled is True
        assert state.broker_ready is True

        with pytest.raises(WorkflowControlConflict, match="stale"):
            set_workflow_dispatch_enabled(
                conn,
                enabled=False,
                expected_version=state.version - 1,
                actor="desktop:test",
                reason="stale cached Desktop request",
            )

        state = set_workflow_dispatch_enabled(
            conn,
            enabled=False,
            expected_version=state.version,
            actor="server-admin",
            reason="remote emergency stop",
        )
        assert state.dispatch_enabled is False
        kinds = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM workflow_controller_events ORDER BY id"
            ).fetchall()
        ]
        assert kinds == [
            "controller_epoch_started",
            "broker_ready",
            "dispatch_resumed",
            "dispatch_paused",
        ]


def test_remote_controller_tick_is_epoch_fenced_and_persisted(kanban_home):
    with kb.connect() as conn:
        first = begin_workflow_controller_epoch(
            conn, controller_epoch="gateway-epoch-a"
        )
        assert first.controller_epoch == "gateway-epoch-a"
        report = run_workflow_controller_tick(
            conn,
            controller_epoch="gateway-epoch-a",
            process_alive=lambda _pid: False,
        )
        assert report.findings == ()
        healthy = get_workflow_controller_state(conn)
        assert healthy.status == "healthy"
        assert healthy.heartbeat_at is not None
        assert healthy.last_reconciled_at is not None

        begin_workflow_controller_epoch(conn, controller_epoch="gateway-epoch-b")
        with pytest.raises(WorkflowControlConflict, match="epoch"):
            run_workflow_controller_tick(
                conn,
                controller_epoch="gateway-epoch-a",
                process_alive=lambda _pid: False,
            )


def test_registers_canonical_targeted_capsule_and_is_ready(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.leaf_key == (
            "github:veltrosecurity/veltro:issue-202:leaf-auth-contract:v1"
        )
        assert task.max_runtime_seconds == 1500
        assert task.evidence_paths == ["src/**"]

        envelope = json.loads(task.body)
        assert envelope["schema"] == "hermes.execution-capsule.v1"
        assert envelope["spec"]["pin_sha"] == pin_sha
        assert envelope["capsule"]["relevant_files"] == ["src/feature.py"]
        assert validate_execution_readiness(conn, task_id).ready is True


def test_readiness_rejects_capsule_tamper_and_base_drift(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        original_body = kb.get_task(conn, task_id).body
        envelope = json.loads(original_body)
        envelope["capsule"]["governing_decisions"] = ["invented decision"]
        conn.execute(
            "UPDATE tasks SET body = ? WHERE id = ?",
            (json.dumps(envelope), task_id),
        )
        conn.commit()
        tampered = validate_execution_readiness(conn, task_id)
        assert tampered.ready is False
        assert "capsule_hash_mismatch" in tampered.blockers

        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (original_body, task_id))
        conn.commit()
        (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "drift"],
            check=True,
            capture_output=True,
            text=True,
        )
        drifted = validate_execution_readiness(conn, task_id)
        assert drifted.ready is False
        assert "pin_sha_drift" in drifted.blockers


def test_readiness_rejects_dirty_initial_workspace(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        (repo / "src" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")

        readiness = validate_execution_readiness(conn, task_id)
        assert readiness.ready is False
        assert "workspace_dirty" in readiness.blockers
        assert _claim_execution_leaf(conn, task_id) is None


def test_controller_git_inspection_ignores_repository_executable_config(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    marker = tmp_path / "fsmonitor-invoked"
    monitor = tmp_path / "fsmonitor.sh"
    monitor.write_text(
        f"#!/bin/sh\nprintf invoked >> {marker}\n",
        encoding="utf-8",
    )
    monitor.chmod(0o755)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.fsmonitor", str(monitor)],
        check=True,
    )

    with kb.connect() as conn:
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        assert validate_execution_readiness(conn, task_id).ready
    assert not marker.exists()

    evidence._git(repo, "status", "--porcelain=v1")
    assert not marker.exists()


def test_claim_requires_readiness_and_respects_dependency(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="dependency", assignee="coder")
        child_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha, dependencies=(parent_id,)),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
            parents=(parent_id,),
        )
        readiness = validate_execution_readiness(conn, child_id)
        assert readiness.ready is False
        assert "dependencies_not_done" in readiness.blockers
        assert _claim_execution_leaf(conn, child_id) is None

        assert kb.claim_task(conn, parent_id, claimer="test:parent") is not None
        assert kb.complete_task(conn, parent_id, result="done") is True
        kb.recompute_ready(conn)
        waiting = kb.get_task(conn, child_id)
        assert waiting is not None and waiting.status == "todo"
        assert _claim_execution_leaf(conn, child_id, ttl_seconds=60) is None
        _enable_test_dispatch(conn)
        claimed = _claim_execution_leaf(
            conn,
            child_id,
            ttl_seconds=60,
            dispatch_enabled=True,
        )
        assert claimed is not None
        assert claimed.current_run_id is not None


def test_controller_owns_first_evidence_and_wall_clock_deadlines(
    kanban_home, tmp_path, monkeypatch
):
    first_repo = tmp_path / "first-repo"
    runtime_repo = tmp_path / "runtime-repo"
    first_pin = _init_repo(first_repo)
    runtime_pin = _init_repo(runtime_repo)
    with kb.connect() as conn:
        state = _enable_test_dispatch(conn)
        first_id = register_execution_leaf(
            conn,
            spec=_spec(
                first_pin,
                leaf_id="first-evidence",
                first_evidence_seconds=60,
            ),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=first_repo,
        )
        claimed_at = int(time.time())
        first = _claim_execution_leaf(
            conn,
            first_id,
            ttl_seconds=3600,
            dispatch_enabled=True,
        )
        assert first is not None
        assert first.claim_expires is not None
        assert first.claim_expires <= claimed_at + 60

        runtime_id = register_execution_leaf(
            conn,
            spec=_spec(
                runtime_pin,
                leaf_id="runtime-budget",
                first_evidence_seconds=60,
                wall_clock_budget_seconds=60,
            ),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=runtime_repo,
        )
        runtime = _claim_execution_leaf(
            conn,
            runtime_id,
            dispatch_enabled=True,
        )
        assert runtime is not None
        kb._set_worker_pid(conn, runtime_id, 424242)
        expired = int(time.time()) - 1
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (expired, first_id),
        )
        conn.execute(
            "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
            (expired, first.current_run_id),
        )
        old_started = int(time.time()) - 70
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = ?",
            (old_started, runtime.current_run_id),
        )
        conn.commit()
        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *_args, **_kwargs: {
                "host_local": True,
                "termination_attempted": True,
                "terminated": True,
            },
        )

        run_workflow_controller_tick(
            conn,
            controller_epoch=state.controller_epoch,
            process_alive=lambda _pid: True,
        )
        assert kb.get_task(conn, first_id).status == "ready"
        assert kb.get_task(conn, runtime_id).status == "ready"


def test_max_runtime_failure_keeps_expired_fence_and_worker_ownership(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        _enable_test_dispatch(conn)
        task_id = register_execution_leaf(
            conn,
            spec=_spec(
                pin_sha,
                first_evidence_seconds=60,
                wall_clock_budget_seconds=60,
            ),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        claimed = _claim_execution_leaf(conn, task_id, dispatch_enabled=True)
        assert claimed is not None
        kb._set_worker_pid(conn, task_id, 424242)
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = ?",
            (int(time.time()) - 70, claimed.current_run_id),
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

        assert kb.enforce_max_runtime(conn, execution_only=True) == []
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "running"
        assert current.worker_pid == 424242
        assert current.claim_lock == claimed.claim_lock
        assert current.claim_expires < int(time.time())
        assert not kb.complete_task(
            conn,
            task_id,
            result="late completion",
            expected_run_id=claimed.current_run_id,
            expected_claim_lock=claimed.claim_lock,
        )
        assert any(
            event.kind == "runtime_termination_deferred"
            for event in kb.list_events(conn, task_id, allow_execution_leaf=True)
        )


@pytest.mark.parametrize(
    ("worker_pid", "claim_lock_prefix"),
    [
        (None, None),
        (424242, "remote-host"),
    ],
)
def test_expired_execution_claim_stays_fenced_without_confirmed_termination(
    kanban_home, tmp_path, worker_pid, claim_lock_prefix
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        _enable_test_dispatch(conn)
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        claimed = _claim_execution_leaf(conn, task_id, dispatch_enabled=True)
        assert claimed is not None
        claim_lock = claimed.claim_lock
        if claim_lock_prefix is not None:
            claim_lock = f"{claim_lock_prefix}:{claim_lock}"
        expired = int(time.time()) - 1
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, claim_lock = ?, claim_expires = ? "
            "WHERE id = ?",
            (worker_pid, claim_lock, expired, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ?, claim_lock = ?, claim_expires = ? "
            "WHERE id = ?",
            (worker_pid, claim_lock, expired, claimed.current_run_id),
        )
        conn.commit()

        assert kb.release_stale_claims(conn, execution_only=True) == 0
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "running"
        assert current.claim_lock == claim_lock
        assert current.claim_expires == expired
        assert (
            kb.get_run(conn, claimed.current_run_id, allow_execution_leaf=True).ended_at
            is None
        )


def test_missing_worker_identity_stays_owned_during_reconcile_and_supersession(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        _enable_test_dispatch(conn)
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        claimed = _claim_execution_leaf(conn, task_id, dispatch_enabled=True)
        assert claimed is not None

        report = reconcile_execution_leaves(conn, process_alive=lambda _pid: False)
        assert "missing_worker_identity" in report.findings
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "running"
        assert current.current_run_id == claimed.current_run_id
        assert current.claim_lock == claimed.claim_lock
        assert not supersede_execution_leaf(conn, task_id, reason="replacement")
        assert (
            kb.get_run(conn, claimed.current_run_id, allow_execution_leaf=True).ended_at
            is None
        )


def test_protected_claim_refuses_to_close_an_unended_prior_run(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        _enable_test_dispatch(conn)
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        first = _claim_execution_leaf(conn, task_id, dispatch_enabled=True)
        assert first is not None
        conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
            (task_id,),
        )
        conn.commit()

        assert _claim_execution_leaf(conn, task_id, dispatch_enabled=True) is None
        assert (
            kb.get_run(conn, first.current_run_id, allow_execution_leaf=True).ended_at
            is None
        )


def test_frozen_dependency_survives_parent_deletion_and_is_rechecked_at_claim(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="dependency", assignee="coder")
        child_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha, dependencies=(parent_id,)),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
            parents=(parent_id,),
        )
        assert kb.claim_task(conn, parent_id, claimer="test:parent") is not None
        assert kb.complete_task(conn, parent_id, result="done")
        assert kb.archive_task(conn, parent_id)
        with pytest.raises(PermissionError, match="protected Workflow dependency"):
            kb.delete_archived_task(conn, parent_id)

        _enable_test_dispatch(conn)
        assert validate_execution_readiness(conn, child_id).ready
        conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?",
            (parent_id, child_id),
        )
        conn.commit()
        readiness = validate_execution_readiness(conn, child_id)
        assert readiness.ready is False
        assert "dependency_snapshot_mismatch" in readiness.blockers
        assert (
            kb.claim_task(
                conn,
                child_id,
                allow_execution_leaf=True,
            )
            is None
        )
        assert _claim_execution_leaf(conn, child_id, dispatch_enabled=True) is None


def test_same_repository_dependency_candidate_must_be_in_child_pin(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo-dependency-coordinate"
    base_sha = _init_repo(repo)
    (repo / "src" / "parent.py").write_text("PARENT = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "parent candidate"],
        check=True,
        capture_output=True,
    )
    parent_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with kb.connect() as conn:
        parent_id = register_execution_leaf(
            conn,
            spec=_spec(parent_sha, leaf_id="phase-one-parent"),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        assert kb.complete_task(
            conn,
            parent_id,
            result="controller closeout",
            force_execution_admin=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "reset", "--hard", base_sha],
            check=True,
            capture_output=True,
        )
        child_id = register_execution_leaf(
            conn,
            spec=_spec(
                base_sha,
                leaf_id="phase-two-child",
                dependencies=(parent_id,),
            ),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
            parents=(parent_id,),
        )
        missing_closeout = validate_execution_readiness(conn, child_id)
        assert missing_closeout.ready is False
        assert "dependency_candidate_missing" in missing_closeout.blockers

        now = int(time.time())
        conn.execute(
            "INSERT INTO workflow_run_closeout "
            "(run_id, task_id, candidate_sha, diff_digest, required_ci, "
            "review_approved, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, 1, ?, ?)",
            (9001, parent_id, parent_sha, "a" * 64, now, now),
        )
        conn.commit()
        readiness = validate_execution_readiness(conn, child_id)
        assert readiness.ready is False
        assert "dependency_candidate_not_in_pin" in readiness.blockers

        assert supersede_execution_leaf(
            conn,
            child_id,
            reason="dependency coordinates require an integrated successor pin",
        )
        subprocess.run(
            ["git", "-C", str(repo), "reset", "--hard", parent_sha],
            check=True,
            capture_output=True,
        )
        successor_id = register_execution_leaf(
            conn,
            spec=_spec(
                parent_sha,
                version=2,
                leaf_id="phase-two-child",
                dependencies=(parent_id,),
            ),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
            parents=(parent_id,),
        )
        successor_readiness = validate_execution_readiness(conn, successor_id)

    assert successor_readiness.ready is True
    assert successor_readiness.blockers == ()


def test_controller_registration_can_create_successor_under_execution_leaf(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        parent_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha, leaf_id="parent"),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        child_id = register_execution_leaf(
            conn,
            spec=_spec(
                pin_sha,
                leaf_id="controller-successor",
                dependencies=(parent_id,),
            ),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
            parents=(parent_id,),
        )

        assert child_id != parent_id
        assert kb.parent_ids(conn, child_id) == [parent_id]


def test_claim_rechecks_remote_pause_after_readiness(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        state = _enable_test_dispatch(conn)

    readiness_checked = threading.Event()
    continue_claim = threading.Event()
    original = execution.validate_execution_readiness

    def delayed_readiness(conn, candidate_id):
        result = original(conn, candidate_id)
        readiness_checked.set()
        assert continue_claim.wait(timeout=5)
        return result

    monkeypatch.setattr(execution, "validate_execution_readiness", delayed_readiness)

    def claim():
        with kb.connect() as conn:
            return _claim_execution_leaf(conn, task_id, dispatch_enabled=True)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(claim)
        assert readiness_checked.wait(timeout=5)
        with kb.connect() as conn:
            set_workflow_dispatch_enabled(
                conn,
                enabled=False,
                expected_version=state.version,
                actor="workflow-test",
                reason="pause raced with claim",
            )
        continue_claim.set()
        assert future.result(timeout=5) is None

    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "ready"


def test_claim_rejects_stale_caller_controller_epoch(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        stale = _enable_test_dispatch(conn)

        begin_workflow_controller_epoch(conn, controller_epoch="successor-epoch")
        run_workflow_controller_tick(
            conn,
            controller_epoch="successor-epoch",
            process_alive=lambda _pid: False,
        )
        current = _enable_test_dispatch(conn)
        assert current.controller_epoch == "successor-epoch"

        assert (
            _claim_execution_leaf(
                conn,
                task_id,
                dispatch_enabled=True,
                expected_controller_epoch=stale.controller_epoch,
            )
            is None
        )
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "ready"


def test_claim_rejects_workspace_lock_contention(kanban_home, tmp_path):
    fcntl = pytest.importorskip("fcntl")
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        state = _enable_test_dispatch(conn)

        lock_path = execution._workspace_claim_lock_path(repo)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert (
                _claim_execution_leaf(
                    conn,
                    task_id,
                    dispatch_enabled=True,
                    expected_controller_epoch=state.controller_epoch,
                )
                is None
            )

        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "ready"


def test_claim_rejects_starting_or_stale_controller(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        begin_workflow_controller_epoch(conn, controller_epoch="starting-epoch")
        conn.execute(
            "UPDATE workflow_controller_state SET dispatch_enabled = 1, broker_ready = 1 "
            "WHERE singleton = 1"
        )
        conn.commit()
        assert _claim_execution_leaf(conn, task_id, dispatch_enabled=True) is None

        conn.execute(
            "UPDATE workflow_controller_state SET status = 'healthy', heartbeat_at = ? "
            "WHERE singleton = 1",
            (int(time.time()) - execution.WORKFLOW_CONTROLLER_STALE_SECONDS - 1,),
        )
        conn.commit()
        assert _claim_execution_leaf(conn, task_id, dispatch_enabled=True) is None


def test_concurrent_claims_reserve_one_shared_workspace(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_ids = [
            register_execution_leaf(
                conn,
                spec=_spec(pin_sha, leaf_id=leaf_id),
                capsule=_capsule(),
                assignee="coder",
                workspace_path=repo,
            )
            for leaf_id in ("workspace-a", "workspace-b")
        ]
        _enable_test_dispatch(conn)

    def claim(candidate_id):
        with kb.connect() as conn:
            claimed = _claim_execution_leaf(
                conn,
                candidate_id,
                dispatch_enabled=True,
            )
            return claimed.id if claimed is not None else None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, task_ids))

    assert sum(result is not None for result in results) == 1


def test_workspace_reservation_is_shared_across_boards_until_confirmed_release(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    kb.create_board("secondary", name="Secondary")
    kb.init_db(board="secondary")

    with kb.connect(board=kb.DEFAULT_BOARD) as first_conn:
        _enable_test_dispatch(first_conn)
        first_id = register_execution_leaf(
            first_conn,
            spec=_spec(pin_sha, leaf_id="cross-board-first"),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
            board=kb.DEFAULT_BOARD,
        )
        first = _claim_execution_leaf(
            first_conn,
            first_id,
            dispatch_enabled=True,
        )
        assert first is not None
        kb._set_worker_pid(first_conn, first_id, 424242)

    with kb.connect(board="secondary") as second_conn:
        _enable_test_dispatch(second_conn)
        second_id = register_execution_leaf(
            second_conn,
            spec=_spec(pin_sha, leaf_id="cross-board-second"),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
            board="secondary",
        )
        readiness = validate_execution_readiness(second_conn, second_id)
        assert readiness.ready is False
        assert "workspace_in_use" in readiness.blockers
        assert (
            _claim_execution_leaf(
                second_conn,
                second_id,
                dispatch_enabled=True,
            )
            is None
        )

    with kb.connect(board=kb.DEFAULT_BOARD) as first_conn:
        assert kb.complete_task(
            first_conn,
            first_id,
            result="done",
            expected_run_id=first.current_run_id,
            expected_claim_lock=first.claim_lock,
            force_execution_admin=True,
        )
        reconcile_execution_leaves(first_conn, process_alive=lambda _pid: True)

    with kb.connect(board="secondary") as second_conn:
        assert (
            _claim_execution_leaf(
                second_conn,
                second_id,
                dispatch_enabled=True,
            )
            is None
        )

    with kb.connect(board=kb.DEFAULT_BOARD) as first_conn:
        reconcile_execution_leaves(first_conn, process_alive=lambda _pid: False)

    with kb.connect(board="secondary") as second_conn:
        claimed = _claim_execution_leaf(
            second_conn,
            second_id,
            dispatch_enabled=True,
        )
        assert claimed is not None


def test_unfinished_dependency_of_protected_leaf_cannot_be_archived(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="dependency", assignee="coder")
        child_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha, dependencies=(parent_id,)),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
            parents=(parent_id,),
        )

        with pytest.raises(
            PermissionError, match="unfinished protected Workflow dependency"
        ):
            kb.archive_task(conn, parent_id)
        assert kb.get_task(conn, parent_id).status != "archived"
        assert not validate_execution_readiness(conn, child_id).ready

        assert kb.claim_task(conn, parent_id, claimer="test:parent") is not None
        assert kb.complete_task(conn, parent_id, result="done")
        assert kb.archive_task(conn, parent_id)
        readiness = validate_execution_readiness(conn, child_id)
        assert readiness.ready is True


def test_registration_binds_and_freezes_exact_dependency_edges(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="dependency", assignee="coder")
        with pytest.raises(ValueError, match="spec dependencies must exactly match"):
            register_execution_leaf(
                conn,
                spec=_spec(pin_sha),
                capsule=_capsule(),
                assignee="coder",
                workspace_path=repo,
                parents=(parent_id,),
            )

        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha, dependencies=(parent_id,)),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
            parents=(parent_id,),
        )
        with pytest.raises(PermissionError, match="execution dependencies are frozen"):
            kb.unlink_tasks(conn, parent_id, task_id)
        other_parent = kb.create_task(conn, title="other dependency", assignee="coder")
        with pytest.raises(PermissionError, match="execution dependencies are frozen"):
            kb.link_tasks(conn, other_parent, task_id)
        ordinary_child = kb.create_task(conn, title="ordinary child", assignee="coder")
        with pytest.raises(PermissionError, match="controller-only"):
            kb.link_tasks(conn, task_id, ordinary_child)
        with pytest.raises(PermissionError, match="controller-only"):
            kb.unlink_tasks(conn, task_id, ordinary_child)

        parents = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (task_id,)
        ).fetchall()
        assert [row["parent_id"] for row in parents] == [parent_id]


def test_same_leaf_version_rejects_material_spec_change(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        changed = _spec(pin_sha)
        changed = LeafSpec(**{
            **changed.__dict__,
            "objective": "A materially different objective.",
        })
        with pytest.raises(ValueError, match="leaf_key collision"):
            register_execution_leaf(
                conn,
                spec=changed,
                capsule=_capsule(),
                assignee="coder",
                workspace_path=repo,
            )

        with pytest.raises(ValueError, match="active leaf family version"):
            register_execution_leaf(
                conn,
                spec=_spec(pin_sha, version=2),
                capsule=_capsule(),
                assignee="coder",
                workspace_path=repo,
            )

        assert supersede_execution_leaf(
            conn,
            task_id,
            reason="material objective changed",
        )
        superseded = kb.get_task(conn, task_id)
        assert superseded is not None and superseded.status == "archived"
        assert any(
            event.kind == "execution_superseded"
            for event in kb.list_events(conn, task_id, allow_execution_leaf=True)
        )
        next_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha, version=2),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        assert next_id != task_id


def test_supersession_does_not_release_ownership_when_worker_survives(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        _enable_test_dispatch(conn)
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        claimed = _claim_execution_leaf(conn, task_id, dispatch_enabled=True)
        assert claimed is not None
        kb._set_worker_pid(conn, task_id, 424242)
        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *_args, **_kwargs: {
                "host_local": True,
                "termination_attempted": True,
                "terminated": False,
            },
        )

        assert not supersede_execution_leaf(conn, task_id, reason="replacement")
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "running"
        assert current.current_run_id == claimed.current_run_id
        assert current.claim_lock == claimed.claim_lock


def test_restart_reconciliation_reports_non_active_live_current_run(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        _enable_test_dispatch(conn)
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        claimed = _claim_execution_leaf(conn, task_id, dispatch_enabled=True)
        assert claimed is not None
        kb._set_worker_pid(conn, task_id, 424242)
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (task_id,))
        conn.commit()

        report = reconcile_execution_leaves(conn, process_alive=lambda _pid: True)
        assert report.adopted_task_ids == ()
        assert "non_active_live_current_run" in report.findings
        current = kb.get_task(conn, task_id)
        assert current is not None and current.status == "blocked"
        assert (
            kb.get_run(conn, claimed.current_run_id, allow_execution_leaf=True).status
            == "running"
        )

        successor_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha, leaf_id="other-family"),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        successor_readiness = validate_execution_readiness(conn, successor_id)
        assert successor_readiness.ready is False
        assert "workspace_in_use" in successor_readiness.blockers
        assert (
            kb.claim_task(
                conn,
                successor_id,
                allow_execution_leaf=True,
            )
            is None
        )


def test_leaf_spec_canonicalizes_identity_before_hashing_and_registration(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    canonical = _spec(pin_sha)
    padded = LeafSpec(**{
        **canonical.__dict__,
        "repository": f"  {canonical.repository.upper()}  ",
        "campaign_issue": f"  0{canonical.campaign_issue}  ",
        "leaf_id": f"  {canonical.leaf_id}  ",
        "objective": f"  {canonical.objective}  ",
        "pin_sha": pin_sha.upper(),
    })
    assert padded.payload() == canonical.payload()
    assert padded.leaf_key == canonical.leaf_key
    assert padded.leaf_family_key == canonical.leaf_family_key

    with kb.connect() as conn:
        first_id = register_execution_leaf(
            conn,
            spec=canonical,
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        assert (
            register_execution_leaf(
                conn,
                spec=padded,
                capsule=_capsule(),
                assignee="coder",
                workspace_path=repo,
            )
            == first_id
        )


def test_readiness_rejects_worktree_used_by_another_active_attempt(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        _enable_test_dispatch(conn)
        first_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        assert (
            _claim_execution_leaf(conn, first_id, ttl_seconds=60, dispatch_enabled=True)
            is not None
        )

        second_spec = LeafSpec(**{
            **_spec(pin_sha).__dict__,
            "leaf_id": "second-path-disjoint-leaf",
        })
        second_id = register_execution_leaf(
            conn,
            spec=second_spec,
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )

        readiness = validate_execution_readiness(conn, second_id)
        assert readiness.ready is False
        assert "workspace_in_use" in readiness.blockers
        assert _claim_execution_leaf(conn, second_id) is None


def test_restart_reconciliation_adopts_current_and_closes_dead_orphan(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        _enable_test_dispatch(conn)
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        current = _claim_execution_leaf(
            conn, task_id, ttl_seconds=60, dispatch_enabled=True
        )
        assert current is not None
        kb._set_worker_pid(conn, task_id, os.getpid())
        orphan = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, "
            "claim_expires, started_at, leaf_key, spec_hash, pin_sha, capsule_hash) "
            "VALUES (?, 'coder', 'running', 'dead:1:token', ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                current.claim_expires,
                current.started_at,
                current.leaf_key,
                current.spec_hash,
                current.pin_sha,
                current.capsule_hash,
            ),
        ).lastrowid
        conn.commit()

        report = reconcile_execution_leaves(
            conn, process_alive=lambda pid: pid == os.getpid()
        )
        assert report.adopted_task_ids == (task_id,)
        assert report.closed_orphan_run_ids == (orphan,)
        assert report.quarantined_task_ids == ()
        assert kb.get_task(conn, task_id).status == "running"
        assert kb.get_run(conn, orphan, allow_execution_leaf=True).status == "reclaimed"


def test_restart_reconciliation_leaves_dead_current_run_for_terminal_ingestion(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo-dead-current"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        _enable_test_dispatch(conn)
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        current = _claim_execution_leaf(
            conn, task_id, ttl_seconds=60, dispatch_enabled=True
        )
        assert current is not None
        kb._set_worker_pid(conn, task_id, 424242)

        report = reconcile_execution_leaves(conn, process_alive=lambda _pid: False)

        assert report.adopted_task_ids == (task_id,)
        assert report.quarantined_task_ids == ()
        assert report.findings == ()
        persisted = kb.get_task(conn, task_id)
        assert persisted is not None
        assert persisted.status == "running"
        assert persisted.current_run_id == current.current_run_id
        assert persisted.worker_pid == 424242


def test_restart_reconciliation_preserves_ownership_when_live_orphan_survives(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    terminations = []
    with kb.connect() as conn:
        _enable_test_dispatch(conn)
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        current = _claim_execution_leaf(conn, task_id, dispatch_enabled=True)
        assert current is not None
        kb._set_worker_pid(conn, task_id, 111111)
        orphan = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, "
            "claim_expires, worker_pid, started_at, leaf_key, spec_hash, pin_sha, "
            "capsule_hash) VALUES (?, 'coder', 'running', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                "local:orphan-token",
                current.claim_expires,
                222222,
                current.started_at,
                current.leaf_key,
                current.spec_hash,
                current.pin_sha,
                current.capsule_hash,
            ),
        ).lastrowid
        conn.commit()

        def survive(pid, claim_lock):
            terminations.append((pid, claim_lock))
            return {
                "host_local": True,
                "termination_attempted": True,
                "terminated": False,
            }

        monkeypatch.setattr(kb, "_terminate_reclaimed_worker", survive)
        report = reconcile_execution_leaves(conn, process_alive=lambda _pid: True)
        assert terminations == [(222222, "local:orphan-token")]
        assert "live_orphan_run" in report.findings
        assert kb.get_task(conn, task_id).status == "running"
        assert kb.get_run(conn, orphan, allow_execution_leaf=True).status == "running"


def test_restart_reconciliation_keeps_current_run_without_worker_identity_owned(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        _enable_test_dispatch(conn)
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        current = _claim_execution_leaf(
            conn, task_id, ttl_seconds=60, dispatch_enabled=True
        )
        assert current is not None
        assert current.worker_pid is None

        report = reconcile_execution_leaves(conn, process_alive=lambda _pid: False)

        assert report.adopted_task_ids == ()
        assert report.quarantined_task_ids == ()
        assert "missing_worker_identity" in report.findings
        persisted = kb.get_task(conn, task_id)
        assert persisted.status == "running"
        assert persisted.claim_lock == current.claim_lock
        assert persisted.current_run_id == current.current_run_id


def test_restart_reconciliation_keeps_identity_mismatch_owned(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    pin_sha = _init_repo(repo)
    with kb.connect() as conn:
        _enable_test_dispatch(conn)
        task_id = register_execution_leaf(
            conn,
            spec=_spec(pin_sha),
            capsule=_capsule(),
            assignee="coder",
            workspace_path=repo,
        )
        current = _claim_execution_leaf(
            conn, task_id, ttl_seconds=60, dispatch_enabled=True
        )
        assert current is not None
        conn.execute(
            "UPDATE task_runs SET spec_hash = ? WHERE id = ?",
            ("c" * 64, current.current_run_id),
        )
        conn.commit()

        report = reconcile_execution_leaves(conn, process_alive=lambda _pid: False)
        assert report.adopted_task_ids == ()
        assert report.quarantined_task_ids == ()
        assert "identity_mismatch" in report.findings
        persisted = kb.get_task(conn, task_id)
        assert persisted.status == "running"
        assert persisted.claim_lock == current.claim_lock
        assert persisted.current_run_id == current.current_run_id
