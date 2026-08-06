from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kb.connect()
    try:
        yield conn, tmp_path
    finally:
        conn.close()


def _claimed_task(conn, workspace: Path, *, model="model-a", provider="provider-a"):
    task_id = kb.create_task(
        conn,
        title="continue safely",
        assignee="worker-a",
        workspace_kind="dir",
        workspace_path=str(workspace),
        model_override=model,
        provider_override=provider,
        max_retries=3,
    )
    task = kb.claim_task(conn, task_id, claimer="host:1")
    assert task is not None
    return task


def test_iteration_timeout_atomically_persists_durable_handoff_and_cumulative_usage(board):
    conn, tmp_path = board
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = _claimed_task(conn, workspace)

    blocked = kb.record_iteration_timeout(
        conn,
        task.id,
        expected_run_id=task.current_run_id,
        summary="Implemented parser; remaining: integration tests.",
        checkpoint={
            "worker_session_id": "sess-worker-1",
            "workspace": str(workspace),
            "profile": "worker-a",
            "model": "model-a",
            "provider": "provider-a",
            "model_override": "model-a",
            "provider_override": "provider-a",
            "reasoning_effort": None,
            "branch": "fix/continuation",
            "head": "a" * 40,
            "dirty_hash": "dirty-1",
            "dirty_files": ["parser.py", "tests/test_parser.py"],
        },
        budget_used=90,
        budget_max=100,
        soft_checkpoint=True,
    )

    assert blocked is False
    stored_task = kb.get_task(conn, task.id)
    assert stored_task is not None
    assert stored_task.status == "ready"
    assert stored_task.current_run_id is None

    run = kb.list_runs(conn, task.id, include_active=False)[-1]
    assert run.outcome == "timed_out"
    assert run.summary == "Implemented parser; remaining: integration tests."
    assert run.metadata is not None
    assert run.metadata["worker_session_id"] == "sess-worker-1"
    assert run.metadata["task_id"] == task.id
    assert run.metadata["run_id"] == task.current_run_id
    assert run.metadata["workspace"] == str(workspace.resolve())
    assert run.metadata["budget_used"] == 90
    assert run.metadata["budget_max"] == 100
    assert run.metadata["soft_checkpoint"] is True
    assert run.metadata["timeout_retries"] == 1
    assert run.metadata["cumulative_iterations"] == 90
    assert run.metadata["progress_made"] is False
    assert run.metadata["no_progress_retries"] == 1
    assert stored_task.consecutive_failures == 1

    event = kb.list_events(conn, task.id)[-1]
    assert event.kind == "timed_out"
    assert event.run_id == task.current_run_id
    assert event.payload["cumulative_iterations"] == 90
    assert event.payload["timeout_retries"] == 1


def test_iteration_timeout_rejects_stale_run_without_releasing_current_claim(board):
    conn, tmp_path = board
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = _claimed_task(conn, workspace)

    with pytest.raises(kb.KanbanContinuationConflict):
        kb.record_iteration_timeout(
            conn,
            task.id,
            expected_run_id=int(task.current_run_id) + 1,
            summary="stale worker",
            checkpoint={"worker_session_id": "stale"},
            budget_used=90,
            budget_max=100,
            soft_checkpoint=True,
        )

    stored = kb.get_task(conn, task.id)
    assert stored is not None
    assert stored.status == "running"
    assert stored.current_run_id == task.current_run_id


def test_timeout_retry_limit_counts_stagnation_not_productive_checkpoints(board):
    conn, tmp_path = board
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = _claimed_task(conn, workspace)

    def timeout(head: str) -> bool:
        nonlocal task
        blocked = kb.record_iteration_timeout(
            conn,
            task.id,
            expected_run_id=task.current_run_id,
            summary=f"checkpoint at {head}",
            checkpoint={
                "worker_session_id": f"sess-{task.current_run_id}",
                "workspace": str(workspace),
                "profile": "worker-a",
                "model": "model-a",
                "provider": "provider-a",
                "branch": "fix/continuation",
                "head": head,
                "dirty_hash": "clean",
                "dirty_files": [],
            },
            budget_used=90,
            budget_max=100,
            soft_checkpoint=True,
        )
        if not blocked:
            task = kb.claim_task(conn, task.id, claimer=f"host:{head}:{task.current_run_id}")
            assert task is not None
        return blocked

    assert timeout("a" * 40) is False
    assert timeout("b" * 40) is False  # new commit resets stagnation
    assert timeout("b" * 40) is False
    assert timeout("b" * 40) is False
    assert timeout("b" * 40) is True

    stored = kb.get_task(conn, task.id)
    assert stored is not None
    assert stored.status == "blocked"
    assert stored.consecutive_failures == 3
    latest = kb.list_runs(conn, task.id, include_active=False)[-1]
    assert latest.metadata["timeout_retries"] == 5
    assert latest.metadata["no_progress_retries"] == 3
    assert latest.metadata["cumulative_iterations"] == 450


