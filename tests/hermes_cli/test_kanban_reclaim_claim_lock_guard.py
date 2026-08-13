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

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tests.attempt_fence_helpers import (
    create_bound_attempt,
    logical_board_snapshot,
    process_tuple,
)


darwin_only = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="libproc fence is macOS-only",
)


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


@pytest.fixture
def worker_identity():
    leader = subprocess.Popen(["sleep", "60"], process_group=0)
    identity = kb._darwin_process_identity(leader.pid)
    assert identity is not None
    try:
        yield identity
    finally:
        try:
            os.killpg(identity.pgid, 9)
        except ProcessLookupError:
            pass
        leader.wait(timeout=5)


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


@pytest.mark.parametrize("group_state", ["alive", "unknown"])
@darwin_only
def test_stale_fenced_claim_is_never_mutated_without_proven_death(
    conn,
    monkeypatch,
    group_state,
    worker_identity,
):
    task_id, _claimed, _raw = create_bound_attempt(
        conn,
        leader_identity=worker_identity,
    )
    conn.execute(
        "UPDATE tasks SET claim_expires=0 WHERE id=?",
        (task_id,),
    )
    conn.commit()
    before = logical_board_snapshot(conn)
    monkeypatch.setattr(kb, "_fenced_group_state", lambda _fence: group_state)

    assert kb.release_stale_claims(conn) == 0
    assert logical_board_snapshot(conn) == before


@pytest.mark.parametrize(
    ("group_state", "reason"),
    [("alive", "operator checked"), ("unknown", "operator checked"), ("dead", None)],
)
@darwin_only
def test_manual_fenced_reclaim_requires_death_and_reason(
    conn,
    monkeypatch,
    group_state,
    reason,
    worker_identity,
):
    task_id, _claimed, _raw = create_bound_attempt(
        conn,
        leader_identity=worker_identity,
    )
    before = logical_board_snapshot(conn)
    monkeypatch.setattr(kb, "_fenced_group_state", lambda _fence: group_state)

    assert not kb.reclaim_task(conn, task_id, reason=reason)
    assert logical_board_snapshot(conn) == before


@darwin_only
def test_fenced_reassign_reclaim_requires_explicit_reason(
    conn, monkeypatch, worker_identity,
):
    task_id, _claimed, _raw = create_bound_attempt(
        conn,
        leader_identity=worker_identity,
    )
    before = logical_board_snapshot(conn)
    monkeypatch.setattr(kb, "_fenced_group_state", lambda _fence: "dead")

    assert not kb.reassign_task(
        conn,
        task_id,
        "yonatan",
        reclaim_first=True,
    )
    assert logical_board_snapshot(conn) == before


@darwin_only
def test_manual_fenced_reclaim_clears_only_proven_dead_exact_owner(
    conn,
    monkeypatch,
    worker_identity,
):
    task_id, claimed, _raw = create_bound_attempt(
        conn,
        leader_identity=worker_identity,
    )
    monkeypatch.setattr(kb, "_fenced_group_state", lambda _fence: "dead")

    assert kb.reclaim_task(conn, task_id, reason="verified process-group death")
    task = kb.get_task(conn, task_id)
    assert task.status == "ready"
    assert process_tuple(task) == (None, None, None, None, None, None)
    run = conn.execute(
        "SELECT outcome, claim_lock, worker_pid, worker_pgid, worker_identity, "
        "worker_fence FROM task_runs WHERE id=?",
        (claimed.current_run_id,),
    ).fetchone()
    assert tuple(run) == ("reclaimed", None, None, None, None, None)
    event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='reclaimed' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert "verified process-group death" in event["payload"]


@darwin_only
def test_manual_reclaim_cannot_reopen_a_terminal_fenced_outcome(
    conn,
    monkeypatch,
    worker_identity,
):
    task_id, claimed, _raw = create_bound_attempt(
        conn,
        leader_identity=worker_identity,
    )
    conn.execute(
        "UPDATE tasks SET status='done', result='finished' WHERE id=?",
        (task_id,),
    )
    conn.execute(
        "UPDATE task_runs SET status='done', outcome='completed' WHERE id=?",
        (claimed.current_run_id,),
    )
    conn.commit()
    before = logical_board_snapshot(conn)
    monkeypatch.setattr(kb, "_fenced_group_state", lambda _fence: "dead")

    assert not kb.reclaim_task(conn, task_id, reason="already terminal")
    assert logical_board_snapshot(conn) == before
    assert kb.reap_terminal_attempt_fences(conn) == [("task", task_id)]
    assert kb.get_task(conn, task_id).status == "done"


@pytest.mark.parametrize(
    "recovery",
    ["max_runtime", "stale", "orphan", "crashed"],
)
@darwin_only
def test_legacy_recovery_paths_never_mutate_a_fenced_attempt(
    conn,
    monkeypatch,
    recovery,
    worker_identity,
):
    task_id, claimed, raw = create_bound_attempt(
        conn,
        leader_identity=worker_identity,
    )
    host_lock = f"{kb._claimer_id().split(':', 1)[0]}:legacy-recovery"
    fence = json.loads(raw)
    fence["claim_lock"] = host_lock
    raw = json.dumps(fence, sort_keys=True, separators=(",", ":"))
    conn.execute(
        "UPDATE tasks SET claim_lock=?, worker_fence=? WHERE id=?",
        (host_lock, raw, task_id),
    )
    conn.execute(
        "UPDATE task_runs SET claim_lock=?, worker_fence=? WHERE id=?",
        (host_lock, raw, claimed.current_run_id),
    )
    if recovery == "max_runtime":
        conn.execute(
            "UPDATE tasks SET max_runtime_seconds=1, started_at=1 WHERE id=?",
            (task_id,),
        )
        conn.execute(
            "UPDATE task_runs SET started_at=1 WHERE task_id=?",
            (task_id,),
        )
    elif recovery == "stale":
        conn.execute(
            "UPDATE tasks SET started_at=1, last_heartbeat_at=NULL WHERE id=?",
            (task_id,),
        )
        conn.execute(
            "UPDATE task_runs SET started_at=1 WHERE task_id=?",
            (task_id,),
        )
    elif recovery == "orphan":
        conn.execute(
            "UPDATE tasks SET claim_expires=NULL WHERE id=?",
            (task_id,),
        )
    else:
        conn.execute(
            "UPDATE tasks SET started_at=1 WHERE id=?",
            (task_id,),
        )
    conn.commit()
    before = logical_board_snapshot(conn)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    signals = []

    if recovery == "max_runtime":
        result = kb.enforce_max_runtime(
            conn,
            signal_fn=lambda pid, sig: signals.append((pid, sig)),
        )
    elif recovery == "stale":
        result = kb.detect_stale_running(
            conn,
            stale_timeout_seconds=1,
            signal_fn=lambda pid, sig: signals.append((pid, sig)),
        )
    elif recovery == "orphan":
        result = kb.reconcile_orphaned_running(conn)
    else:
        result = kb.detect_crashed_workers(conn)

    assert result == []
    assert signals == []
    assert logical_board_snapshot(conn) == before
