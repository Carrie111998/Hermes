"""Smoke-test the kanban integrity steward script end-to-end.

Hermetic test: spins up a real git repo with a branch ahead of main,
marks a coding task ``done`` via the kernel, runs the steward, and
verifies the task is re-opened. A second case (branch with no
ahead-commits) verifies the steward does NOT re-open a legitimate
``done``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hermes_cli import kanban_db as kb
from cron.scripts import kanban_integrity_steward as steward


CODING_TASK_BODY = (
    "Steward test.\n\nrepo: octocat/hello-world\n\nAcceptance: pushed.\n"
)


def _init_repo(path: Path, branch: str = "feat/test") -> str:
    """Init a real git repo at ``path`` with one commit on ``main`` and
    one commit on ``branch`` ahead of main. Also creates a local bare
    repo as ``origin`` and pushes both branches so ``git ls-remote``
    has something to resolve against in tests where there's no real
    network.

    Returns main SHA.
    """
    path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "t@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "t@example.com"
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@example.com",
         "-c", "user.name=Test", "commit", "-q", "-m", "init"],
        check=True, env=env,
    )
    main_sha = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, env=env,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(path), "checkout", "-q", "-b", branch],
        check=True, env=env,
    )
    (path / "feature.txt").write_text("feat\n")
    subprocess.run(["git", "-C", str(path), "add", "feature.txt"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@example.com",
         "-c", "user.name=Test", "commit", "-q", "-m", "feat"],
        check=True, env=env,
    )
    # Local bare "origin" so the steward's ``git ls-remote origin <branch>``
    # resolves to a real SHA in hermetic tests.
    origin = path.parent / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(origin)],
        check=True, env=env,
    )
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", str(origin)],
        check=True, env=env,
    )
    subprocess.run(
        ["git", "-C", str(path), "push", "-q", "origin", "main", branch],
        check=True, env=env,
    )
    return main_sha


def _make_task(conn, body: str, branch: str, workspace_path: str) -> str:
    return kb.create_task(
        conn, title="steward test", body=body, assignee="alice",
        workspace_kind="worktree", workspace_path=workspace_path,
        branch_name=branch,
    )


def test_steward_reopens_done_task_with_no_ahead_commits(tmp_path, monkeypatch):
    """A done coding task whose branch has no commits ahead of main is
    re-opened by the steward.

    Simulates the drift scenario: the task was force-marked done
    outside the kernel gate (legacy CLI, manual SQL, pre-gate history).
    The steward must detect the regression and re-open the task.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    repo = tmp_path / "repo"
    main_sha = _init_repo(repo, branch="feat/bare")
    # Reset branch back to main so it's not ahead of main.
    subprocess.run(
        ["git", "-C", str(repo), "reset", "--hard", main_sha],
        check=True,
    )

    # The worktree workspace must live inside the repo so the steward's
    # repo resolver can find a git toplevel.
    worktree_path = repo / ".worktrees" / "t_abc"
    worktree_path.mkdir(parents=True, exist_ok=True)

    conn = kb.connect()
    try:
        task_id = _make_task(conn, CODING_TASK_BODY, "feat/bare", str(worktree_path))
        # Force the task to ``done`` via direct SQL — bypass the gate to
        # simulate a pre-gate historical task or a force-bypass
        # invocation. The steward's job is to catch this.
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
            (int(time.time()), task_id),
        )
        conn.commit()
    finally:
        conn.close()

    # Run the steward.
    reopened = steward.audit_done_coding_tasks()
    assert any(r["task_id"] == task_id for r in reopened), (
        f"steward did not re-open the bare-branch task; got {reopened!r}"
    )

    # Verify the task is back to ``ready``.
    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
        assert row["status"] == "ready", row["status"]
        # And the audit event landed.
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? "
            "AND kind='merge_required_reopened' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        payload = json.loads(ev["payload"])
        assert payload["repo"] == "octocat/hello-world"
        assert payload["branch"] == "feat/bare"
        assert "no commits ahead" in payload["reason"]
    finally:
        conn.close()


def test_steward_leaves_legitimate_done_task_alone(tmp_path, monkeypatch):
    """A done coding task whose branch IS ahead of main stays done.

    Same force-bypass simulation: flip the task to done via direct
    SQL — but here the branch IS ahead of main, so the steward should
    leave it alone.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    repo = tmp_path / "repo"
    _init_repo(repo, branch="feat/good")  # branch is ahead of main
    worktree_path = repo / ".worktrees" / "t_xyz"
    worktree_path.mkdir(parents=True, exist_ok=True)

    conn = kb.connect()
    try:
        task_id = _make_task(conn, CODING_TASK_BODY, "feat/good", str(worktree_path))
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
            (int(time.time()), task_id),
        )
        conn.commit()
    finally:
        conn.close()

    reopened = steward.audit_done_coding_tasks()
    assert not any(r["task_id"] == task_id for r in reopened), (
        f"steward incorrectly re-opened the legitimate task: {reopened!r}"
    )

    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
        assert row["status"] == "done"
    finally:
        conn.close()


def test_steward_ignores_non_coding_done_tasks(tmp_path, monkeypatch):
    """A done task without a ``repo:`` directive is left alone."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="research", body="No repo here. Audit task.",
            assignee="alice",
        )
        ok = kb.complete_task(conn, task_id, summary="done")
        assert ok is True
    finally:
        conn.close()

    reopened = steward.audit_done_coding_tasks()
    assert reopened == []
