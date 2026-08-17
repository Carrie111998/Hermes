"""Tests for the bounded kanban init lock (issue #36644).

`connect()` wrapped its entire body in an unbounded blocking `flock(LOCK_EX)`
on every call. A single process stalled inside the critical section blocked the
long-lived gateway dispatcher's next-tick `connect()` forever — no timeout, no
recovery, board silently stops being worked.

Two fixes, both covered here:
1. Fast path: once a path is initialized in this process, `connect()` skips the
   cross-process init lock entirely (nothing left to serialize), so a held lock
   cannot block a steady-state connect.
2. Bounded acquire: even on first-init, `_cross_process_init_lock` retries a
   non-blocking acquire up to a deadline, then proceeds (with a WARNING) rather
   than hanging.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

import hermes_state
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
    return home


def _hold_init_lock(db_path: Path):
    """Return (start_event, release_event, thread) holding the init lock."""
    holding = threading.Event()
    release = threading.Event()

    def _holder():
        with kb._cross_process_init_lock(db_path):
            holding.set()
            release.wait(timeout=10)

    t = threading.Thread(target=_holder, daemon=True)
    t.start()
    assert holding.wait(timeout=5), "holder thread never acquired the lock"
    return release, t


def test_initialized_path_connect_skips_init_lock(kanban_home):
    """A connect to an already-initialized path must not block on the init lock."""
    db_path = kb.kanban_db_path(board="default")
    # Initialize once.
    kb.connect().close()
    assert str(db_path.resolve()) in kb._INITIALIZED_PATHS

    # Hold the init lock; a fast-path connect must return promptly anyway.
    release, t = _hold_init_lock(db_path)
    try:
        start = time.monotonic()
        kb.connect().close()
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"fast-path connect blocked on the init lock ({elapsed:.2f}s)"
    finally:
        release.set()
        t.join(timeout=5)


def _init_lock_is_held(db_path: Path) -> bool:
    """Is the cross-process init lock for ``db_path`` held right now?

    Probes with a second handle to the same lock file. Both backends conflict
    with themselves across handles within one process — ``flock`` locks are
    per open-file-description, and Windows byte-range locks are per handle —
    so this reports a lock held by ``connect()`` on this very thread.
    """
    lock_path = db_path.with_name(db_path.name + ".init.lock")
    handle = lock_path.open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return False
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def test_wal_setup_runs_outside_the_cross_process_init_lock(kanban_home, monkeypatch):
    """Per-connection WAL/pragma setup must NOT hold the cross-process lock.

    On a hot board the WAL probe is the statement that attaches the ``-shm``
    and takes the first read lock, so it queues behind live writers for 1-2s
    per process. Holding the init lock across it made every first-connecting
    process pay for all its predecessors: measured on Windows with 12 workers
    first-connecting at once, serialized hold time reached 16s against the 10s
    budget, so the "not acquired within 10s" fallback fired on healthy queues
    instead of wedged holders. Narrowing the lock cut that to under a second.
    """
    db_path = kb.kanban_db_path(board="default")
    observed = {}

    real = hermes_state.apply_wal_with_fallback

    def spy(conn, **kwargs):
        observed["held"] = _init_lock_is_held(db_path)
        return real(conn, **kwargs)

    monkeypatch.setattr(hermes_state, "apply_wal_with_fallback", spy)
    kb.connect().close()

    assert observed.get("held") is False, (
        "apply_wal_with_fallback ran while holding the cross-process init lock; "
        "that serializes 1-2s of per-connection setup per process and blows the "
        f"{kb._INIT_LOCK_TIMEOUT_SECONDS:.0f}s budget under ordinary contention"
    )


def test_schema_ddl_still_holds_the_cross_process_init_lock(kanban_home, monkeypatch):
    """The narrowing must not un-protect the section that needs serializing.

    ``executescript(SCHEMA_SQL)`` + the additive ALTER TABLE pass are the only
    writes in ``connect()``; they must stay single-writer across the host.
    """
    db_path = kb.kanban_db_path(board="default")
    observed = {}

    real = kb._migrate_add_optional_columns

    def spy(conn):
        observed["held"] = _init_lock_is_held(db_path)
        return real(conn)

    monkeypatch.setattr(kb, "_migrate_add_optional_columns", spy)
    kb.connect().close()

    assert observed.get("held") is True, (
        "schema init ran without the cross-process init lock — concurrent "
        "processes can now race the additive migration pass"
    )


def test_integrity_probe_still_holds_the_cross_process_init_lock(kanban_home, monkeypatch):
    """The corruption probes stay serialized so one process wins the verdict."""
    db_path = kb.kanban_db_path(board="default")
    observed = {}

    real = kb._guard_existing_db_is_healthy

    def spy(path):
        observed["held"] = _init_lock_is_held(db_path)
        return real(path)

    monkeypatch.setattr(kb, "_guard_existing_db_is_healthy", spy)
    kb.connect().close()

    assert observed.get("held") is True, (
        "the integrity probe ran without the cross-process init lock"
    )


def test_first_init_connect_is_bounded_when_lock_held(kanban_home, monkeypatch):
    """First-init connect must time out the cross-process lock and proceed,
    not hang forever, when another holder owns it."""
    monkeypatch.setattr(kb, "_INIT_LOCK_TIMEOUT_SECONDS", 0.6)
    db_path = kb.kanban_db_path(board="default")

    release, t = _hold_init_lock(db_path)
    try:
        start = time.monotonic()
        conn = kb.connect()  # path NOT yet initialized — must take the bounded path
        conn.close()
        elapsed = time.monotonic() - start
        # Proceeded within roughly the timeout window (not unbounded).
        assert 0.4 <= elapsed < 3.0, f"expected bounded ~0.6s acquire, got {elapsed:.2f}s"
        assert str(db_path.resolve()) in kb._INITIALIZED_PATHS
    finally:
        release.set()
        t.join(timeout=5)
