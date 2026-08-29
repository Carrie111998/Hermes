"""Retry of transient SQLite engine errors (#74934 port).

Under dual gateway/agent WAL contention (FTS5 trigram sync holding the
write lock on large appends), the SQLite engine can raise a transient
'no more rows available' error. The exception CLASS varies with the
SQLite build — some surface it as ``sqlite3.InterfaceError``, which is a
sibling of ``DatabaseError`` (not a subclass) and therefore escaped both
existing retry branches in ``_execute_write`` on attempt 0, killing the
turn as ``session_persistence_failed`` while the identical write would
have succeeded milliseconds later.

The fix is message-scoped, not class-scoped: any ``sqlite3.Error`` whose
text contains 'no more rows available' retries within the existing
deadline/patience loop; every other error propagates untouched.

The same write boundary retries CPython's exact
``SystemError: returned NULL without setting an exception`` only after the
current SQLite transaction is known clean.
"""

import logging
import sqlite3

import pytest

from hermes_state import SessionDB

SQLITE_NULL_SYSTEM_ERROR = "returned NULL without setting an exception"


@pytest.fixture
def db(tmp_path, monkeypatch):
    # Keep retries fast: tiny jitter, short-but-sufficient patience.
    monkeypatch.setattr(SessionDB, "_WRITE_PATIENCE_S", 2.0)
    monkeypatch.setattr(SessionDB, "_WRITE_RETRY_MIN_S", 0.001)
    monkeypatch.setattr(SessionDB, "_WRITE_RETRY_MAX_S", 0.005)
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    d.close()


class TestNoMoreRowsRetry:
    def test_transient_interface_error_is_retried_to_success(self, db):
        """InterfaceError('no more rows available') must be retried inside
        the deadline/patience loop and succeed once the contention clears."""
        calls = {"n": 0}

        def flaky(conn):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise sqlite3.InterfaceError("no more rows available")
            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES ('nmr', 'ok') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            return "done"

        assert db._execute_write(flaky) == "done"
        assert calls["n"] == 4
        assert db.get_meta("nmr") == "ok"

    def test_unrelated_interface_error_propagates_immediately(self, db):
        """The catch-all is message-scoped: an InterfaceError with any other
        text must escape on the first attempt, not be swallowed/retried."""
        calls = {"n": 0}

        def broken(conn):
            calls["n"] += 1
            raise sqlite3.InterfaceError("bad parameter or other API misuse")

        with pytest.raises(sqlite3.InterfaceError, match="bad parameter"):
            db._execute_write(broken)
        assert calls["n"] == 1

    def test_no_more_rows_via_database_error_is_retried(self, db):
        """Some builds raise the same transient message through the generic
        DatabaseError class — it must ride the same retry loop instead of
        being misrouted into the FTS-corruption rebuild path."""
        calls = {"n": 0}

        def flaky(conn):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise sqlite3.DatabaseError("no more rows available")
            return "ok"

        assert db._execute_write(flaky) == "ok"
        assert calls["n"] == 3

    def test_exhausted_patience_propagates_the_transient_error(self, db, monkeypatch):
        """If contention never clears within the patience budget, the
        original error must surface rather than looping forever."""
        monkeypatch.setattr(SessionDB, "_WRITE_PATIENCE_S", 0.05)

        def always(conn):
            raise sqlite3.InterfaceError("no more rows available")

        with pytest.raises(sqlite3.InterfaceError, match="no more rows"):
            db._execute_write(always)


