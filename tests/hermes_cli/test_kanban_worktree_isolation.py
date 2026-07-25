"""Per-task worktree isolation for decompose siblings.

Decompose children used to inherit the root's literal ``workspace_path``,
so every sibling of a worktree-kind root pointed at the SAME checkout —
and ``_resolve_worktree_workspace``'s existing-checkout shortcut reused it
on whatever branch was there, letting sibling workers run concurrently in
one directory on one branch (cross-task provenance corruption, no lock).

Two-part fix under test:
- ``decompose_triage_task`` persists a distinct task-specific
  ``<repo>/.worktrees/<child-id>`` target for every inherited worktree child.
- ``_resolve_worktree_workspace`` falls back to a fresh per-task worktree
  when the requested path is occupied by another task's branch (heals
  pre-existing rows that still carry a shared path).
"""

from __future__ import annotations

import subprocess
import threading
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


def test_decompose_worktree_children_get_own_workspace(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    root_path = repo / ".worktrees" / "root"
    kb.write_board_metadata("default", default_workdir=str(repo))
    with kb.connect() as conn:
        root = kb.create_task(conn, title="build the feature", triage=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', "
            "workspace_path=? WHERE id = ?",
            (str(root_path), root),
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
                "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
                (cid,),
            ).fetchone()
            assert row["workspace_kind"] == "worktree"
            # Each child has its own concrete target; the root's literal path
            # must never be shared even before dispatch resolves it.
            assert row["workspace_path"] == str(repo / ".worktrees" / cid)


def test_decompose_external_linked_root_never_shares_sibling_checkout(
    kanban_home, tmp_path
):
    repo = _make_repo(tmp_path)
    external_root = _add_worktree(repo, tmp_path / "external-root", "wt/root")

    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="external linked root",
            workspace_kind="worktree",
            workspace_path=str(external_root),
            triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "one"}, {"title": "two"}],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 2
        children = [kb.get_task(conn, child_id) for child_id in child_ids]

    stored = []
    resolved = []
    for child in children:
        assert child is not None
        assert child.workspace_path is not None
        stored.append(Path(child.workspace_path))
        resolved.append(kb.resolve_workspace(child))
    assert stored[0] != stored[1]
    assert external_root.resolve() not in {path.resolve() for path in stored}
    assert resolved[0] != resolved[1]
    assert external_root.resolve() not in {path.resolve() for path in resolved}


def test_decompose_linked_root_from_bare_repo_uses_bare_anchor(
    kanban_home, tmp_path
):
    source = _make_repo(tmp_path)
    bare = tmp_path / "repo.git"
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    external_root = _add_worktree(bare, tmp_path / "bare-root", "wt/bare-root")

    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="bare common-dir root",
            workspace_kind="worktree",
            workspace_path=str(external_root),
            triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "one"}, {"title": "two"}],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 2
        children = [kb.get_task(conn, child_id) for child_id in child_ids]

    resolved = []
    for child_id, child in zip(child_ids, children):
        assert child is not None
        assert child.workspace_path == str(bare / ".worktrees" / child_id)
        resolved.append(kb.resolve_workspace(child))
    assert resolved[0] != resolved[1]
    assert external_root.resolve() not in {path.resolve() for path in resolved}


def test_decompose_git_validation_does_not_hold_board_write_lock(
    kanban_home, tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path)
    kb.write_board_metadata("default", default_workdir=str(repo))
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="slow validation",
            workspace_kind="worktree",
            triage=True,
        )

    validator_entered = threading.Event()
    release_validator = threading.Event()
    writer_done = threading.Event()
    errors: list[BaseException] = []
    original = kb._repo_root_for_worktree_target

    def slow_validator(path):
        if not validator_entered.is_set():
            validator_entered.set()
            if not release_validator.wait(timeout=5):
                raise TimeoutError("test did not release Git validator")
        return original(path)

    monkeypatch.setattr(kb, "_repo_root_for_worktree_target", slow_validator)

    def decompose():
        try:
            with kb.connect() as conn:
                kb.decompose_triage_task(
                    conn,
                    root,
                    root_assignee="orchestrator",
                    children=[{"title": "child"}],
                    author="decomposer",
                )
        except BaseException as exc:  # surfaced on the main test thread below
            errors.append(exc)

    def write_unrelated_root_field():
        try:
            with kb.connect() as conn:
                with kb.write_txn(conn):
                    conn.execute(
                        "UPDATE tasks SET title = ? WHERE id = ?",
                        ("writer was not blocked", root),
                    )
        except BaseException as exc:
            errors.append(exc)
        finally:
            writer_done.set()

    decomposer = threading.Thread(target=decompose, daemon=True)
    writer = threading.Thread(target=write_unrelated_root_field, daemon=True)
    decomposer.start()
    assert validator_entered.wait(timeout=5)
    writer.start()
    try:
        assert writer_done.wait(timeout=2), "Git validation held BEGIN IMMEDIATE"
    finally:
        release_validator.set()
    writer.join(timeout=5)
    decomposer.join(timeout=10)
    assert not writer.is_alive()
    assert not decomposer.is_alive()
    assert errors == []


def test_decompose_dir_children_still_inherit_path(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="ops sweep", triage=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind='dir', "
            "workspace_path='/srv/ops' WHERE id = ?",
            (root,),
        )
        conn.commit()

        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "child", "assignee": "alice", "parents": []}],
            author="decomposer",
        )
        assert child_ids is not None
        row = conn.execute(
            "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
            (child_ids[0],),
        ).fetchone()
        assert row["workspace_kind"] == "dir"
        assert row["workspace_path"] == "/srv/ops"


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


def test_resolve_worktree_same_branch_still_reuses(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="returning task",
            workspace_kind="worktree",
        )
        own = _add_worktree(repo, repo / ".worktrees" / tid, f"wt/{tid}")
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(own), tid),
        )
        conn.commit()
        task = kb.get_task(conn, tid)

    workspace, branch = kb._resolve_worktree_workspace(task)
    assert workspace == own.resolve()
    assert branch == f"wt/{tid}"


def test_resolve_worktree_own_path_on_foreign_branch_keeps_legacy_reuse(
    kanban_home, tmp_path
):
    repo = _make_repo(tmp_path)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="foreign-branch checkout",
            workspace_kind="worktree",
        )
        own = _add_worktree(repo, repo / ".worktrees" / tid, "wt/foreign")
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(own), tid),
        )
        conn.commit()
        task = kb.get_task(conn, tid)

    # The fallback target would be the occupied path itself, so the
    # legacy reuse applies rather than failing dispatch.
    workspace, branch = kb._resolve_worktree_workspace(task)
    assert workspace == own.resolve()
    assert branch == "wt/foreign"
