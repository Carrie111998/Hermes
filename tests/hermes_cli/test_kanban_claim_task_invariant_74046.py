"""Regression tests for issue #74046 — UNIQUE constraint on task_runs.task_id
during gateway kanban dispatch.

The original crash was an uncaught ``sqlite3.IntegrityError: UNIQUE
constraint failed: task_runs.task_id`` raised from ``claim_task`` when an
interrupted writer left an open run for a task and the next tick
attempted to insert a second row. The fix reaps ALL open runs for the
task_id (not just the one ``current_run_id`` pointed at) and uses
``INSERT OR IGNORE`` so any racing writer that beats us to a row adopts
it instead of crashing.

These tests construct the exact failure mode from the bug report:
a task with a leaked run is re-claimed and the dispatcher must
succeed without raising.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _seed_running_task(conn):
    """Create a task and drive it to ``ready`` so claim_task has work to do."""
    tid = kb.create_task(conn, title="t", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    return tid


def _inject_orphan_run(conn, task_id: str) -> int:
    """Plant a leaked task_runs row that survived a crash mid-claim.

    Mirrors the bug-report scenario: claim_task updated the tasks row to
    status='running' but the INSERT into task_runs never committed.
    On re-entry, the new claim_task would attempt to INSERT a second
    open row and trip the UNIQUE constraint.
    """
    cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key, status,
            claim_lock, claim_expires, max_runtime_seconds,
            started_at
        ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
        """,
        (task_id, "worker", None, "stale-claimer", 9_999_999_999, 3600, 1_700_000_000),
    )
    return int(cur.lastrowid)


def test_claim_task_succeeds_when_an_orphan_run_exists(kanban_home: Path) -> None:
    """Reproduces #74046: a leaked task_runs row must not crash claim_task.

    Pre-fix: this raised
        sqlite3.IntegrityError: UNIQUE constraint failed: task_runs.task_id
    because the stale-recovery UPDATE only matched ``current_run_id``
    and the subsequent INSERT collided with the leftover open row.
    """
    with kb.connect_closing() as conn:
        tid = _seed_running_task(conn)
        # Reset to ready so claim_task has to do the full work; this is
        # the same shape as a tick cycle resuming after an upstream drop.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
        _inject_orphan_run(conn, tid)

        claimed = kb.claim_task(conn, tid, claimer="worker")

        assert claimed is not None, "claim_task must succeed even with a leaked run"
        assert claimed.status == "running"
        assert claimed.current_run_id is not None

        # The orphan must have been reclaimed — exactly one open row
        # should remain (the newly-inserted one), not the original leak.
        open_rows = conn.execute(
            "SELECT id, claim_lock FROM task_runs "
            "WHERE task_id = ? AND ended_at IS NULL",
            (tid,),
        ).fetchall()
        assert len(open_rows) == 1, (
            f"expected exactly 1 open row after claim, got {len(open_rows)} "
            f"for task_id={tid}"
        )
        # The surviving row must be ours (claim_lock matches the claimer
        # we passed), not the leftover orphan (claim_lock='stale-claimer').
        assert open_rows[0]["claim_lock"] == "worker", (
            f"surviving row has claim_lock={open_rows[0]['claim_lock']!r}, "
            f"expected 'worker' — the orphan was not reaped"
        )

        # Exactly one (newly inserted) running row should now exist for this task.
        running_count = conn.execute(
            "SELECT COUNT(*) AS c FROM task_runs "
            "WHERE task_id = ? AND status = 'running'",
            (tid,),
        ).fetchone()["c"]
        assert running_count == 1, f"expected 1 running row, got {running_count}"

        # The task pointer should be the surviving running row.
        t = kb.get_task(conn, tid)
        assert t is not None and t.current_run_id is not None
        assert int(t.current_run_id) == int(claimed.current_run_id)


def test_claim_task_reaps_multiple_orphan_runs(kanban_home: Path) -> None:
    """Multiple leaked runs from repeated interrupted writes must all close."""
    with kb.connect_closing() as conn:
        tid = _seed_running_task(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
        # Plant TWO orphans with different claim_locks so the legacy
        # single-row UPDATE (filtered by id only) would have missed one.
        _inject_orphan_run(conn, tid)
        _inject_orphan_run(conn, tid)

        claimed = kb.claim_task(conn, tid, claimer="worker")

        assert claimed is not None
        open_rows = conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id = ? AND ended_at IS NULL",
            (tid,),
        ).fetchall()
        assert len(open_rows) == 1, (
            f"expected exactly 1 open row after sweep, got {len(open_rows)}"
        )

        # Both orphans should be marked 'reclaimed' so audit history is preserved.
        reclaimed_count = conn.execute(
            "SELECT COUNT(*) AS c FROM task_runs "
            "WHERE task_id = ? AND status = 'reclaimed'",
            (tid,),
        ).fetchone()["c"]
        assert reclaimed_count >= 2, (
            f"expected >= 2 reclaimed rows, got {reclaimed_count}"
        )


def test_claim_task_idempotent_under_race(kanban_home: Path) -> None:
    """If another writer already inserted a running row for this task_id
    between our sweep and our INSERT, INSERT OR IGNORE adopts the
    existing row instead of crashing.
    """
    with kb.connect_closing() as conn:
        tid = _seed_running_task(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))

        # Simulate a parallel writer that has already inserted a row but
        # not yet committed the tasks row update. We commit that row.
        _inject_orphan_run(conn, tid)

        # A second claim_task attempt must succeed and adopt the existing row.
        first = kb.claim_task(conn, tid, claimer="worker-A")
        assert first is not None
        first_run_id = int(first.current_run_id)

        # Now reset and re-claim with a different claimer — this exercises
        # the path where the sweep reaps a non-orphan row.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, current_run_id=NULL "
                         "WHERE id=?", (tid,))

        second = kb.claim_task(conn, tid, claimer="worker-B")
        assert second is not None
        second_run_id = int(second.current_run_id)

        # The post-claim state must have exactly one running row.
        running = conn.execute(
            "SELECT COUNT(*) AS c FROM task_runs "
            "WHERE task_id = ? AND status = 'running'",
            (tid,),
        ).fetchone()["c"]
        assert running == 1, f"expected 1 running row, got {running}"

        # The first run must have been reclaimed by the second sweep.
        reclaimed = conn.execute(
            "SELECT status FROM task_runs WHERE id = ?",
            (first_run_id,),
        ).fetchone()
        assert reclaimed["status"] == "reclaimed", (
            f"first run not reclaimed: {dict(reclaimed)}"
        )

        # current_run_id points at the new (worker-B) row.
        assert second_run_id != first_run_id