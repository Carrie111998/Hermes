"""Tests: reclaim paths are claim-lock-aware so they can't desync a re-claimed
task (issue #36910).

A stale crash/stale-claim/max-runtime reclaim, computed from a snapshot of an
OLD worker, used to reset ``tasks.status`` back to ``ready`` with only a
``WHERE status='running'`` guard. If the task had since been reclaimed AND
re-claimed by a NEW worker (new run, new claim_lock, live pid), that stale
UPDATE clobbered the live task: ``tasks.status='ready'`` while the new
``task_runs.status='running'`` and the worker kept executing — the board showed
the task in the Ready lane and the dispatcher could treat live work as
available. The reset is now gated on the snapshot's ``claim_lock`` (and pid),
so it only fires when the task is still owned by the worker the reclaim was
computed for.
"""

from __future__ import annotations

import subprocess
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


def test_stale_crash_reset_rejected_for_reclaimed_task(conn):
    """A reset carrying an OLD worker's claim_lock must NOT clobber a task
    that has since been re-claimed by a new worker."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="desync", assignee="w")

    # Worker A claims, then dies.
    kb.claim_task(conn, tid, claimer=f"{host}:A")
    dead = subprocess.Popen(["true"])
    dead.wait()
    kb._set_worker_pid(conn, tid, dead.pid)
    old = conn.execute(
        "SELECT claim_lock, worker_pid FROM tasks WHERE id=?", (tid,)
    ).fetchone()

    # Reclaim + re-claim by worker B (alive).
    conn.execute(
        "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, "
        "worker_pid=NULL, current_run_id=NULL WHERE id=?",
        (tid,),
    )
    conn.commit()
    kb.claim_task(conn, tid, claimer=f"{host}:B")
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        kb._set_worker_pid(conn, tid, sleeper.pid)

        # The stale reset for worker A — same shape as the guarded UPDATE in
        # detect_crashed_workers — must reject (rowcount 0) because B owns it.
        cur = conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL "
            "WHERE id=? AND status='running' AND worker_pid=? AND claim_lock IS ?",
            (tid, old["worker_pid"], old["claim_lock"]),
        )
        conn.commit()
        assert cur.rowcount == 0, "stale reclaim wrongly clobbered the re-claimed task"

        final = conn.execute(
            "SELECT status, claim_lock FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        assert final["status"] == "running"
        assert final["claim_lock"] == f"{host}:B"
    finally:
        sleeper.terminate()


def test_genuine_crash_still_reclaims(conn):
    """When the claim_lock still matches the dead worker, the crash reclaim
    fires normally — the guard must not break the legitimate path."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="legit", assignee="w")
    kb.claim_task(conn, tid, claimer=f"{host}:A")
    dead = subprocess.Popen(["true"])
    dead.wait()
    kb._set_worker_pid(conn, tid, dead.pid)
    # Rewind started_at so the launch grace window doesn't skip the check.
    conn.execute("UPDATE tasks SET started_at = started_at - 9999 WHERE id=?", (tid,))
    conn.execute(
        "UPDATE task_runs SET started_at = started_at - 9999 WHERE task_id=?", (tid,)
    )
    conn.commit()
    kb._record_worker_exit(dead.pid, 1 << 8)  # nonzero exit → crash

    crashed = kb.detect_crashed_workers(conn)
    assert tid in crashed
    final = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    assert final["status"] in ("ready", "blocked", "todo")


@pytest.mark.parametrize("stale_dimension", ["run", "claim"])
def test_manual_reclaim_expectation_mismatch_has_no_signal_side_effect(
    conn, stale_dimension,
):
    """A stale run handle fails before PID or containment termination."""
    tid = kb.create_task(conn, title="exact manual reclaim", assignee="w")
    assert kb.claim_task(conn, tid, claimer="worker:exact")
    row = conn.execute(
        "SELECT current_run_id, claim_lock FROM tasks WHERE id=?",
        (tid,),
    ).fetchone()
    conn.execute("UPDATE tasks SET worker_pid=424242 WHERE id=?", (tid,))
    conn.execute(
        "UPDATE task_runs SET worker_pid=424242 WHERE id=?",
        (row["current_run_id"],),
    )
    conn.commit()
    signals = []

    expected_run_id = int(row["current_run_id"])
    expected_claim_lock = row["claim_lock"]
    if stale_dimension == "run":
        expected_run_id += 1
    else:
        expected_claim_lock = "worker:stale"

    assert not kb.reclaim_task(
        conn,
        tid,
        expected_run_id=expected_run_id,
        expected_claim_lock=expected_claim_lock,
        signal_fn=lambda pid, sig: signals.append((pid, sig)),
    )
    assert signals == []
    final = conn.execute(
        "SELECT status, current_run_id, claim_lock, worker_pid FROM tasks WHERE id=?",
        (tid,),
    ).fetchone()
    assert final["status"] == "running"
    assert int(final["current_run_id"]) == int(row["current_run_id"])
    assert final["claim_lock"] == row["claim_lock"]
    assert int(final["worker_pid"]) == 424242


def test_exact_legacy_reclaim_serializes_validation_and_signal(conn):
    """A successor cannot publish a recycled PID between check and signal."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="legacy signal race", assignee="w")
    assert kb.claim_task(conn, tid, claimer=f"{host}:old")
    old = conn.execute(
        "SELECT current_run_id, claim_lock FROM tasks WHERE id=?",
        (tid,),
    ).fetchone()
    assert old["current_run_id"] is not None
    assert old["claim_lock"] is not None
    recycled_pid = 424242
    conn.execute(
        "UPDATE tasks SET worker_pid=? WHERE id=?",
        (recycled_pid, tid),
    )
    conn.execute(
        "UPDATE task_runs SET worker_pid=? WHERE id=?",
        (recycled_pid, old["current_run_id"]),
    )
    conn.commit()
    outcome = {"writer_committed": False, "writer_blocked": False}
    signals = []

    def race_successor_before_signal(pid, sig):
        other = sqlite3.connect(kb.kanban_db_path(), timeout=0)
        try:
            other.execute("PRAGMA busy_timeout=0")
            other.execute("BEGIN IMMEDIATE")
            now = int(time.time())
            successor_lock = f"{host}:successor"
            other.execute(
                "UPDATE task_runs SET ended_at=?, status='ended', outcome='done' "
                "WHERE id=?",
                (now, old["current_run_id"]),
            )
            cur = other.execute(
                "INSERT INTO task_runs "
                "(task_id, status, claim_lock, worker_pid, started_at) "
                "VALUES (?, 'running', ?, ?, ?)",
                (tid, successor_lock, recycled_pid, now),
            )
            other.execute(
                "UPDATE tasks SET status='running', claim_lock=?, "
                "worker_pid=?, current_run_id=?, started_at=? WHERE id=?",
                (successor_lock, recycled_pid, int(cur.lastrowid), now, tid),
            )
            other.commit()
            outcome["writer_committed"] = True
        except sqlite3.OperationalError:
            outcome["writer_blocked"] = True
            other.rollback()
        finally:
            other.close()
        signals.append((pid, sig))

    assert kb.reclaim_task(
        conn,
        tid,
        expected_run_id=int(old["current_run_id"]),
        expected_claim_lock=old["claim_lock"],
        signal_fn=race_successor_before_signal,
    )
    assert outcome == {"writer_committed": False, "writer_blocked": True}
    assert len(signals) == 1
