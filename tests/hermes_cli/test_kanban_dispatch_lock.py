"""Tests for the kanban dispatcher single-writer lock (issue #35240).

A ``hermes gateway run --replace`` / ``gateway restart`` from a shell on a
systemd/launchd host can leave an orphan dispatcher that escapes the
service cgroup, survives ``systemctl restart``, and becomes a second
long-lived writer on the same ``kanban.db`` — the documented root cause of
multi-writer SQLite WAL corruption. ``dispatch_once`` now wraps each tick in
a non-blocking, board-scoped dispatch lock so two dispatchers can never run
a reclaim/spawn/write tick concurrently. The losing dispatcher returns an
empty ``DispatchResult`` with ``skipped_locked=True`` and does no DB writes.
"""

from __future__ import annotations

import os
import subprocess
import threading

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c




def test_held_lock_skips_the_tick_without_writes(conn):
    """While another holder owns the board lock, dispatch_once must skip and
    must NOT invoke spawn_fn (no DB writes happen on a skipped tick)."""
    kb.create_task(conn, title="t", assignee="w")
    db_path = kb.kanban_db_path(board="default")

    spawn_calls: list = []

    def spy_spawn(task, workspace_path, board=None):
        spawn_calls.append(getattr(task, "id", task))
        return 999999

    # Hold the lock, then attempt a contended tick.
    with kb._dispatch_tick_lock(db_path) as held:
        assert held is True  # we genuinely acquired it
        result = kb.dispatch_once(conn, spawn_fn=spy_spawn)

    assert result.skipped_locked is True
    assert result.spawned == []
    assert spawn_calls == [], "spawn_fn must not run while the tick is locked out"




def test_lock_is_board_scoped(conn):
    """Holding board A's dispatch lock must not block a tick on board B —
    distinct boards have distinct DB files and tick independently."""
    db_default = kb.kanban_db_path(board="default")
    db_other = db_default.with_name("other-board-kanban.db")

    # Two different lock files → both acquirable simultaneously.
    with kb._dispatch_tick_lock(db_default) as held_a:
        assert held_a is True
        with kb._dispatch_tick_lock(db_other) as held_b:
            assert held_b is True, "a lock on a different board must be independent"


def test_dispatch_binds_pending_worker_before_release(
    conn,
    tmp_path,
    monkeypatch,
):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    task_id = kb.create_task(conn, title="bind before release", assignee="w")
    holder = {}

    def pending_spawn(task, workspace_path, board=None):
        pending = kb._spawn_behind_bootstrap(
            ["/usr/bin/true"],
            env=os.environ,
            cwd=str(tmp_path),
            stdout=subprocess.DEVNULL,
        )
        holder["pending"] = pending
        assert pending.proc.poll() is None
        return pending

    try:
        result = kb.dispatch_once(
            conn,
            spawn_fn=pending_spawn,
            reconcile_orphans=False,
        )
        pending = holder["pending"]
        assert pending.proc.wait(timeout=5) == 0
        assert result.spawned == [(task_id, "w", result.spawned[0][2])]
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, task.current_run_id)
        assert task.worker_fence is not None
        assert run.worker_fence == task.worker_fence
        assert task.worker_pid == pending.identity.pid
        assert task.worker_pgid == pending.identity.pgid
        assert task.worker_identity == pending.identity.token
    finally:
        pending = holder.get("pending")
        if pending is not None:
            pending.abort()


def test_dispatch_rejects_legacy_integer_spawn_without_process_fence(
    conn,
    monkeypatch,
):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    task_id = kb.create_task(conn, title="legacy pid", assignee="w")

    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda *_args, **_kwargs: 424242,
        reconcile_orphans=False,
    )

    assert result.spawned == []
    task = kb.get_task(conn, task_id)
    assert task.worker_pid is None
    assert task.worker_pgid is None
    assert task.worker_identity is None
    assert task.worker_fence is None
    events = list(
        conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? "
            "ORDER BY id",
            (task_id,),
        )
    )
    assert events[-1]["kind"] == "spawn_failed"
    assert "legacy" in events[-1]["payload"]


def test_dispatch_failed_bind_never_contaminates_new_claim_owner(
    conn,
    tmp_path,
    monkeypatch,
):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    task_id = kb.create_task(conn, title="foreign owner", assignee="w")
    holder = {}

    def losing_spawn(task, workspace_path, board=None):
        pending = kb._spawn_behind_bootstrap(
            ["/bin/sleep", "60"],
            env=os.environ,
            cwd=str(tmp_path),
            stdout=subprocess.DEVNULL,
        )
        holder["pending"] = pending
        with kb.connect() as racer:
            racer.execute(
                "UPDATE tasks SET claim_lock='fixture:new-owner' WHERE id=?",
                (task.id,),
            )
            racer.commit()
        return pending

    result = kb.dispatch_once(
        conn,
        spawn_fn=losing_spawn,
        reconcile_orphans=False,
    )

    assert result.spawned == []
    pending = holder["pending"]
    assert pending.proc.poll() is not None
    task = kb.get_task(conn, task_id)
    assert task.claim_lock == "fixture:new-owner"
    assert task.worker_fence is None
    run = kb.get_run(conn, task.current_run_id)
    assert run.worker_fence is not None
    assert kb.reap_terminal_attempt_fences(conn, limit=16) == [
        ("run", task.current_run_id)
    ]
    assert kb.get_task(conn, task_id).claim_lock == "fixture:new-owner"


