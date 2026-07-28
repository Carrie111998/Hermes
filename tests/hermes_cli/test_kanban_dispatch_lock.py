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

import multiprocessing
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
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


def test_uncontended_tick_runs_and_is_not_skipped(conn):
    """With no other holder, a tick runs normally and skipped_locked is False."""
    kb.create_task(conn, title="t", assignee="w")
    result = kb.dispatch_once(conn)
    assert result.skipped_locked is False


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


def test_lock_releases_so_next_tick_runs(conn):
    """After the holder releases, the next tick is no longer skipped."""
    kb.create_task(conn, title="t", assignee="w")
    db_path = kb.kanban_db_path(board="default")

    with kb._dispatch_tick_lock(db_path) as held:
        assert held is True
        assert kb.dispatch_once(conn).skipped_locked is True

    # Lock released — a fresh tick proceeds.
    assert kb.dispatch_once(conn).skipped_locked is False


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


def test_reentrant_same_path_lock_is_exclusive(conn):
    """A second acquisition of the SAME board's lock from a sibling context
    must report not-held (the flock is exclusive within the host)."""
    db_path = kb.kanban_db_path(board="default")
    with kb._dispatch_tick_lock(db_path) as held_a:
        assert held_a is True
        with kb._dispatch_tick_lock(db_path) as held_b:
            assert held_b is False, "same-board lock must be exclusive"


def test_write_txn_shares_the_dispatch_tick_lock_in_process(conn):
    """``write_txn`` must take the *same* lock file as ``_dispatch_tick_lock``.

    Regression for the gap described in FASE2_escopo_correcao.md: dashboard/
    CLI writes (which go through ``write_txn``) previously had no cross-
    process serialization against the dispatcher tick. This does not
    exercise real OS-level blocking (see the multiprocessing test below for
    that); it pins the invariant that both code paths resolve to the exact
    same lock file for a given board, which is what makes them mutually
    exclusive across processes.
    """
    db_path = kb.kanban_db_path(board="default")
    dispatch_lock_path = db_path.with_name(db_path.name + ".dispatch.lock")
    resolved = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    write_lock_path = resolved.with_name(resolved.name + ".dispatch.lock")
    assert write_lock_path == dispatch_lock_path.resolve()

    kb.create_task(conn, title="t", assignee="w")
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("lock-test", "test", None, 0),
        )


def _hold_dispatch_lock_then_signal(db_path_str, ready_evt, release_evt):
    """Child process: acquire the board's dispatch lock and hold it."""
    import fcntl

    db_path = Path(db_path_str)
    lock_path = db_path.with_name(db_path.name + ".dispatch.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    ready_evt.set()
    release_evt.wait(timeout=10)
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def test_write_txn_blocks_behind_a_cross_process_dispatch_lock_holder(conn):
    """A ``write_txn`` must block while another OS process holds the lock."""
    db_path = kb.kanban_db_path(board="default")
    kb.create_task(conn, title="t", assignee="w")

    ready_evt = multiprocessing.Event()
    release_evt = multiprocessing.Event()
    proc = multiprocessing.Process(
        target=_hold_dispatch_lock_then_signal,
        args=(str(db_path), ready_evt, release_evt),
    )
    proc.start()
    try:
        assert ready_evt.wait(timeout=5), "child never acquired the dispatch lock"
        started = time.monotonic()

        import threading

        def _release_after_delay():
            time.sleep(0.3)
            release_evt.set()

        releaser = threading.Thread(target=_release_after_delay)
        releaser.start()

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET priority = 5 WHERE id = "
                "(SELECT id FROM tasks LIMIT 1)"
            )
        elapsed = time.monotonic() - started
        releaser.join()
        assert elapsed >= 0.25, (
            "write_txn returned before the external dispatch-lock holder "
            "released it — the cross-process lock is not being honoured"
        )
    finally:
        release_evt.set()
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)

    row = conn.execute("PRAGMA integrity_check").fetchone()
    assert row[0] == "ok"
