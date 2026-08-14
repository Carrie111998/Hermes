"""Workspace-conflict dispatch policy tests."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def workspace_policy(monkeypatch: pytest.MonkeyPatch):
    import hermes_cli.config as config

    def set_policy(policy: str) -> None:
        monkeypatch.setattr(
            config,
            "load_config",
            lambda: {"kanban": {"workspace_conflict": policy, "review_dispatch": True}},
        )

    return set_policy


@pytest.fixture
def spawnable_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)


def _running_dir_task(conn, path: Path) -> str:
    task_id = kb.create_task(
        conn,
        title="current owner",
        assignee="worker",
        workspace_kind="dir",
        workspace_path=str(path),
    )
    assert kb.claim_task(conn, task_id) is not None
    return task_id


def _ready_dir_task(conn, path: Path, *, title: str = "candidate") -> str:
    return kb.create_task(
        conn,
        title=title,
        assignee="worker",
        workspace_kind="dir",
        workspace_path=str(path),
    )


def test_workspace_conflict_allow_preserves_parallel_dispatch(
    kanban_home: Path,
    workspace_policy,
    spawnable_profiles: None,
    tmp_path: Path,
) -> None:
    """The default-compatible allow policy leaves shared directories alone."""
    workspace_policy("allow")
    shared = tmp_path / "shared"
    with kb.connect() as conn:
        _running_dir_task(conn, shared)
        candidate = _ready_dir_task(conn, shared)

        result = kb.dispatch_once(conn, spawn_fn=lambda *_args: None)

        assert [task_id for task_id, _assignee, _path in result.spawned] == [candidate]
        assert result.skipped_workspace_conflict == []
        assert kb.get_task(conn, candidate).status == "running"


def test_workspace_conflict_warn_dispatches_and_logs(
    kanban_home: Path,
    workspace_policy,
    spawnable_profiles: None,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace_policy("warn")
    shared = tmp_path / "shared"
    with kb.connect() as conn:
        blocker = _running_dir_task(conn, shared)
        candidate = _ready_dir_task(conn, shared)

        with caplog.at_level("WARNING"):
            result = kb.dispatch_once(conn, spawn_fn=lambda *_args: None)

        assert [task_id for task_id, _assignee, _path in result.spawned] == [candidate]
        assert f"task {candidate} workspace" in caplog.text
        assert blocker in caplog.text


def test_workspace_conflict_serialize_defers_then_dispatches_after_completion(
    kanban_home: Path,
    workspace_policy,
    spawnable_profiles: None,
    tmp_path: Path,
) -> None:
    workspace_policy("serialize")
    shared = tmp_path / "shared"
    with kb.connect() as conn:
        blocker = _running_dir_task(conn, shared)
        candidate = _ready_dir_task(conn, shared)

        deferred = kb.dispatch_once(conn, spawn_fn=lambda *_args: None)
        assert deferred.spawned == []
        assert deferred.skipped_workspace_conflict == [
            (candidate, [blocker], str(shared.resolve()))
        ]
        assert kb.get_task(conn, candidate).status == "ready"

        assert kb.complete_task(conn, blocker, result="finished")
        dispatched = kb.dispatch_once(conn, spawn_fn=lambda *_args: None)
        assert [task_id for task_id, _assignee, _path in dispatched.spawned] == [candidate]


def test_workspace_conflict_serialize_never_blocks_scratch(
    kanban_home: Path,
    workspace_policy,
    spawnable_profiles: None,
) -> None:
    workspace_policy("serialize")
    with kb.connect() as conn:
        blocker = kb.create_task(conn, title="scratch owner", assignee="worker")
        candidate = kb.create_task(conn, title="scratch candidate", assignee="worker")
        assert kb.claim_task(conn, blocker) is not None

        result = kb.dispatch_once(conn, spawn_fn=lambda *_args: None)

        assert [task_id for task_id, _assignee, _path in result.spawned] == [candidate]
        assert result.skipped_workspace_conflict == []


def test_workspace_conflict_serialize_applies_to_review_dispatch(
    kanban_home: Path,
    workspace_policy,
    spawnable_profiles: None,
    tmp_path: Path,
) -> None:
    workspace_policy("serialize")
    shared = tmp_path / "shared"
    with kb.connect() as conn:
        blocker = _running_dir_task(conn, shared)
        review_task = _ready_dir_task(conn, shared, title="review candidate")
        claimed = kb.claim_task(conn, review_task)
        assert claimed is not None
        assert kb.request_review(
            conn, review_task, summary="ready for review", expected_run_id=claimed.current_run_id,
        )

        result = kb.dispatch_once(conn, spawn_fn=lambda *_args: None)

        assert result.spawned == []
        assert result.skipped_workspace_conflict == [
            (review_task, [blocker], str(shared.resolve()))
        ]
        assert kb.get_task(conn, review_task).status == "review"


def test_workspace_conflict_serialize_applies_to_cli_claim(
    kanban_home: Path,
    workspace_policy,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_policy("serialize")
    shared = tmp_path / "shared"
    with kb.connect() as conn:
        blocker = _running_dir_task(conn, shared)
        candidate = _ready_dir_task(conn, shared)

    exit_code = kb_cli._cmd_claim(argparse.Namespace(task_id=candidate, ttl=None))

    assert exit_code == 1
    assert blocker in capsys.readouterr().err
    with kb.connect() as conn:
        assert kb.get_task(conn, candidate).status == "ready"


def _init_git_repo(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)


def test_workspace_conflict_uses_deterministic_worktree_target(
    kanban_home: Path,
    workspace_policy,
    spawnable_profiles: None,
    tmp_path: Path,
) -> None:
    """A shared named worktree is detected without creating it for the candidate."""
    workspace_policy("serialize")
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    target = repo / ".worktrees" / "shared"
    with kb.connect() as conn:
        blocker = kb.create_task(
            conn,
            title="worktree owner",
            assignee="worker",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name="shared",
            priority=1,
        )
        candidate = kb.create_task(
            conn,
            title="worktree candidate",
            assignee="worker",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name="shared",
        )
        first = kb.dispatch_once(conn, spawn_fn=lambda *_args: None, max_spawn=1)
        assert [task_id for task_id, _assignee, _path in first.spawned] == [blocker]
        assert target.is_dir()

        second = kb.dispatch_once(conn, spawn_fn=lambda *_args: None)

        assert second.spawned == []
        assert second.skipped_workspace_conflict == [
            (candidate, [blocker], str(target.resolve()))
        ]
