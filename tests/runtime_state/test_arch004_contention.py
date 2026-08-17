"""ARCH-004 real-process WAL contention and process-death contracts."""

from __future__ import annotations

from multiprocessing import Event, Process
import sqlite3
from types import SimpleNamespace
import time

from runtime_state import (
    RETRY_EXHAUSTED,
    RuntimeStateDB,
    RetryConfig,
    create_session_state,
)
from runtime_state.journal import JournalWriteError, run_journaled_mutation


def _fast_retry_config() -> RetryConfig:
    return RetryConfig(
        busy_timeout_ms=1,
        max_retries=2,
        base_delay_ms=0,
        delay_cap_ms=0,
        jitter_min_ms=0,
        jitter_max_ms=0,
    )


def _hold_sqlite_writer(path_string: str, ready: Event, release: Event) -> None:
    conn = sqlite3.connect(path_string, isolation_level=None, timeout=0.01)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=1")
        conn.execute("BEGIN IMMEDIATE")
        ready.set()
        release.wait(10)
        conn.rollback()
    finally:
        conn.close()


def _mutate_status(conn, status: str):
    conn.execute(
        "UPDATE session_state SET status=? WHERE profile_name='p' AND session_id='s'",
        (status,),
    )
    return SimpleNamespace(success=True)


def test_real_process_wal_holder_exhausts_bounded_retry(tmp_path):
    path = tmp_path / "state.db"
    with RuntimeStateDB(path, retry_config=_fast_retry_config()) as db:
        assert create_session_state(db.connection, "p", "s", user_id="u", workspace="w").success
        ready, release = Event(), Event()
        holder = Process(target=_hold_sqlite_writer, args=(str(path), ready, release))
        holder.start()
        try:
            assert ready.wait(5)
            started = time.monotonic()
            try:
                run_journaled_mutation(
                    db.connection,
                    "session_state",
                    "p",
                    "s",
                    "update_columns",
                    lambda: _mutate_status(db.connection, "degraded"),
                )
            except JournalWriteError as exc:
                assert exc.code == RETRY_EXHAUSTED
            else:
                raise AssertionError("SQLite holder did not block the mutation")
            assert time.monotonic() - started < 2
            assert db.connection.execute(
                "SELECT status FROM session_state WHERE profile_name='p' AND session_id='s'"
            ).fetchone()[0] == "active"
        finally:
            release.set()
            holder.join(5)
            if holder.is_alive():
                holder.terminate()
                holder.join(5)
            assert not holder.is_alive()


def test_process_death_releases_wal_writer(tmp_path):
    path = tmp_path / "state.db"
    with RuntimeStateDB(path, retry_config=_fast_retry_config()) as db:
        assert create_session_state(db.connection, "p", "s", user_id="u", workspace="w").success
        ready, release = Event(), Event()
        holder = Process(target=_hold_sqlite_writer, args=(str(path), ready, release))
        holder.start()
        try:
            assert ready.wait(5)
            holder.terminate()
            holder.join(5)
            assert not holder.is_alive()
            result = run_journaled_mutation(
                db.connection,
                "session_state",
                "p",
                "s",
                "update_columns",
                lambda: _mutate_status(db.connection, "degraded"),
            )
            assert result.success
            assert db.connection.execute(
                "SELECT status FROM session_state WHERE profile_name='p' AND session_id='s'"
            ).fetchone()[0] == "degraded"
        finally:
            if holder.is_alive():
                release.set()
                holder.terminate()
                holder.join(5)
