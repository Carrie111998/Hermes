from __future__ import annotations

import json
import multiprocessing
import queue
import subprocess
import time
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB
from hermes_cli import kanban_db as kdb
from hermes_cli import projects_db as pdb
from hermes_cli.interactive_workspace import (
    InteractiveWorkspaceError,
    InteractiveWorkspaceRequest,
    mark_interactive_task_session_connected,
    start_interactive_task_workspace,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _cross_process_lock_probe(lock_path: str, acquired, hold_seconds: float) -> None:
    from hermes_cli.interactive_workspace import _cross_process_start_lock

    with _cross_process_start_lock(Path(lock_path)):
        acquired.put(time.monotonic())
        time.sleep(hold_seconds)


def _fixture_repo(tmp_path: Path, *, preflight_exit: int = 0) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(remote), str(repo)], check=True, capture_output=True)
    _git(repo, "checkout", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Hermes Test")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    hook_dir = repo / ".hermes"
    hook_dir.mkdir()
    (hook_dir / "preflight.py").write_text(
        "import os, sys\n"
        "required = ['HERMES_WORKSPACE_ROOT', 'HERMES_WORKSPACE_SCOPE', "
        "'HERMES_PROJECT_ID', 'HERMES_TASK_ID', 'GOLIATH_ROOT', 'GOLIATH_WRITE_SCOPE']\n"
        "missing = [name for name in required if not os.environ.get(name)]\n"
        "print('PREFLIGHT_OK' if not missing else 'MISSING:' + ','.join(missing))\n"
        f"sys.exit({preflight_exit} if not missing else 9)\n",
        encoding="utf-8",
    )
    (hook_dir / "workspace-start.json").write_text(
        json.dumps(
            {
                "preflight": {
                    "command": ["python3", ".hermes/preflight.py"],
                    "required_inputs": ["write_scope"],
                    "timeout_seconds": 30,
                    "env": {
                        "GOLIATH_ROOT": "workspace_path",
                        "GOLIATH_WRITE_SCOPE": "write_scope",
                    },
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "README.md", ".hermes")
    _git(repo, "commit", "-m", "fixture")
    _git(repo, "push", "-u", "origin", "main")
    subprocess.run(
        ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
    )
    return repo, remote


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")
    token = set_hermes_home_override(home)
    try:
        yield home
    finally:
        reset_hermes_home_override(token)


def _seed_task(home: Path, repo: Path) -> tuple[str, str, str, Path]:
    with pdb.connect_closing() as conn:
        project_id = pdb.create_project(
            conn,
            name="Fixture Project",
            slug="fixture-project",
            primary_path=str(repo),
            folders=[str(repo)],
            board_slug="default",
        )
    kdb.create_board(
        "default",
        name="Fixture",
        default_workdir=str(repo),
        project_id=project_id,
    )
    with kdb.connect_closing(board="default") as conn:
        task_id = kdb.create_task(
            conn,
            title="Interactive fixture",
            created_by="test",
            workspace_kind="worktree",
            branch_name="feat/interactive-fixture",
            project_id=project_id,
            initial_status="running",
            board="default",
        )
        task = kdb.get_task(conn, task_id)
        assert task is not None
        assert task.workspace_path
        target = Path(task.workspace_path)
    return project_id, task_id, "W-fixture", target


def _request(project_id: str, task_id: str, workstream_id: str) -> InteractiveWorkspaceRequest:
    return InteractiveWorkspaceRequest(
        project_id=project_id,
        task_id=task_id,
        workstream_id=workstream_id,
        idempotency_key=f"{project_id}:{task_id}:{workstream_id}:v1",
        write_scope="fixture subsystem",
        profile_name="fixture-profile",
    )


def test_start_persists_task_worktree_session_preflight_and_idempotent_event(
    tmp_path: Path, isolated_home: Path
):
    repo, _remote = _fixture_repo(tmp_path)
    project_id, task_id, workstream_id, target = _seed_task(isolated_home, repo)

    request = _request(project_id, task_id, workstream_id)
    first = start_interactive_task_workspace(request)
    second = start_interactive_task_workspace(request)
    connected = mark_interactive_task_session_connected(request, first.session_id)
    connected_retry = mark_interactive_task_session_connected(request, first.session_id)
    changed_intent = InteractiveWorkspaceRequest(
        project_id=project_id,
        task_id=task_id,
        workstream_id="W-other",
        idempotency_key=_request(project_id, task_id, workstream_id).idempotency_key,
        write_scope="fixture subsystem",
        profile_name="fixture-profile",
    )
    with pytest.raises(InteractiveWorkspaceError) as mismatch:
        start_interactive_task_workspace(changed_intent)

    assert first.reused is False
    assert second.reused is True
    assert second.session_id == first.session_id
    assert connected.session_id == first.session_id
    assert connected.reused is False
    assert connected_retry.reused is True
    assert mismatch.value.code == "idempotency_mismatch"
    assert first.project_id == project_id
    assert first.task_id == task_id
    assert first.workstream_id == workstream_id
    assert Path(first.workspace_path) == target
    assert first.branch == "feat/interactive-fixture"
    assert first.preflight_status == "passed"
    assert "PREFLIGHT_OK" in first.preflight_summary
    assert _git(target, "branch", "--show-current") == first.branch
    assert _git(target, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/main")

    db = SessionDB(db_path=isolated_home / "state.db")
    try:
        stored = db.get_session(first.session_id)
        assert stored is not None
        assert stored["cwd"] == str(target)
        assert stored["git_branch"] == first.branch
        assert stored["git_repo_root"] == str(repo)
        assert stored["profile_name"] == "fixture-profile"
        assert stored["message_count"] == 0
    finally:
        db.close()

    with kdb.connect_closing(board="default") as conn:
        task = kdb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert kdb.list_runs(conn, task_id) == []
        events = kdb.list_events(conn, task_id)
        starts = [event for event in events if event.kind == "interactive_workspace_prepared"]
        connected_events = [event for event in events if event.kind == "interactive_session_connected"]
        assert len(starts) == 1
        assert len(connected_events) == 1
        payload = starts[0].payload
        assert payload["session_id"] == first.session_id
        assert payload["project_id"] == project_id
        assert payload["workstream_id"] == workstream_id
        assert payload["workspace_path"] == str(target)
        assert payload["preflight_status"] == "passed"


def test_cross_process_start_lock_serializes_identical_intents(tmp_path: Path):
    context = multiprocessing.get_context("spawn")
    acquired = context.Queue()
    lock_path = tmp_path / "interactive-start.lock"
    first = context.Process(
        target=_cross_process_lock_probe,
        args=(str(lock_path), acquired, 0.5),
    )
    second = context.Process(
        target=_cross_process_lock_probe,
        args=(str(lock_path), acquired, 0.0),
    )

    first.start()
    first_acquired = acquired.get(timeout=5)
    second.start()
    with pytest.raises(queue.Empty):
        acquired.get(timeout=0.15)
    first.join(timeout=5)
    assert first.exitcode == 0
    second_acquired = acquired.get(timeout=5)
    second.join(timeout=5)
    assert second.exitcode == 0
    assert second_acquired - first_acquired >= 0.4


def test_project_task_mismatch_fails_before_workspace_or_session(
    tmp_path: Path, isolated_home: Path
):
    repo, _remote = _fixture_repo(tmp_path)
    project_id, task_id, workstream_id, target = _seed_task(isolated_home, repo)
    with pdb.connect_closing() as conn:
        wrong_project_id = pdb.create_project(
            conn,
            name="Wrong Project",
            slug="wrong-project",
            primary_path=str(repo),
            folders=[str(repo)],
            board_slug="default",
        )

    with pytest.raises(InteractiveWorkspaceError, match="does not belong") as exc_info:
        start_interactive_task_workspace(_request(wrong_project_id, task_id, workstream_id))

    assert exc_info.value.code == "project_task_mismatch"
    assert not target.exists()
    db = SessionDB(db_path=isolated_home / "state.db")
    try:
        assert db.list_sessions_rich(limit=20) == []
    finally:
        db.close()


def test_preflight_failure_is_persisted_and_cleans_only_new_worktree(
    tmp_path: Path, isolated_home: Path
):
    repo, _remote = _fixture_repo(tmp_path, preflight_exit=7)
    project_id, task_id, workstream_id, target = _seed_task(isolated_home, repo)

    with pytest.raises(InteractiveWorkspaceError, match="preflight") as exc_info:
        start_interactive_task_workspace(_request(project_id, task_id, workstream_id))

    assert exc_info.value.code == "preflight_failed"
    assert not target.exists()
    assert _git(repo, "branch", "--list", "feat/interactive-fixture") == ""
    db = SessionDB(db_path=isolated_home / "state.db")
    try:
        assert db.list_sessions_rich(limit=20) == []
    finally:
        db.close()

    with kdb.connect_closing(board="default") as conn:
        failures = [event for event in kdb.list_events(conn, task_id) if event.kind == "interactive_session_start_failed"]
        assert len(failures) == 1
        assert failures[0].payload["code"] == "preflight_failed"
        assert failures[0].payload["preflight_status"] == "failed"
