"""Kanban task-skill recovery: kernel API + CLI surface (issue #22925).

Covers the post-dispatch recovery slice that upstream issue #22925 asks
for — changing/clearing task skills after creation, resetting the
consecutive-failure state, clearing stale claims (with a live-claim
guard), and rejecting path-like skill input — while preserving the
existing completed-task result backfill behavior.

Kernel tests exercise ``kanban_db.edit_task_recovery``; CLI tests drive
``hermes kanban edit`` through the same ``kanban_command`` / ``run_slash``
entry points the CLI and gateway use.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
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


def _create_task(conn, *, title="recovery task", skills=None, assignee="alice"):
    return kb.create_task(
        conn, title=title, assignee=assignee, skills=skills,
    )


def _force_failures(conn, task_id, count: int = 3, error: str = "boom"):
    conn.execute(
        "UPDATE tasks SET consecutive_failures = ?, last_failure_error = ? "
        "WHERE id = ?",
        (count, error, task_id),
    )
    conn.commit()


def _claim(conn, task_id):
    assert kb.claim_task(conn, task_id) is not None
    return conn.execute(
        "SELECT status, claim_lock, claim_expires FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def _build_kanban_parser():
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser


# ---------------------------------------------------------------------------
# Kernel: changing / clearing task skills after creation
# ---------------------------------------------------------------------------


def test_kernel_replace_skills_after_creation(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn, skills=["alpha"])
        assert kb.get_task(conn, tid).skills == ["alpha"]
        assert kb.edit_task_recovery(conn, tid, skills=["beta", "gamma"])
        task = kb.get_task(conn, tid)
        assert task.skills == ["beta", "gamma"]


def test_kernel_clear_skills(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn, skills=["alpha", "bogus"])
        assert kb.edit_task_recovery(conn, tid, clear_skills=True)
        task = kb.get_task(conn, tid)
        # Empty list (explicitly no extra skills), not None (defaults).
        assert task.skills == []


def test_kernel_skills_edit_records_audit_event_and_comment(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn, skills=["alpha"])
        kb.edit_task_recovery(conn, tid, skills=["beta"], author="ops")
        events = kb.list_events(conn, tid)
        assert any(e.kind == "edited" for e in events)
        edited = next(e for e in events if e.kind == "edited")
        assert "skills" in edited.payload["fields"]
        comments = kb.list_comments(conn, tid)
        assert any(c.author == "ops" and "skill" in c.body.lower() for c in comments)


# ---------------------------------------------------------------------------
# Kernel: resetting failure state
# ---------------------------------------------------------------------------


def test_kernel_reset_failures(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn)
        _force_failures(conn, tid, count=3, error="spawn boom")
        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 3
        assert task.last_failure_error == "spawn boom"
        assert kb.edit_task_recovery(conn, tid, reset_failures=True)
        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None


# ---------------------------------------------------------------------------
# Kernel: guarding an actively claimed / running task
# ---------------------------------------------------------------------------


def test_kernel_skills_edit_refuses_actively_claimed_running_task(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn, skills=["alpha"])
        row = _claim(conn, tid)
        assert row["status"] == "running"
        assert row["claim_lock"] is not None
        with pytest.raises(RuntimeError, match="running"):
            kb.edit_task_recovery(conn, tid, skills=["beta"])
        with pytest.raises(RuntimeError, match="running"):
            kb.edit_task_recovery(conn, tid, clear_skills=True)
        assert kb.get_task(conn, tid).skills == ["alpha"]


def test_kernel_reset_failures_refuses_actively_claimed_running_task(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn)
        _force_failures(conn, tid, count=3)
        _claim(conn, tid)
        with pytest.raises(RuntimeError, match="running"):
            kb.edit_task_recovery(conn, tid, reset_failures=True)
        assert kb.get_task(conn, tid).consecutive_failures == 3


def test_kernel_clear_claim_refuses_live_claim(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn)
        row = _claim(conn, tid)
        # Live claim: not expired.
        assert row["claim_expires"] > int(time.time())
        with pytest.raises(RuntimeError, match="reclaim"):
            kb.edit_task_recovery(conn, tid, clear_claim=True)


def test_kernel_clear_claim_clears_stale_claim(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn)
        _claim(conn, tid)
        # Backdate the claim so it is stale (TTL passed).
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 60, tid),
        )
        conn.commit()
        assert kb.edit_task_recovery(conn, tid, clear_claim=True)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.claim_lock is None
        assert task.claim_expires is None
        assert task.worker_pid is None


# ---------------------------------------------------------------------------
# Kernel: rejecting / normalizing path-like skill input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "./foo",
        "../foo",
        "/abs/path/foo",
        "~/foo",
        "sub/dir/skill.md",
        r"C:\skills\foo",
        "skills/foo.yaml",
    ],
)
def test_kernel_rejects_path_like_skill_input(kanban_home, bad):
    with kb.connect() as conn:
        tid = _create_task(conn, skills=["alpha"])
        with pytest.raises(ValueError, match="path"):
            kb.edit_task_recovery(conn, tid, skills=[bad])
        # Nothing changed.
        assert kb.get_task(conn, tid).skills == ["alpha"]


def test_kernel_rejects_toolset_and_comma_skill_input(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn, skills=["alpha"])
        with pytest.raises(ValueError, match="toolset"):
            kb.edit_task_recovery(conn, tid, skills=["web"])
        with pytest.raises(ValueError, match="comma"):
            kb.edit_task_recovery(conn, tid, skills=["blogwatcher,github"])
        assert kb.get_task(conn, tid).skills == ["alpha"]


# ---------------------------------------------------------------------------
# Kernel: edge cases and board resolution
# ---------------------------------------------------------------------------


def test_kernel_unknown_task_returns_false(kanban_home):
    with kb.connect() as conn:
        assert kb.edit_task_recovery(conn, "t_deadbeef", clear_skills=True) is False


def test_kernel_noop_raises_valueerror(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn)
        with pytest.raises(ValueError, match="operation"):
            kb.edit_task_recovery(conn, tid)


def test_kernel_archived_task_returns_false(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn)
        assert kb.archive_task(conn, tid)
        assert kb.edit_task_recovery(conn, tid, clear_skills=True) is False


def test_kernel_works_on_named_board(kanban_home):
    kb.create_board("beta")
    with kb.connect_closing(board="beta") as conn:
        tid = _create_task(conn, skills=["alpha"])
        assert kb.edit_task_recovery(conn, tid, skills=["gamma"])
        assert kb.get_task(conn, tid).skills == ["gamma"]
    # Default board untouched.
    with kb.connect() as conn:
        assert kb.list_tasks(conn, limit=100) == []


def test_completed_result_backfill_preserved(kanban_home):
    """edit_completed_task_result still backfills done tasks (regression)."""
    with kb.connect() as conn:
        tid = _create_task(conn)
        assert kb.complete_task(conn, tid, result="old result")
        assert kb.edit_completed_task_result(
            conn, tid, result="new result", summary="new summary",
        )
        assert kb.get_task(conn, tid).result == "new result"


# ---------------------------------------------------------------------------
# CLI: hermes kanban edit --skills / --clear-skills / --reset-failures
# ---------------------------------------------------------------------------


def _create_via_slash(conn, **kwargs):
    out = kc.run_slash("create 'recovery task' --assignee alice")
    m = re.search(r"(t_[a-f0-9]+)", out)
    assert m, out
    return m.group(1)


def test_cli_edit_skills_replace_after_creation(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn, skills=["alpha"])
    out = kc.run_slash(f"edit {tid} --skills beta gamma")
    assert "Edited" in out, out
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).skills == ["beta", "gamma"]


def test_cli_edit_clear_skills(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn, skills=["alpha", "bogus"])
    out = kc.run_slash(f"edit {tid} --clear-skills")
    assert "Edited" in out, out
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).skills == []


def test_cli_edit_empty_array_syntax_clears_skills(kanban_home):
    """The issue's `--skills []` form behaves like --clear-skills."""
    with kb.connect() as conn:
        tid = _create_task(conn, skills=["alpha"])
    out = kc.run_slash(f"edit {tid} --skills []")
    assert "Edited" in out, out
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).skills == []


