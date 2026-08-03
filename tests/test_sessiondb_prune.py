"""Tests for SessionDB._prune_dead_read_conns and cross-thread close safety.

Covers the three critical fixes from PR #76424:
- check_same_thread=False in _get_read_conn
- Two-phase prune (close-before-pop, I/O outside lock, retain on failure)
- Cross-thread close without ProgrammingError
"""

import logging
import threading
import time

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    d.create_session(session_id="s1", source="cli", model="m")
    yield d
    d.close()


# ── test_prune_dead_read_conns_success ────────────────────────────


@pytest.mark.requires_wal
def test_prune_dead_read_conns_success(db):
    """Threads open read connections, then die — prune removes their entries.

    After prune, only the main thread's read connection (if any) remains
    in _read_conns.
    """
    main_tid = threading.get_ident()

    def open_and_die(results: list):
        conn = db._get_read_conn()
        assert conn is not None
        results.append(threading.get_ident())
    # Open a read connection in the main thread so it has an entry
    main_conn = db._get_read_conn()
    assert main_conn is not None

    results: list[int] = []
    threads = []
    for _ in range(4):
        t = threading.Thread(target=open_and_die, args=(results,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Threads are dead now — their tids should be in _read_conns
    dead_tids = set(results)
    with db._read_conns_lock:
        pre_tids = set(db._read_conns.keys())
    assert dead_tids.issubset(pre_tids), "dead thread entries missing before prune"

    pruned = db._prune_dead_read_conns()
    assert pruned == len(threads), f"expected {len(threads)} pruned, got {pruned}"

    with db._read_conns_lock:
        remaining = dict(db._read_conns)
    assert main_tid in remaining, "main thread entry should be present"
    assert main_tid not in dead_tids or True  # main_tid not in dead_tids by construction
    # Check no dead tids remain
    for tid in dead_tids:
        assert tid not in remaining, f"dead tid {tid} leaked after prune"


# ── test_prune_dead_read_conns_failed_close_retained ──────────────


@pytest.mark.requires_wal
def test_prune_dead_read_conns_failed_close_retained(db, monkeypatch, caplog):
    """When conn.close() raises, the entry stays in _read_conns and a warning is logged."""

    def open_conn():
        db._get_read_conn()

    t = threading.Thread(target=open_conn)
    t.start()
    t.join()

    with db._read_conns_lock:
        dead_tid = next(
            tid for tid in db._read_conns if tid != threading.get_ident()
        )
        dead_conn = db._read_conns[dead_tid]

    # Make close() fail on the dead thread's connection
    original_close = dead_conn.close
    close_failed = threading.Event()

    def failing_close():
        close_failed.set()
        raise OSError("simulated close failure")

    monkeypatch.setattr(dead_conn, "close", failing_close)

    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        pruned = db._prune_dead_read_conns()

    assert pruned == 0, "nothing should be pruned when close fails"
    assert close_failed.is_set(), "failing_close must have been called"

    with db._read_conns_lock:
        assert dead_tid in db._read_conns, "failed-close entry must be retained"
        assert db._read_conns[dead_tid] is dead_conn, "retained entry must be same object"

    assert any(
        "Failed to close dead-thread read connection" in rec.message
        for rec in caplog.records
    ), "warning must be logged on close failure"

    # Restore close so the cleanup path succeeds
    monkeypatch.setattr(dead_conn, "close", original_close)


# ── test_cross_thread_close_with_check_same_thread_false ──────────


@pytest.mark.requires_wal
def test_cross_thread_close_with_check_same_thread_false(db):
    """Thread A opens a connection; Thread B closes it via prune — no ProgrammingError.

    Without check_same_thread=False, sqlite3 raises ProgrammingError when a
    connection is closed from a different thread than the one that opened it.
    """
    conn_holder: list = []
    opened = threading.Event()

    def thread_a():
        conn_holder.append(db._get_read_conn())
        opened.set()
        # Don't close — simulate thread death so prune finds it

    t = threading.Thread(target=thread_a)
    t.start()
    t.join()
    assert opened.wait(5), "thread A must open a connection"

    assert len(conn_holder) == 1
    assert conn_holder[0] is not None

    # Thread A is dead. Prune should close its connection from main thread
    # without raising ProgrammingError.
    pruned = db._prune_dead_read_conns()
    assert pruned == 1, f"expected 1 pruned, got {pruned}"

    with db._read_conns_lock:
        assert threading.get_ident() in db._read_conns or len(db._read_conns) == 0
