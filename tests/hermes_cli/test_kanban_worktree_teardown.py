"""Tests for worktree workspace retention and teardown at task completion/archive.

Review-delivered source work remains inspectable after completion; archive or
explicit GC removes it only when it is provably disposable. Scratch cleanup
and all dirty/unpushed/user-tree safety guards remain intact.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb


def _git(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project repo with a remote whose history is fully pushed."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("-C", str(project), "config", "user.email", "t@example.com")
    _git("-C", str(project), "config", "user.name", "t")
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git("-C", str(project), "add", "README.md")
    _git("-C", str(project), "commit", "-m", "init")
    _git("-C", str(project), "push", "origin", "HEAD")
    return project


def _make_worktree(repo: Path, task_id: str, branch: str | None = None) -> Path:
    target = repo / ".worktrees" / task_id
    kb._ensure_git_worktree(repo, target, branch or f"wt/{task_id}")
    return target


def _branch_exists(repo: Path, branch: str) -> bool:
    out = _git("-C", str(repo), "branch", "--list", branch)
    return bool(out.strip())


# ---------------------------------------------------------------------------
# _cleanup_worktree_workspace unit behavior
# ---------------------------------------------------------------------------


def test_clean_pushed_worktree_removed(repo: Path) -> None:
    wt = _make_worktree(repo, "t_aaaa1111")
    kb._cleanup_worktree_workspace("t_aaaa1111", str(wt))
    assert not wt.exists()
    # auto-generated task branch goes with it
    assert not _branch_exists(repo, "wt/t_aaaa1111")
    # main checkout untouched
    assert (repo / "README.md").exists()


def test_dirty_worktree_preserved(repo: Path) -> None:
    wt = _make_worktree(repo, "t_bbbb2222")
    (wt / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    kb._cleanup_worktree_workspace("t_bbbb2222", str(wt))
    assert wt.is_dir()
    assert (wt / "wip.txt").exists()


def test_unpushed_commits_preserved(repo: Path) -> None:
    wt = _make_worktree(repo, "t_cccc3333")
    (wt / "work.txt").write_text("committed but not pushed\n", encoding="utf-8")
    _git("-C", str(wt), "add", "work.txt")
    _git("-C", str(wt), "commit", "-m", "local work")
    kb._cleanup_worktree_workspace("t_cccc3333", str(wt))
    assert wt.is_dir()


def test_custom_branch_survives_worktree_removal(repo: Path) -> None:
    wt = _make_worktree(repo, "t_dddd4444", branch="feature/custom")
    kb._cleanup_worktree_workspace("t_dddd4444", str(wt), "feature/custom")
    assert not wt.exists()
    # only auto-generated wt/* branches are deleted
    assert _branch_exists(repo, "feature/custom")


def test_custom_existing_worktree_is_not_task_owned(repo: Path) -> None:
    target = repo / "user-worktree"
    kb._ensure_git_worktree(repo, target, "feature/user-worktree")
    kb._cleanup_worktree_workspace("t_ownercase", str(target), "feature/user-worktree")
    assert target.is_dir()
    assert _branch_exists(repo, "feature/user-worktree")


def test_main_checkout_never_removed(repo: Path) -> None:
    kb._cleanup_worktree_workspace("t_eeee5555", str(repo))
    assert repo.is_dir()
    assert (repo / "README.md").exists()


def test_non_git_dir_preserved(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-worktree"
    plain.mkdir()
    kb._cleanup_worktree_workspace("t_ffff6666", str(plain))
    assert plain.is_dir()


def test_tree_dirtied_between_check_and_removal_preserved(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU: a tree that becomes dirty after the pre-check is NOT removed.

    Simulates the race by making the pre-check see a clean tree while the
    tree is actually dirty when ``git worktree remove`` runs. Without
    ``--force``, git's own dirty guard re-verifies at removal time and the
    removal fails safe.
    """
    import cli

    wt = _make_worktree(repo, "t_gggg7777")
    (wt / "late-wip.txt").write_text("dirtied after the check\n", encoding="utf-8")
    # Pre-check lies (as if the file appeared just after it ran) — real git
    # must still refuse the removal.
    monkeypatch.setattr(cli, "_worktree_is_dirty", lambda _p: False)
    kb._cleanup_worktree_workspace("t_gggg7777", str(wt))
    assert wt.is_dir()
    assert (wt / "late-wip.txt").exists()