def test_checkpoint_hash_changes_when_dirty_file_contents_change(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def git(*args):
        subprocess.run(
            ["git", *args],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-q")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.com")
    tracked = workspace / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-qm", "test: seed repository")

    tracked.write_text("first dirty contents\n", encoding="utf-8")
    untracked = workspace / "untracked.txt"
    untracked.write_text("first untracked contents\n", encoding="utf-8")
    first = kb.capture_worker_checkpoint(
        worker_session_id="session",
        workspace=str(workspace),
        profile="worker",
        model="model",
        provider="provider",
    )

    tracked.write_text("second dirty contents\n", encoding="utf-8")
    untracked.write_text("second untracked contents\n", encoding="utf-8")
    second = kb.capture_worker_checkpoint(
        worker_session_id="session",
        workspace=str(workspace),
        profile="worker",
        model="model",
        provider="provider",
    )

    assert first["dirty_files"] == second["dirty_files"]
    assert first["dirty_hash"] != second["dirty_hash"]


def test_checkpoint_hash_changes_when_only_staged_contents_change(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def git(*args):
        subprocess.run(
            ["git", *args],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-q")
    git("config", "user.name", "Test User")
    git("config", "user.email", "test@example.com")
    tracked = workspace / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-qm", "test: seed repository")

    tracked.write_text("staged version a\n", encoding="utf-8")
    git("add", "tracked.txt")
    tracked.write_text("worktree version\n", encoding="utf-8")
    first = kb.capture_worker_checkpoint(
        worker_session_id="session",
        workspace=str(workspace),
        profile="worker",
        model="model",
        provider="provider",
    )

    tracked.write_text("staged version c\n", encoding="utf-8")
    git("add", "tracked.txt")
    tracked.write_text("worktree version\n", encoding="utf-8")
    second = kb.capture_worker_checkpoint(
        worker_session_id="session",
        workspace=str(workspace),
        profile="worker",
        model="model",
        provider="provider",
    )

    assert first["dirty_files"] == second["dirty_files"]
    assert first["dirty_hash"] != second["dirty_hash"]


def _resume_metadata(tmp_path: Path) -> tuple[dict, dict, dict]:
    workspace = str((tmp_path / "workspace").resolve())
    prior = {
        "task_id": "t_resume",
        "run_id": 11,
        "worker_session_id": "sess-1",
        "workspace": workspace,
        "profile": "worker-a",
        "model": "model-a",
        "provider": "provider-a",
        "model_override": "model-a",
        "provider_override": "provider-a",
        "reasoning_effort": "high",
        "branch": "fix/continuation",
        "head": "b" * 40,
    }
    current = {
        "task_id": "t_resume",
        "run_id": 12,
        "workspace": workspace,
        "profile": "worker-a",
        "model": "model-a",
        "provider": "provider-a",
        "model_override": "model-a",
        "provider_override": "provider-a",
        "reasoning_effort": "high",
        "branch": "fix/continuation",
        "head": "b" * 40,
        "active_run_count": 1,
    }
    session = {
        "id": "sess-1",
        "source": "kanban",
        "profile_name": "worker-a",
        "cwd": workspace,
        "model": "model-a",
        "model_config": json.dumps({"provider": "provider-a"}),
    }
    return prior, current, session


def test_compatible_worker_resume_requires_exact_identity_match(tmp_path):
    prior, current, session = _resume_metadata(tmp_path)

    assert kb.compatible_worker_resume_session(prior, current, session) == "sess-1"


def test_resolver_selects_persisted_compatible_kanban_session(board):
    from hermes_state import SessionDB

    conn, tmp_path = board
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_home = tmp_path / ".hermes" / "profiles" / "worker-a"
    profile_home.mkdir(parents=True)
    profile_home.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")

    task = _claimed_task(conn, workspace)
    sessions = SessionDB(profile_home / "state.db")
    sessions.create_session(
        session_id="sess-compatible",
        source="kanban",
        model="model-a",
        model_config={"provider": "provider-a"},
        cwd=str(workspace.resolve()),
        profile_name="worker-a",
    )
    sessions.close()

    blocked = kb.record_iteration_timeout(
        conn,
        task.id,
        expected_run_id=task.current_run_id,
        summary="safe handoff",
        checkpoint={
            "worker_session_id": "sess-compatible",
            "workspace": str(workspace),
            "profile": "worker-a",
            "model": "model-a",
            "provider": "provider-a",
            "branch": None,
            "head": None,
            "dirty_hash": None,
            "dirty_files": [],
        },
        budget_used=90,
        budget_max=100,
        soft_checkpoint=True,
    )
    assert blocked is False
    retry = kb.claim_task(conn, task.id, claimer="host:retry")
    assert retry is not None

    assert kb._resolve_worker_resume_session(
        retry, str(workspace), str(profile_home), board="default"
    ) == "sess-compatible"


@pytest.mark.parametrize(
    ("surface", "key", "changed"),
    [
        ("prior", "task_id", "t_other"),
        ("prior", "workspace", "/tmp/other"),
        ("prior", "profile", "worker-b"),
        ("prior", "model", "model-b"),
        ("prior", "provider", "provider-b"),
        ("prior", "model_override", "model-b"),
        ("prior", "provider_override", "provider-b"),
        ("prior", "reasoning_effort", "low"),
        ("prior", "branch", "other"),
        ("prior", "head", "c" * 40),
        ("prior", "dirty_hash", "dirty-2"),
        ("current", "active_run_count", 2),
        ("session", "source", "cli"),
        ("session", "profile_name", "worker-b"),
        ("session", "cwd", "/tmp/other"),
        ("session", "model", "model-b"),
        ("session", "model_config", "{}"),
    ],
)
def test_compatible_worker_resume_rejects_changed_ownership_workspace_route_or_head(
    tmp_path, surface, key, changed
):
    prior, current, session = _resume_metadata(tmp_path)
    {"prior": prior, "current": current, "session": session}[surface][key] = changed

    assert kb.compatible_worker_resume_session(prior, current, session) is None
