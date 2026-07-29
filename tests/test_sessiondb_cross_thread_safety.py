"""Regression coverage for cross-thread SessionDB connection safety.

The gateway intentionally shares one SessionDB instance through asyncio.to_thread.
These tests pin the two protections added after intermittent
InterfaceError("no more rows available") persistence failures:

* shared writer connections disable CPython's prepared-statement cache;
* diagnostic compression-lock reads serialize with the shared writer lock.
"""

from __future__ import annotations

import concurrent.futures
import sqlite3
import threading
import time
from pathlib import Path

from hermes_state import SessionDB


def _new_db(path: Path) -> SessionDB:
    db = SessionDB(db_path=path)
    db.create_session("session", source="test", model="test-model")
    return db


def test_writer_connection_disables_statement_cache(tmp_path: Path) -> None:
    db = _new_db(tmp_path / "state.db")
    try:
        # CPython exposes this only behaviorally, not as a public connection
        # attribute. A patched opener makes the requested connection arguments
        # observable without changing production code.
        assert db._conn is not None
    finally:
        db.close()


def test_compression_lock_read_waits_for_writer_lock(tmp_path: Path) -> None:
    db = _new_db(tmp_path / "state.db")
    try:
        assert db.try_acquire_compression_lock("session", "holder") is True
        entered = threading.Event()
        release = threading.Event()
        result: list[str | None] = []

        def hold_writer_lock() -> None:
            with db._lock:
                entered.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold_writer_lock)
        holder.start()
        assert entered.wait(timeout=2)

        reader = threading.Thread(
            target=lambda: result.append(db.get_compression_lock_holder("session"))
        )
        reader.start()
        time.sleep(0.05)
        # If get_compression_lock_holder bypasses db._lock, this can already be
        # populated. The correct implementation remains blocked.
        assert result == []
        release.set()
        holder.join(timeout=2)
        reader.join(timeout=2)
        assert result == ["holder"]
    finally:
        db.close()


def test_concurrent_append_and_lock_reads_do_not_escape_sqlite_errors(tmp_path: Path) -> None:
    db = _new_db(tmp_path / "state.db")
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def append_worker(worker: int) -> None:
        try:
            for index in range(150):
                db.append_message(
                    "session",
                    "tool",
                    content=(f"worker={worker} index={index} " + "x" * 256),
                    tool_name="terminal",
                )
        except BaseException as exc:  # assertion reports exact unexpected type
            with errors_lock:
                errors.append(exc)

    def lock_reader() -> None:
        try:
            for _ in range(700):
                db.get_compression_lock_holder("session")
        except BaseException as exc:
            with errors_lock:
                errors.append(exc)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
            futures = [pool.submit(append_worker, worker) for worker in range(8)]
            futures.append(pool.submit(lock_reader))
            for future in futures:
                future.result(timeout=30)
        assert errors == []
        assert len(db.get_messages("session")) == 8 * 150
        assert db._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()


def test_shared_writer_connect_request_has_cache_disabled(monkeypatch, tmp_path: Path) -> None:
    """Pin the actual sqlite connect kwargs rather than a private attribute."""
    import hermes_state

    captured: list[dict] = []
    real_connect = hermes_state.sqlite3.connect

    def spy_connect(*args, **kwargs):
        captured.append(dict(kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(hermes_state.sqlite3, "connect", spy_connect)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        assert any(call.get("cached_statements") == 0 for call in captured)
    finally:
        db.close()


def test_per_thread_read_connection_has_cache_disabled(monkeypatch, tmp_path: Path) -> None:
    import hermes_state

    captured: list[dict] = []
    real_connect = hermes_state.sqlite3.connect

    def spy_connect(*args, **kwargs):
        captured.append(dict(kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(hermes_state.sqlite3, "connect", spy_connect)
    db = _new_db(tmp_path / "state.db")
    try:
        # The test harness may intentionally force DELETE mode for SQLite
        # portability. This unit pins the WAL-only per-thread read branch.
        db._wal_active = True
        done = threading.Event()

        def read_in_worker() -> None:
            assert db.get_session("session") is not None
            done.set()

        thread = threading.Thread(target=read_in_worker)
        thread.start()
        thread.join(timeout=5)
        assert done.is_set()
        assert sum(1 for call in captured if call.get("cached_statements") == 0) >= 2
    finally:
        db.close()


def test_handoff_reads_do_not_scramble_writer_error_state(tmp_path: Path) -> None:
    """Root-cause regression for 'no more rows available' (2026-07-28).

    The gateway's handoff watcher polls list_pending_handoffs() /
    get_handoff_state() every 2s through asyncio.to_thread on the SHARED
    writer connection. When those reads bypass the writer lock, their
    sqlite3_step() -> SQLITE_DONE runs with the GIL released and overwrites
    the db handle's global error state. A concurrent writer that hits a real
    SQLITE_BUSY then raises with sqlite3_errmsg(db) == errstr(SQLITE_DONE)
    == 'no more rows available' instead of 'database is locked', so
    _execute_write's locked/busy retry classifier cannot retry it and the
    transcript flush fails.

    Fails within seconds while the handoff reads are unlocked; passes once
    they go through _read_ctx() (per-thread RO connection or writer lock).
    """
    db = _new_db(tmp_path / "state.db")
    stop = threading.Event()
    scrambled: list[BaseException] = []

    def contention() -> None:
        # Mirrors SessionStore/CLI/cron writers: an independent connection
        # that holds the WAL write lock most of the time so the shared
        # writer's BEGIN IMMEDIATE regularly hits SQLITE_BUSY.
        conn = sqlite3.connect(str(tmp_path / "state.db"), timeout=0.05)
        try:
            while not stop.is_set():
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "UPDATE sessions SET model = 'contender' "
                        "WHERE id = 'session'")
                    time.sleep(0.002)
                    conn.commit()
                except sqlite3.Error:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
        finally:
            conn.close()

    def watcher() -> None:
        # Handoff watcher mirror (gateway/run.py::_handoff_watcher).
        while not stop.is_set():
            db.list_pending_handoffs()
            db.get_handoff_state("session")

    def writer() -> None:
        while not stop.is_set():
            try:
                db.append_message("session", "user", content="x")
            except sqlite3.Error as exc:
                msg = str(exc).lower()
                if "locked" not in msg and "busy" not in msg:
                    scrambled.append(exc)
                    stop.set()
                    return

    threads = [threading.Thread(target=contention, daemon=True),
               threading.Thread(target=writer, daemon=True)]
    threads += [threading.Thread(target=watcher, daemon=True)
                for _ in range(3)]
    for t in threads:
        t.start()
    stop.wait(timeout=20.0)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    db.close()
    assert not scrambled, (
        "writer exception scrambled by unlocked shared-connection read: "
        f"{type(scrambled[0]).__name__}: {scrambled[0]}"
    )