# ---------------------------------------------------------------------------
# Lifecycle integration: complete / archive / deferred parents
# ---------------------------------------------------------------------------


def _worktree_task(conn, repo: Path, title: str = "wt-task") -> tuple[str, Path]:
    tid = kb.create_task(conn, title=title, assignee="worker")
    wt = _make_worktree(repo, tid)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=?, "
            "branch_name=? WHERE id=?",
            (str(wt), f"wt/{tid}", tid),
        )
    return tid, wt


def test_complete_task_reaps_clean_worktree(kanban_home: Path, repo: Path) -> None:
    with kb.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")
    assert not wt.exists()
    assert not _branch_exists(repo, f"wt/{tid}")


def test_review_delivered_completion_retains_clean_worktree_until_archive(
    kanban_home: Path, repo: Path
) -> None:
    with kb.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        implementation = kb.claim_task(conn, tid, claimer="worker")
        assert implementation is not None
        assert kb.request_review(
            conn, tid, summary="PR delivered", reviewer="reviewer",
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, tid, claimer="reviewer")
        assert review is not None
        assert kb.complete_task(
            conn, tid, summary="Approved", expected_run_id=review.current_run_id
        )
        assert wt.is_dir(), "review delivery must leave source work inspectable"
        assert _branch_exists(repo, f"wt/{tid}"), (
            "review delivery must retain the local task branch until archive"
        )
        assert kb.archive_task(conn, tid)
    assert not wt.exists(), "archive is an explicit cleanup action"


def test_review_delivered_worktree_gc_is_explicit_cleanup(
    kanban_home: Path, repo: Path
) -> None:
    with kb.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        assert kb.complete_task(conn, tid, summary="Approved")
        assert wt.is_dir()
    # GC's backstop covers retained done tasks from the review-only path.
    assert kanban_cli._cmd_gc(
        argparse.Namespace(event_retention_days=30, log_retention_days=30)
    ) == 0
    assert not wt.exists()


def test_corrupt_review_claim_payload_keeps_completion_cleanup_safe(
    kanban_home: Path, repo: Path
) -> None:
    """A non-object claimed payload is non-review provenance, not an error."""
    with kb.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        implementation = kb.claim_task(conn, tid, claimer="worker")
        assert implementation is not None
        assert kb.request_review(
            conn, tid, summary="PR delivered", reviewer="reviewer",
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, tid, claimer="reviewer")
        assert review is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET payload='[]' "
                "WHERE task_id=? AND kind='claimed' AND run_id=?",
                (tid, review.current_run_id),
            )
        assert kb.complete_task(
            conn, tid, summary="Approved", expected_run_id=review.current_run_id
        )
        assert kb.get_task(conn, tid).status == "done"
    assert not wt.exists(), "corrupt provenance must retain ordinary cleanup"
    assert not _branch_exists(repo, f"wt/{tid}")


def test_review_delivered_gc_preserves_dirty_worktree(
    kanban_home: Path, repo: Path
) -> None:
    with kb.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (wt / "wip.txt").write_text("review evidence\n", encoding="utf-8")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        assert kb.complete_task(conn, tid, summary="Approved")
        assert wt.is_dir()
    assert kanban_cli._cmd_gc(
        argparse.Namespace(event_retention_days=30, log_retention_days=30)
    ) == 0
    assert wt.is_dir(), "GC must preserve dirty review-delivered work"
    assert (wt / "wip.txt").read_text(encoding="utf-8") == "review evidence\n"


def test_reopened_review_then_ordinary_completion_does_not_retain_worktree(
    kanban_home: Path, repo: Path
) -> None:
    with kb.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        implementation = kb.claim_task(conn, tid, claimer="worker")
        assert implementation is not None
        assert kb.request_review(
            conn, tid, summary="needs changes", reviewer="reviewer",
            expected_run_id=implementation.current_run_id,
        )
        assert kb.reopen_review_task(conn, tid)
        rerun = kb.claim_task(conn, tid, claimer="worker")
        assert rerun is not None
        assert kb.complete_task(
            conn, tid, summary="fixed", expected_run_id=rerun.current_run_id
        )
    assert not wt.exists(), "ordinary completion after reopen must clean up"


