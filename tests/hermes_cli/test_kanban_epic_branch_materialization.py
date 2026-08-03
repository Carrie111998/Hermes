"""The epic base branch must exist by the time the first story is materialized.

Observed on Agentic OS Cockpit epic ``t_c29de776``: all three qualified story
worktrees and branches materialized, but ``epic/t_c29de776`` did not. Review
runs then failed before reviewer spawn, because ``_story_base_branch`` selects
the epic branch as the review base and ``git merge-base`` could not resolve it.
An operator had to create the ref by hand.

Root cause: only ``_spawn_one_v2`` (the handoff_v2 event consumer) passed
``base_branch=_story_base_branch(...)`` into ``_resolve_worktree_workspace``.
The time-polling ready loop, the review loop, and ``resolve_workspace`` all
called it with ``base_branch=None``, so ``_ensure_epic_branch`` never ran on
those paths and a story could materialize with its required epic base absent.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def epic_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "epic@example.com")
    _git(repo, "config", "user.name", "Epic Test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    return repo


def _v2_board(board: str, repo: Path) -> None:
    kb.ensure_product_board_defaults(board, default_workdir=str(repo))
    path = kb.board_metadata_path(board)
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.setdefault("product_workflow", {})["handoff_v2"] = True
    path.write_text(json.dumps(meta), encoding="utf-8")
    if _git(repo, "status", "--porcelain"):
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "board bootstrap")


def _epic_with_story(conn, board: str, repo: Path, title: str) -> tuple[str, str]:
    epic_id = kb.create_task(
        conn, title="Epic outcome", board=board, work_item_kind="epic",
    )
    story_id = kb.create_task(
        conn,
        title=title,
        board=board,
        assignee="developer",
        workspace_kind="worktree",
        workspace_path=str(repo),
        workflow_template_id="product",
        current_step_key="development",
    )
    kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
    return epic_id, story_id


def _add_story(conn, board: str, repo: Path, epic_id: str, title: str) -> str:
    story_id = kb.create_task(
        conn,
        title=title,
        board=board,
        assignee="developer",
        workspace_kind="worktree",
        workspace_path=str(repo),
        workflow_template_id="product",
        current_step_key="development",
    )
    kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
    return story_id


def test_ready_loop_materialization_creates_the_epic_base_branch(
    epic_home, tmp_path, all_assignees_spawnable
):
    repo = _repo(tmp_path)
    board = "epic-ready-loop"
    _v2_board(board, repo)
    base_sha = _git(repo, "rev-parse", "HEAD")
    with kb.connect(board=board) as conn:
        epic_id, story_id = _epic_with_story(conn, board, repo, "Story one")
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board=board)
        story = kb.get_task(conn, story_id)

    epic_branch = kb.epic_branch_for(epic_id)
    # The epic base exists...
    assert _git(repo, "rev-parse", "--verify", epic_branch)
    # ...at exactly the commit the first story branched from...
    assert _git(repo, "rev-parse", epic_branch) == base_sha
    assert story is not None and story.branch_name
    assert _git(repo, "merge-base", epic_branch, story.branch_name) == base_sha
    # ...and the story branch really is rooted on it.
    assert (
        _git(repo, "merge-base", "--is-ancestor", epic_branch, story.branch_name)
        == ""
    )


def test_sibling_materialization_does_not_move_the_epic_base_branch(
    epic_home, tmp_path, all_assignees_spawnable
):
    repo = _repo(tmp_path)
    board = "epic-sibling"
    _v2_board(board, repo)
    with kb.connect(board=board) as conn:
        epic_id, _first = _epic_with_story(conn, board, repo, "Story one")
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board=board)
    epic_branch = kb.epic_branch_for(epic_id)
    pinned = _git(repo, "rev-parse", epic_branch)

    # main moves on before the sibling is dispatched.
    (repo / "moved.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "moved.txt")
    _git(repo, "commit", "-m", "main moves on")
    assert _git(repo, "rev-parse", "HEAD") != pinned

    with kb.connect(board=board) as conn:
        sibling_id = _add_story(conn, board, repo, epic_id, "Story two")
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board=board)
        sibling = kb.get_task(conn, sibling_id)

    assert _git(repo, "rev-parse", epic_branch) == pinned
    assert sibling is not None and sibling.branch_name
    # The sibling branched off the epic base, not off the moved main.
    assert _git(repo, "merge-base", epic_branch, sibling.branch_name) == pinned


def test_resolve_workspace_derives_the_epic_base_without_an_explicit_argument(
    epic_home, tmp_path
):
    """The generic resolver is the seam every dispatch path shares — deriving
    the base there is what stops a new call site from reintroducing the bug."""
    repo = _repo(tmp_path)
    board = "epic-generic-resolver"
    _v2_board(board, repo)
    base_sha = _git(repo, "rev-parse", "HEAD")
    with kb.connect(board=board) as conn:
        epic_id, story_id = _epic_with_story(conn, board, repo, "Story one")
        story = kb.get_task(conn, story_id)
        assert story is not None
        kb._resolve_worktree_workspace(story, board=board, conn=conn)

    epic_branch = kb.epic_branch_for(epic_id)
    assert _git(repo, "rev-parse", epic_branch) == base_sha


def test_reusing_an_existing_story_worktree_restores_a_missing_epic_base(
    epic_home, tmp_path
):
    """The state the incident left behind: story worktrees present, epic base
    gone. The next dispatch must restore it rather than reuse the worktree and
    fail again in Review target preparation."""
    repo = _repo(tmp_path)
    board = "epic-reuse-heals"
    _v2_board(board, repo)
    base_sha = _git(repo, "rev-parse", "HEAD")
    with kb.connect(board=board) as conn:
        epic_id, story_id = _epic_with_story(conn, board, repo, "Story one")
        story = kb.get_task(conn, story_id)
        assert story is not None
        workspace, branch = kb._resolve_worktree_workspace(
            story, board=board, conn=conn
        )
        epic_branch = kb.epic_branch_for(epic_id)
        _git(repo, "branch", "-D", epic_branch)
        with pytest.raises(subprocess.CalledProcessError):
            _git(repo, "rev-parse", "--verify", epic_branch)

        kb.set_workspace_path(conn, story_id, str(workspace))
        kb.set_branch_name(conn, story_id, branch)
        reused = kb.get_task(conn, story_id)
        assert reused is not None
        again, _branch = kb._resolve_worktree_workspace(
            reused, board=board, conn=conn
        )

    assert Path(again) == Path(workspace)
    assert _git(repo, "rev-parse", epic_branch) == base_sha


def test_missing_epic_base_fails_materialization_loudly(
    epic_home, tmp_path, monkeypatch
):
    """A story worktree must never be left usable while its required epic base
    is absent — the failure has to be explicit, not silent."""
    repo = _repo(tmp_path)
    board = "epic-loud-failure"
    _v2_board(board, repo)
    with kb.connect(board=board) as conn:
        _epic_id, story_id = _epic_with_story(conn, board, repo, "Story one")
        story = kb.get_task(conn, story_id)
        assert story is not None
        monkeypatch.setattr(
            kb, "_git_branch_exists", lambda _repo, _branch: False
        )
        monkeypatch.setattr(
            subprocess, "run", _refuse_branch_creation(subprocess.run)
        )
        with pytest.raises(RuntimeError, match="epic"):
            kb._resolve_worktree_workspace(story, board=board, conn=conn)


def _refuse_branch_creation(real_run):
    def _run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "branch" in cmd:
            class _Result:
                returncode = 1
                stdout = ""
                stderr = "refused by test"
            return _Result()
        return real_run(cmd, *args, **kwargs)

    return _run
