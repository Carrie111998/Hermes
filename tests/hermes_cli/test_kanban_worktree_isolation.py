"""Per-task worktree isolation for decompose siblings.

Decompose children used to inherit the root's literal ``workspace_path``,
so every sibling of a worktree-kind root pointed at the SAME checkout —
and ``_resolve_worktree_workspace``'s existing-checkout shortcut reused it
on whatever branch was there, letting sibling workers run concurrently in
one directory on one branch (cross-task provenance corruption, no lock).

Two-part fix under test:
- ``decompose_triage_task`` leaves worktree children's ``workspace_path``
  unset so each child materializes its own ``<repo>/.worktrees/<child-id>``.
- ``_resolve_worktree_workspace`` falls back to a fresh per-task worktree
  when the requested path is occupied by another task's branch (heals
  pre-existing rows that still carry a shared path).
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
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


def test_decompose_worktree_children_get_own_workspace(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="build the feature", triage=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', "
            "workspace_path='/repo/.worktrees/root', expected_base_sha=? WHERE id = ?",
            ("b" * 40, root),
        )
        conn.commit()

        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "spec it", "assignee": "alice", "parents": []},
                {"title": "implement it", "assignee": "bob", "parents": [0]},
            ],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 2

        for cid in child_ids:
            row = conn.execute(
                "SELECT workspace_kind, workspace_path, expected_base_sha "
                "FROM tasks WHERE id = ?",
                (cid,),
            ).fetchone()
            assert row["workspace_kind"] == "worktree"
            # Each child resolves its own <repo>/.worktrees/<child-id> at
            # dispatch; the root's literal path must never be shared.
            assert row["workspace_path"] is None
            assert row["expected_base_sha"] == "b" * 40




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


def test_pinned_worktree_rejects_occupied_canonical_path_with_wrong_branch(
    kanban_home, tmp_path
):
    repo = _make_repo(tmp_path)
    expected_base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="pinned",
            workspace_kind="worktree",
            workspace_path=str(repo),
            branch_name="wt/pinned",
            expected_base_sha=expected_base,
        )
        persisted = kb.get_task(conn, tid)
    assert persisted is not None

    target = repo / ".worktrees" / tid
    _add_worktree(repo, target, "occupied-branch")
    pinned = replace(persisted, workspace_path=str(target))
    assert pinned.expected_base_sha == expected_base
    with pytest.raises(RuntimeError, match="cannot safely reuse"):
        kb.resolve_workspace(pinned)

    # Preserve the legacy fallback only for tasks that did not request an
    # immutable base contract.
    legacy = replace(pinned, expected_base_sha=None)
    assert kb.resolve_workspace(legacy).resolve() == target.resolve()


def test_missing_expected_base_fails_without_fallback(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    target = repo / ".worktrees" / "missing-base"
    missing = "f" * 40

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="missing base",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name="wt/missing-base",
            expected_base_sha=missing,
        )
        task = kb.get_task(conn, tid)

    assert task is not None
    with pytest.raises(RuntimeError, match="git worktree add failed"):
        kb._resolve_worktree_workspace(task)
    assert not target.exists()
    branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "wt/missing-base"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == ""


def test_existing_dirty_worktree_is_reused_without_reset(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    expected = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    target = _add_worktree(repo, repo / ".worktrees" / "resume", "wt/resume")
    dirty = target / "resume.txt"
    dirty.write_text("preserve me\n")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="resume pinned task",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name="wt/resume",
            expected_base_sha=expected,
        )
        task = kb.get_task(conn, tid)

    assert task is not None
    workspace, branch = kb._resolve_worktree_workspace(task)
    assert workspace == target.resolve()
    assert branch == "wt/resume"
    assert dirty.read_text() == "preserve me\n"