def test_reopened_review_deferred_parent_uses_latest_completion(
    kanban_home: Path, repo: Path
) -> None:
    with kb.connect_closing() as conn:
        parent, parent_wt = _worktree_task(conn, repo, title="review parent")
        child = kb.create_task(conn, title="child", assignee="worker")
        kb.link_tasks(conn, parent, child)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        implementation = kb.claim_task(conn, parent, claimer="worker")
        assert implementation is not None
        assert kb.request_review(
            conn, parent, summary="needs changes", reviewer="reviewer",
            expected_run_id=implementation.current_run_id,
        )
        assert kb.reopen_review_task(conn, parent)
        rerun = kb.claim_task(conn, parent, claimer="worker")
        assert rerun is not None
        assert kb.complete_task(
            conn, parent, summary="fixed", expected_run_id=rerun.current_run_id
        )
        assert parent_wt.is_dir(), "active child should defer parent cleanup"

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        assert kb.claim_task(conn, child, claimer="worker") is not None
        assert kb.complete_task(conn, child, summary="child done")
    assert not parent_wt.exists(), "deferred cleanup must use latest provenance"


def test_scratch_completion_and_abandoned_archive_still_clean_up(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        completed = kb.create_task(conn, title="scratch", assignee="worker")
        completed_path = kanban_home / "kanban" / "workspaces" / completed
        completed_path.mkdir(parents=True)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_path=? WHERE id=?",
                (str(completed_path), completed),
            )
        assert kb.complete_task(conn, completed, summary="done")
        assert not completed_path.exists()

        abandoned = kb.create_task(conn, title="abandoned", assignee="worker")
        abandoned_path = kanban_home / "kanban" / "workspaces" / abandoned
        abandoned_path.mkdir(parents=True)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_path=? WHERE id=?",
                (str(abandoned_path), abandoned),
            )
        assert kb.archive_task(conn, abandoned)
        assert not abandoned_path.exists()


def test_complete_task_preserves_dirty_worktree(kanban_home: Path, repo: Path) -> None:
    with kb.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (wt / "wip.txt").write_text("unsaved\n", encoding="utf-8")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")
    assert wt.is_dir()
    assert (wt / "wip.txt").exists()


def test_archive_task_reaps_clean_worktree(kanban_home: Path, repo: Path) -> None:
    with kb.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        assert kb.archive_task(conn, tid)
    assert not wt.exists()


def test_parent_worktree_deferred_until_children_done(
    kanban_home: Path, repo: Path
) -> None:
    with kb.connect_closing() as conn:
        parent, parent_wt = _worktree_task(conn, repo, title="parent")
        child = kb.create_task(conn, title="child", assignee="worker")
        kb.link_tasks(conn, parent, child)

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        assert kb.claim_task(conn, parent, claimer="worker") is not None
        assert kb.complete_task(conn, parent, summary="parent done")
        # child still active -> parent worktree must survive for handoff
        assert parent_wt.is_dir()

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        assert kb.claim_task(conn, child, claimer="worker") is not None
        assert kb.complete_task(conn, child, summary="child done")
    # last child terminal -> deferred parent worktree reaped
    assert not parent_wt.exists()


def test_archived_parent_cleanup_wins_after_active_child_finishes(
    kanban_home: Path, repo: Path
) -> None:
    with kb.connect_closing() as conn:
        parent, parent_wt = _worktree_task(conn, repo, title="archived parent")
        child = kb.create_task(conn, title="child", assignee="worker")
        kb.link_tasks(conn, parent, child)

        assert kb.archive_task(conn, parent)
        assert parent_wt.is_dir(), "active child should defer physical removal"

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        assert kb.claim_task(conn, child, claimer="worker") is not None
        assert kb.complete_task(conn, child, summary="child done")
    assert not parent_wt.exists(), "archive must remain authoritative after deferral"