class _ConnectionProxy:
    def __init__(self, conn):
        self._conn = conn

    @property
    def in_transaction(self):
        return self._conn.in_transaction

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestSqliteNullSystemErrorRetry:
    def test_transient_exact_system_error_is_retried_to_success(
        self, db, caplog
    ):
        calls = {"n": 0}

        def flaky(conn):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise SystemError(SQLITE_NULL_SYSTEM_ERROR)
            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES ('sqlite_null', 'ok') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            return "done"

        with caplog.at_level(logging.INFO, logger="hermes_state"):
            assert (
                db._execute_write(
                    flaky,
                    operation="test_sqlite_null",
                    session_id="s-null",
                )
                == "done"
            )

        assert calls["n"] == 4
        assert db.get_meta("sqlite_null") == "ok"
        recovery_events = [
            rec for rec in caplog.records
            if "sqlite_null_system_error recovered:" in rec.message
        ]
        assert len(recovery_events) == 1
        assert "operation=test_sqlite_null" in recovery_events[0].message
        assert "session=s-null" in recovery_events[0].message
        assert "attempts=3" in recovery_events[0].message
        assert "rollback_safety=rolled_back" in recovery_events[0].message

    def test_partial_transaction_is_rolled_back_before_retry(self, db):
        calls = {"n": 0}

        def flaky_after_mutation(conn):
            calls["n"] += 1
            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES ('sqlite_null_once', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"attempt-{calls['n']}",),
            )
            if calls["n"] == 1:
                raise SystemError(SQLITE_NULL_SYSTEM_ERROR)
            return "ok"

        assert db._execute_write(flaky_after_mutation) == "ok"
        assert calls["n"] == 2
        assert db.get_meta("sqlite_null_once") == "attempt-2"

    def test_rollback_failure_fails_closed_without_retry(self, db, monkeypatch):
        calls = {"n": 0}
        sleeps = []
        original_conn = db._conn

        class RollbackFailingConnection(_ConnectionProxy):
            def rollback(self):
                raise RuntimeError("rollback failed")

        monkeypatch.setattr(
            db,
            "_sleep_before_write_retry",
            lambda *args, **kwargs: sleeps.append(True) or True,
        )
        db._conn = RollbackFailingConnection(original_conn)

        def broken(conn):
            calls["n"] += 1
            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES ('sqlite_null_bad', 'x')"
            )
            raise SystemError(SQLITE_NULL_SYSTEM_ERROR)

        try:
            with pytest.raises(SystemError, match=SQLITE_NULL_SYSTEM_ERROR):
                db._execute_write(broken)
        finally:
            db._conn = original_conn
            if original_conn.in_transaction:
                original_conn.rollback()

        assert calls["n"] == 1
        assert sleeps == []
        assert db.get_meta("sqlite_null_bad") is None

    def test_commit_stage_system_error_fails_closed_without_retry(
        self, db, monkeypatch
    ):
        calls = {"n": 0}
        sleeps = []
        original_conn = db._conn

        class CommitFailingConnection(_ConnectionProxy):
            def commit(self):
                raise SystemError(SQLITE_NULL_SYSTEM_ERROR)

        monkeypatch.setattr(
            db,
            "_sleep_before_write_retry",
            lambda *args, **kwargs: sleeps.append(True) or True,
        )
        db._conn = CommitFailingConnection(original_conn)

        def write_once(conn):
            calls["n"] += 1
            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES ('sqlite_null_commit', 'x')"
            )

        try:
            with pytest.raises(SystemError, match=SQLITE_NULL_SYSTEM_ERROR):
                db._execute_write(write_once)
        finally:
            db._conn = original_conn
            if original_conn.in_transaction:
                original_conn.rollback()

        assert calls["n"] == 1
        assert sleeps == []
        assert db.get_meta("sqlite_null_commit") is None

    def test_unrelated_system_error_propagates_immediately(self, db):
        calls = {"n": 0}

        def broken(conn):
            calls["n"] += 1
            raise SystemError("unrelated C extension failure")

        with pytest.raises(SystemError, match="unrelated C extension"):
            db._execute_write(broken)
        assert calls["n"] == 1

    def test_exhausted_patience_propagates_original_system_error(
        self, db, caplog
    ):
        calls = {"n": 0}

        def always(conn):
            calls["n"] += 1
            raise SystemError(SQLITE_NULL_SYSTEM_ERROR)

        with caplog.at_level(logging.ERROR, logger="hermes_state"):
            with pytest.raises(SystemError, match=SQLITE_NULL_SYSTEM_ERROR):
                db._execute_write(
                    always,
                    patience_s=0.01,
                    operation="test_sqlite_null_exhaustion",
                    session_id="s-null-exhausted",
                )

        assert calls["n"] >= 1
        exhausted_events = [
            rec for rec in caplog.records
            if "sqlite_null_system_error exhausted:" in rec.message
        ]
        assert len(exhausted_events) == 1
        assert "operation=test_sqlite_null_exhaustion" in exhausted_events[0].message
        assert "session=s-null-exhausted" in exhausted_events[0].message
        assert "rollback_safety=rolled_back" in exhausted_events[0].message