def test_review_dispatch_uses_same_bind_before_release_contract(
    conn,
    tmp_path,
    monkeypatch,
):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
    task_id = kb.create_task(conn, title="review bind", assignee="w")
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
    conn.commit()
    holder = {}

    def pending_spawn(task, workspace_path, board=None):
        assert "sdlc-review" in task.skills
        pending = kb._spawn_behind_bootstrap(
            ["/usr/bin/true"],
            env=os.environ,
            cwd=str(tmp_path),
            stdout=subprocess.DEVNULL,
        )
        holder["pending"] = pending
        return pending

    try:
        result = kb.dispatch_once(
            conn,
            spawn_fn=pending_spawn,
            reconcile_orphans=False,
        )
        pending = holder["pending"]
        assert pending.proc.wait(timeout=5) == 0
        assert [item[0] for item in result.spawned] == [task_id]
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, task.current_run_id)
        assert task.worker_fence is not None
        assert run.worker_fence == task.worker_fence
    finally:
        pending = holder.get("pending")
        if pending is not None:
            pending.abort()


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_post_bind_release_failure_terminalizes_exact_attempt_then_reaps(
    conn,
    tmp_path,
    monkeypatch,
    lane,
):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
    task_id = kb.create_task(conn, title=f"{lane} release failure", assignee="w")
    if lane == "review":
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
        conn.commit()
    holder = {}

    def pending_spawn(task, workspace_path, board=None):
        pending = kb._spawn_behind_bootstrap(
            ["/bin/sleep", "60"],
            env=os.environ,
            cwd=str(tmp_path),
            stdout=subprocess.DEVNULL,
        )
        holder["pending"] = pending

        def fail_release():
            raise OSError("release failed after bind")

        pending.release = fail_release
        return pending

    result = kb.dispatch_once(
        conn,
        spawn_fn=pending_spawn,
        reconcile_orphans=False,
    )
    assert result.spawned == []
    pending = holder["pending"]
    assert pending.proc.poll() is not None
    task = kb.get_task(conn, task_id)
    run = kb.get_run(conn, task.current_run_id)
    assert task.status == lane
    assert run.ended_at is not None
    assert run.outcome == "spawn_failed"
    assert task.worker_fence == run.worker_fence
    assert task.worker_pid == run.worker_pid == pending.identity.pid
    assert task.worker_pgid == run.worker_pgid == pending.identity.pgid
    assert task.worker_identity == run.worker_identity == pending.identity.token

    assert kb.reap_terminal_attempt_fences(conn, limit=16) == [("task", task_id)]
    task = kb.get_task(conn, task_id)
    run = kb.get_run(conn, run.id)
    assert task.status == lane
    assert task.worker_fence is None and run.worker_fence is None
    assert task.claim_lock is None and run.claim_lock is None


def test_post_bind_release_failure_lost_claim_is_zero_delta(
    conn,
    tmp_path,
    monkeypatch,
):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    task_id = kb.create_task(conn, title="release ownership lost", assignee="w")
    holder = {}

    def snapshot():
        return (
            tuple(tuple(row) for row in conn.execute("SELECT * FROM tasks")),
            tuple(tuple(row) for row in conn.execute("SELECT * FROM task_runs")),
            tuple(tuple(row) for row in conn.execute("SELECT * FROM task_events")),
        )

    def pending_spawn(task, workspace_path, board=None):
        pending = kb._spawn_behind_bootstrap(
            ["/bin/sleep", "60"],
            env=os.environ,
            cwd=str(tmp_path),
            stdout=subprocess.DEVNULL,
        )
        holder["pending"] = pending

        def fail_release_after_owner_change():
            pending.abort()
            conn.execute(
                "UPDATE tasks SET claim_lock='fixture:new-owner' WHERE id=?",
                (task.id,),
            )
            conn.commit()
            holder["before"] = snapshot()
            raise OSError("release failed after ownership loss")

        pending.release = fail_release_after_owner_change
        return pending

    with pytest.raises(kb.SpawnBindError, match="lost exact attempt ownership"):
        kb.dispatch_once(
            conn,
            spawn_fn=pending_spawn,
            reconcile_orphans=False,
        )
    assert snapshot() == holder["before"]
    assert holder["pending"].proc.poll() is not None


@pytest.mark.parametrize("fatal", [kb.UnknownWorkerProcess, kb.SpawnBindError])
def test_run_daemon_stops_after_unrecoverable_dispatch_failure(
    kanban_home,
    monkeypatch,
    fatal,
):
    calls = []
    stop = threading.Event()

    def fail_once(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise fatal("fatal fence state")
        stop.set()
        return kb.DispatchResult()

    monkeypatch.setattr(kb, "dispatch_once", fail_once)
    monkeypatch.setattr(kb, "connect", lambda: kb.sqlite3.connect(":memory:"))
    with pytest.raises(fatal, match="fatal fence state"):
        kb.run_daemon(interval=0, stop_event=stop)
    assert calls == [1]