def test_cli_edit_reset_failures(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn)
        _force_failures(conn, tid, count=3, error="spawn boom")
    out = kc.run_slash(f"edit {tid} --reset-failures")
    assert "Edited" in out, out
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None


def test_cli_edit_guards_actively_claimed_running_task(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn, skills=["alpha"])
        _claim(conn, tid)
    out = kc.run_slash(f"edit {tid} --skills beta")
    assert "running" in out.lower(), out
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).skills == ["alpha"]


def test_cli_edit_rejects_path_like_skill(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn, skills=["alpha"])
    parser = _build_kanban_parser()
    args = parser.parse_args(["kanban", "edit", tid, "--skills", "./foo"])
    rc = kc.kanban_command(args)
    assert rc == 2
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).skills == ["alpha"]


def test_cli_edit_requires_at_least_one_operation(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn)
    parser = _build_kanban_parser()
    args = parser.parse_args(["kanban", "edit", tid])
    rc = kc.kanban_command(args)
    assert rc == 2


def test_cli_edit_clear_stale_claim(kanban_home):
    with kb.connect() as conn:
        tid = _create_task(conn)
        _claim(conn, tid)
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 60, tid),
        )
        conn.commit()
    out = kc.run_slash(f"edit {tid} --clear-claim")
    assert "Edited" in out, out
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.claim_lock is None


