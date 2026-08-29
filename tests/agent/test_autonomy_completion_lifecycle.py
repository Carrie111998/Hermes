"""Autonomy completion lifecycle (PR #90820 Round 3 / Round-3 clean rebuild).

Behavior contract:

1. ``agent.autonomy.reserve_active(objective_id)`` admits an objective
   into the in-process slot. A second ``reserve_active`` for a
   DIFFERENT objective is rejected while the first is still active.
2. ``clear_active(objective_id)`` releases the slot. It is a no-op
   when the slot is owned by a different objective (defensive).
3. **Real production boundary** — ``complete_task`` in
   ``hermes_cli/kanban_db`` calls ``clear_active(task_id)`` AFTER the
   task row is durably done and the lifecycle hook is about to fire.
   This means once a kanban task completes, the autonomy slot is
   released without a stuck reservation, and a second objective may
   be admitted.
4. The slot is NOT released prematurely — the call sits inside the
   completion path after the write transaction has committed and
   after ``recompute_ready`` + ``_cleanup_workspace``.

Tests cover all four rules and exercise the REAL
``hermes_cli.kanban_db.complete_task`` path against a temp
``HERMES_HOME``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


# --------------------------------------------------------------------
# Lifecycle primitives
# --------------------------------------------------------------------

def test_reserve_blocks_second_objective():
    """A second reserve_active for a different objective is rejected."""
    from agent import autonomy

    autonomy.clear_active()  # ensure clean slate
    try:
        r1 = autonomy.reserve_active("obj_a")
        assert r1.objective_id == "obj_a"
        assert autonomy.is_active("obj_a")
        # Different objective → RuntimeError.
        with pytest.raises(RuntimeError, match="already active"):
            autonomy.reserve_active("obj_b")
        # Same objective → idempotent.
        r1_again = autonomy.reserve_active("obj_a")
        assert r1_again.objective_id == "obj_a"
    finally:
        autonomy.clear_active()


def test_clear_releases_and_admits_next():
    """clear_active releases the slot so a second objective may be admitted."""
    from agent import autonomy

    autonomy.clear_active()
    try:
        autonomy.reserve_active("obj_a")
        assert autonomy.is_active("obj_a")
        released = autonomy.clear_active("obj_a")
        assert released is True
        assert not autonomy.is_active()
        # Second objective may now be admitted.
        r2 = autonomy.reserve_active("obj_b")
        assert r2.objective_id == "obj_b"
    finally:
        autonomy.clear_active()


def test_clear_with_mismatched_objective_is_noop():
    """clear_active(some_other_id) refuses to clobber a different active slot."""
    from agent import autonomy

    autonomy.clear_active()
    try:
        autonomy.reserve_active("obj_a")
        # Mismatched clear — the slot is still owned by obj_a.
        released = autonomy.clear_active("obj_b")
        assert released is False
        assert autonomy.is_active("obj_a"), (
            "clear_active(mismatched) must NOT release the slot"
        )
        # The original owner can still clear it.
        released = autonomy.clear_active("obj_a")
        assert released is True
    finally:
        autonomy.clear_active()


# --------------------------------------------------------------------
# Real production boundary: complete_task → clear_active
# --------------------------------------------------------------------

@pytest.fixture
def autonomy_completion_env(tmp_path, monkeypatch):
    """Provide a temp HERMES_HOME + a kanban board with a task in
    ``running`` status, plus a fresh autonomy slot."""
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None

    from agent import autonomy

    autonomy.clear_active()

    home = tmp_path / ".hermes"
    kanban_dir = home / "kanban"
    boards_dir = kanban_dir / "boards" / "demo-board"
    boards_dir.mkdir(parents=True, exist_ok=True)
    (boards_dir / "board.json").write_text(
        json.dumps({"slug": "demo-board"}), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Initialize the board's DB and seed a task in 'running' status.
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board="demo-board")
    kanban_db.create_task(
        conn,
        title="autonomy lifecycle test",
        body="",
        workspace_kind="scratch",
        initial_status="running",
    )
    # Rename to a known id.
    conn.execute(
        "UPDATE tasks SET id = ? WHERE rowid = (SELECT MIN(rowid) FROM tasks)",
        ("t_lifecycle",),
    )
    conn.close()

    yield home

    autonomy.clear_active()


def test_complete_task_releases_autonomy_slot(autonomy_completion_env, monkeypatch):
    """``complete_task`` releases the autonomy slot at the real boundary.

    Steps:
      1. Reserve the slot for ``t_lifecycle``.
      2. Call ``complete_task(t_lifecycle)`` (the real production code).
      3. Assert the slot is empty — a second reserve for a different
         objective succeeds.
    """
    from agent import autonomy
    from hermes_cli import kanban_db

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "demo-board")

    autonomy.reserve_active("t_lifecycle", reason="worker admitted")
    assert autonomy.is_active("t_lifecycle")

    # Drive the REAL completion path.
    conn = kanban_db.connect(board="demo-board")
    try:
        kanban_db.complete_task(conn, "t_lifecycle")
    finally:
        conn.close()

    # After completion, the slot must be released — a second reserve
    # for a DIFFERENT objective succeeds (no stale reservation).
    assert not autonomy.is_active("t_lifecycle"), (
        "complete_task must call clear_active(task_id) at the production boundary"
    )
    r2 = autonomy.reserve_active("t_lifecycle_b")
    assert r2.objective_id == "t_lifecycle_b"


def test_completion_does_not_release_other_objective(autonomy_completion_env, monkeypatch):
    """Completing ``t_lifecycle`` must NOT clobber a reservation owned by a
    DIFFERENT objective (defense in depth)."""
    from agent import autonomy
    from hermes_cli import kanban_db

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "demo-board")

    # The "active" reservation is owned by some OTHER objective id.
    # The completion of ``t_lifecycle`` must not affect it.
    autonomy.reserve_active("unrelated_objective")
    assert autonomy.is_active("unrelated_objective")

    conn = kanban_db.connect(board="demo-board")
    try:
        kanban_db.complete_task(conn, "t_lifecycle")
    finally:
        conn.close()

    # The unrelated reservation is still in place.
    assert autonomy.is_active("unrelated_objective"), (
        "completing one task must NOT release an unrelated active reservation"
    )
    autonomy.clear_active()


def test_active_preserved_during_running(autonomy_completion_env, monkeypatch):
    """The reservation is NOT cleared prematurely while the task is still
    active (status != done)."""
    from agent import autonomy
    from hermes_cli import kanban_db

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "demo-board")
    autonomy.reserve_active("t_lifecycle")
    assert autonomy.is_active("t_lifecycle")

    # Read the task — confirm it is NOT yet 'done'. The autonomy slot
    # must STILL be active.
    conn = kanban_db.connect(board="demo-board")
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", ("t_lifecycle",)
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] != "done", (
        "test setup failed: task should not start in 'done' state"
    )
    assert autonomy.is_active("t_lifecycle"), (
        "an active reservation must remain active while the task is "
        "still running/ready; clear_active fires at the completion "
        "boundary, not on read or other idle paths"
    )

    # Cleanup.
    autonomy.clear_active()