"""Capability-based workspace policy and per-task worktree isolation.

Decompose children used to inherit the root's literal ``workspace_path``,
so every sibling of a worktree-kind root pointed at the SAME checkout —
and ``_resolve_worktree_workspace``'s existing-checkout shortcut reused it
on whatever branch was there, letting sibling workers run concurrently in
one directory on one branch (cross-task provenance corruption, no lock).

Only explicitly repository-writing children receive worktrees. Non-writing
children fail closed to scratch even when their root owns a worktree. Existing
stale paths still fall back to a fresh per-task checkout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _add_worktree(repo: Path, target: Path, branch: str) -> Path:
    _git(repo, "worktree", "add", str(target), "-b", branch, "HEAD")
    return target


def test_decompose_worktree_root_applies_child_capability_policy(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="build the feature", triage=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', "
            "workspace_path='/repo/.worktrees/root' WHERE id = ?",
            (root,),
        )
        conn.commit()

        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {
                    "title": "research it",
                    "assignee": "alice",
                    "parents": [],
                    "workspace_policy": "scratch",
                },
                {
                    "title": "implement it",
                    "assignee": "bob",
                    "parents": [0],
                    "workspace_policy": "repo_write",
                },
                {
                    "title": "review it",
                    "assignee": "reviewer",
                    "parents": [1],
                },
            ],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 3
        rows = [kb.get_task(conn, child_id) for child_id in child_ids]

    research, implementation, review = rows
    assert research.workspace_kind == "scratch"
    assert research.workspace_path is None
    assert implementation.workspace_kind == "worktree"
    assert implementation.workspace_path is None
    assert review.workspace_kind == "scratch"
    assert review.workspace_path is None


@pytest.mark.parametrize(
    "title",
    [
        "research upstream behavior",
        "run QA evidence review",
        "review the proposed changes",
        "draft launch copy",
        "reconcile operations checklist",
    ],
)
def test_missing_policy_never_inherits_worktree_root(kanban_home, title):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="code root", triage=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', "
            "workspace_path='/repo/.worktrees/root' WHERE id = ?",
            (root,),
        )
        conn.commit()
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": title, "assignee": "specialist", "parents": []}],
            author="decomposer",
        )
        child = kb.get_task(conn, child_ids[0])

    assert child.workspace_kind == "scratch"
    assert child.workspace_path is None


def test_explicit_repo_write_siblings_get_distinct_unresolved_worktrees(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="parallel code", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {
                    "title": "implement API",
                    "assignee": "alice",
                    "parents": [],
                    "workspace_policy": "repo_write",
                },
                {
                    "title": "implement UI",
                    "assignee": "bob",
                    "parents": [],
                    "workspace_policy": "repo_write",
                },
            ],
            author="decomposer",
        )
        rows = [kb.get_task(conn, child_id) for child_id in child_ids]

    assert all(row.workspace_kind == "worktree" for row in rows)
    assert all(row.workspace_path is None for row in rows)
    assert rows[0].id != rows[1].id




def test_resolve_worktree_falls_back_when_path_occupied(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    occupied = _add_worktree(repo, repo / ".worktrees" / "sibling", "wt/sibling")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="second sibling",
            workspace_kind="worktree",
            workspace_path=str(occupied),  # inherited shared/stale path
        )
        task = kb.get_task(conn, tid)

    workspace, branch = kb._resolve_worktree_workspace(task)
    assert workspace == (repo / ".worktrees" / tid).resolve()
    assert branch == f"wt/{tid}"
    # The sibling's checkout is untouched, still on its own branch.
    assert (occupied / "README.md").exists()
    head = subprocess.run(
        ["git", "-C", str(occupied), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "wt/sibling"