def test_cli_edit_works_on_named_board(kanban_home):
    kb.create_board("beta")
    with kb.connect_closing(board="beta") as conn:
        tid = _create_task(conn, skills=["alpha"])
    parser = _build_kanban_parser()
    args = parser.parse_args(["kanban", "--board", "beta", "edit", tid, "--skills", "gamma"])
    rc = kc.kanban_command(args)
    assert rc == 0
    with kb.connect_closing(board="beta") as conn:
        assert kb.get_task(conn, tid).skills == ["gamma"]
    with kb.connect() as conn:
        assert kb.list_tasks(conn, limit=100) == []


def test_cli_edit_result_backfill_unchanged(kanban_home):
    """`--result` backfill on done tasks keeps working alongside recovery."""
    with kb.connect() as conn:
        tid = _create_task(conn, skills=["alpha"])
        assert kb.complete_task(conn, tid, result="old")
    parser = _build_kanban_parser()
    args = parser.parse_args(
        ["kanban", "edit", tid, "--result", "new result", "--skills", "beta"]
    )
    rc = kc.kanban_command(args)
    assert rc == 0
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.result == "new result"
        assert task.skills == ["beta"]


# ---------------------------------------------------------------------------
# E2E: the full recovery loop — blocked dispatch failure → edit → retry
# (issue #22925: create with wrong skills → dispatch fails → task blocked
# with a failure streak → operator repairs skills + resets failures through
# the supported surface → unblock → dispatchable again, audit trail intact)
# ---------------------------------------------------------------------------


def _simulate_dispatch_failure(conn, task_id, *, error="Unknown skill(s)"):
    """Leave the task in the state the dispatcher produces after a spawn
    failure on wrong force-loaded skills: blocked with a failure streak."""
    assert kb.block_task(conn, task_id, reason=error)
    _force_failures(conn, task_id, count=3, error=error)
    return kb.get_task(conn, task_id)


def test_e2e_blocked_dispatch_failure_replace_skills_then_unblock(kanban_home):
    """Wrong skills → blocked → `edit --skills X --reset-failures` →
    `unblock` → ready with corrected skills, zeroed failures, audit trail."""
    with kb.connect() as conn:
        tid = _create_task(conn, skills=["bogus-skill"], assignee="alice")
        task = _simulate_dispatch_failure(conn, tid)
        assert task.status == "blocked"
        assert task.skills == ["bogus-skill"]
        assert task.consecutive_failures == 3

    # Repair through the supported CLI / /kanban surface (run_slash is the
    # exact entry point the gateway and interactive CLI use).
    out = kc.run_slash(f"edit {tid} --skills real-skill --reset-failures")
    assert "Edited" in out, out
    out = kc.run_slash(f"unblock {tid}")
    assert "Unblocked" in out, out

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "ready"          # dispatchable again
        assert task.skills == ["real-skill"]
        assert task.consecutive_failures == 0  # breaker reset
        assert task.last_failure_error is None
        # Audit trail: edited event + operator comment (no direct SQL).
        events = kb.list_events(conn, tid)
        assert any(e.kind == "edited" for e in events)
        edited = next(e for e in events if e.kind == "edited")
        assert "skills" in edited.payload["fields"]
        assert "failures" in edited.payload["fields"]
        comments = kb.list_comments(conn, tid)
        assert any("RECOVERY EDIT" in c.body for c in comments)


def test_e2e_blocked_dispatch_failure_clear_skills_then_unblock(kanban_home):
    """The --clear-skills variant: bogus skill removed entirely, breaker
    reset, unblocked to ready."""
    with kb.connect() as conn:
        tid = _create_task(conn, skills=["bogus-skill"], assignee="alice")
        task = _simulate_dispatch_failure(conn, tid)
        assert task.status == "blocked"

    out = kc.run_slash(f"edit {tid} --clear-skills --reset-failures")
    assert "Edited" in out, out
    assert "Unblocked" in kc.run_slash(f"unblock {tid}"), out

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.skills == []               # explicit empty list ≠ NULL
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None
        assert any(e.kind == "edited" for e in kb.list_events(conn, tid))


def test_e2e_full_recovery_loop_on_named_board(kanban_home):
    """The complete loop (blocked → edit → unblock) resolves correctly on a
    named board via `--board <slug>`, and leaves the default board alone."""
    kb.create_board("gamma")
    with kb.connect_closing(board="gamma") as conn:
        tid = _create_task(conn, skills=["bogus-skill"], assignee="alice")
        _simulate_dispatch_failure(conn, tid)

    parser = _build_kanban_parser()
    rc = kc.kanban_command(
        parser.parse_args(
            ["kanban", "--board", "gamma", "edit", tid,
             "--skills", "real-skill", "--reset-failures"]
        )
    )
    assert rc == 0
    rc = kc.kanban_command(
        parser.parse_args(["kanban", "--board", "gamma", "unblock", tid])
    )
    assert rc == 0

    with kb.connect_closing(board="gamma") as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.skills == ["real-skill"]
        assert task.consecutive_failures == 0
        assert any(e.kind == "edited" for e in kb.list_events(conn, tid))
    # Default board untouched.
    with kb.connect() as conn:
        assert kb.list_tasks(conn, limit=100) == []
