"""Read-only PostgreSQL opens must not be able to change the store.

A read-only open serves the dashboard's status/session listing, cron history,
usage analytics, and resume lookup. Review finding at head `cac143fa40`:

    Removing `read_only` as a backend selector fixes the split-store read.
    However, `maybe_open_postgres(read_only=True, ...)` still unconditionally
    calls `init_postgres_schema()`. A status/resume/analytics reader can
    therefore create or reconcile schema through a path presented as
    read-only, and the returned Postgres handle has no engine- or
    adapter-enforced write prohibition.

Three invariants follow, and each is pinned here:

  1. a read-only open runs NO DDL — provisioning belongs to writable opens;
  2. a read-only open against an absent or behind-this-build schema FAILS
     rather than mutating it or serving a store it cannot correctly read;
  3. the returned handle carries an engine-enforced write prohibition.

Invariant 4 is the previous round's fix, guarded here against regression:
`read_only` must still resolve the PostgreSQL backend, never fall back to
SQLite.
"""

from __future__ import annotations

import pytest


class _FakeCursor:
    def __init__(self, conn, rows=None):
        self._conn = conn
        self._rows = rows if rows is not None else []

    def execute(self, sql, params=()):
        self._conn.executed.append(sql.strip())
        return self

    def executescript(self, sql):
        self._conn.executed.append("SCRIPT:" + sql.strip()[:40])
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        return None


class _FakeConn:
    """Minimal connection double that records every statement it is given."""

    def __init__(self, *, has_sessions=True, version=None):
        self.executed: list[str] = []
        self.commits = 0
        self._has_sessions = has_sessions
        self._version = version

    def execute(self, sql, params=()):
        self.executed.append(sql.strip())
        low = sql.lower()
        if "information_schema.tables" in low:
            return _FakeCursor(self, [(1,)] if self._has_sessions else [])
        return _FakeCursor(self)

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        return None


def _expected_version():
    from hermes_state_postgres import _PG_ONLY_MIGRATIONS

    return max((m.version for m in _PG_ONLY_MIGRATIONS), default=0)


# ---------------------------------------------------------------------------
# 1. A read-only open runs no DDL
# ---------------------------------------------------------------------------


class TestReadOnlyOpenRunsNoDDL:
    def test_read_only_does_not_call_schema_init(self, monkeypatch):
        """Provisioning through a path presented as read-only is the bug."""
        import hermes_state_postgres as hsp

        conn = _FakeConn(has_sessions=True)
        called: list[str] = []

        monkeypatch.setattr(hsp, "connect_postgres", lambda dsn: conn)
        monkeypatch.setattr(
            hsp, "init_postgres_schema",
            lambda c, v: called.append("init"),
        )
        monkeypatch.setattr(
            hsp, "postgres_migration_version", lambda c: _expected_version()
        )

        hsp.maybe_open_postgres(True, 1, dsn_override="postgresql://h/db")

        assert called == [], (
            "a read-only open ran init_postgres_schema; a status/analytics "
            "reader must not be able to create or reconcile schema"
        )

    def test_writable_open_still_initialises_schema(self, monkeypatch):
        """The owner path must keep provisioning — don't over-correct."""
        import hermes_state_postgres as hsp

        conn = _FakeConn(has_sessions=True)
        called: list[str] = []

        monkeypatch.setattr(hsp, "connect_postgres", lambda dsn: conn)
        monkeypatch.setattr(
            hsp, "init_postgres_schema",
            lambda c, v: called.append("init"),
        )

        hsp.maybe_open_postgres(False, 1, dsn_override="postgresql://h/db")

        assert called == ["init"], "writable opens must still provision schema"

    def test_read_only_issues_no_ddl_statements(self, monkeypatch):
        """Belt and braces: inspect what actually reached the connection."""
        import hermes_state_postgres as hsp

        conn = _FakeConn(has_sessions=True)
        monkeypatch.setattr(hsp, "connect_postgres", lambda dsn: conn)
        monkeypatch.setattr(
            hsp, "postgres_migration_version", lambda c: _expected_version()
        )

        hsp.maybe_open_postgres(True, 1, dsn_override="postgresql://h/db")

        ddl = [
            s for s in conn.executed
            if s.upper().startswith(("CREATE", "ALTER", "DROP", "INSERT",
                                     "UPDATE", "DELETE", "SCRIPT:"))
        ]
        assert ddl == [], f"read-only open issued mutating statements: {ddl}"


# ---------------------------------------------------------------------------
# 2. Fail closed on an unusable store
# ---------------------------------------------------------------------------


class TestReadOnlyFailsClosed:
    def test_absent_schema_raises_instead_of_provisioning(self, monkeypatch):
        import hermes_state_postgres as hsp

        conn = _FakeConn(has_sessions=False)
        monkeypatch.setattr(hsp, "connect_postgres", lambda dsn: conn)

        with pytest.raises(RuntimeError, match="no Hermes schema found"):
            hsp.maybe_open_postgres(True, 1, dsn_override="postgresql://h/db")

    def test_schema_behind_this_build_raises(self, monkeypatch):
        import hermes_state_postgres as hsp

        conn = _FakeConn(has_sessions=True)
        monkeypatch.setattr(hsp, "connect_postgres", lambda dsn: conn)
        monkeypatch.setattr(
            hsp, "postgres_migration_version",
            lambda c: _expected_version() - 1,
        )

        with pytest.raises(RuntimeError, match="migration version"):
            hsp.maybe_open_postgres(True, 1, dsn_override="postgresql://h/db")

    def test_schema_ahead_of_this_build_is_allowed(self, monkeypatch):
        """A newer store still satisfies an older reader's queries.

        Refusing it would break every mixed-version deployment mid-rollout.
        The schema only ever grows, so 'ahead' is safe; only 'behind' is not.
        """
        import hermes_state_postgres as hsp

        conn = _FakeConn(has_sessions=True)
        monkeypatch.setattr(hsp, "connect_postgres", lambda dsn: conn)
        monkeypatch.setattr(
            hsp, "postgres_migration_version",
            lambda c: _expected_version() + 5,
        )

        assert hsp.maybe_open_postgres(
            True, 1, dsn_override="postgresql://h/db"
        ) is conn

    def test_error_message_does_not_leak_the_password(self, monkeypatch):
        """DSNs carry credentials; a refusal message must not print them."""
        import hermes_state_postgres as hsp

        conn = _FakeConn(has_sessions=False)
        monkeypatch.setattr(hsp, "connect_postgres", lambda dsn: conn)
        secret = "hunter2seekrit"

        with pytest.raises(RuntimeError) as excinfo:
            hsp.maybe_open_postgres(
                True, 1,
                dsn_override=f"postgresql://user:{secret}@host:5432/db?sslmode=require",
            )

        assert secret not in str(excinfo.value)
        assert "host:5432/db" in str(excinfo.value), (
            "the message should still identify WHICH store was refused"
        )


# ---------------------------------------------------------------------------
# 3. Enforced write prohibition
# ---------------------------------------------------------------------------


class TestReadOnlyWriteProhibition:
    def test_session_is_set_read_only_before_anything_else(self, monkeypatch):
        """The prohibition must be in force before any other statement runs.

        Engine-level (`default_transaction_read_only`) rather than an
        adapter-side SQL classifier: a parser has permanent false-negative
        holes and protects nothing against code reaching the raw connection.
        """
        import hermes_state_postgres as hsp

        conn = _FakeConn(has_sessions=True)
        monkeypatch.setattr(hsp, "connect_postgres", lambda dsn: conn)
        monkeypatch.setattr(
            hsp, "postgres_migration_version", lambda c: _expected_version()
        )

        hsp.maybe_open_postgres(True, 1, dsn_override="postgresql://h/db")

        assert conn.executed, "no statements were issued at all"
        first = conn.executed[0].lower()
        assert "default_transaction_read_only" in first and " on" in first, (
            f"the read-only prohibition was not the first statement; got: "
            f"{conn.executed[0]!r}"
        )

    def test_writable_open_does_not_set_read_only(self, monkeypatch):
        import hermes_state_postgres as hsp

        conn = _FakeConn(has_sessions=True)
        monkeypatch.setattr(hsp, "connect_postgres", lambda dsn: conn)
        monkeypatch.setattr(hsp, "init_postgres_schema", lambda c, v: None)

        hsp.maybe_open_postgres(False, 1, dsn_override="postgresql://h/db")

        assert not any(
            "default_transaction_read_only" in s.lower() for s in conn.executed
        ), "a writable open must not put the session into read-only mode"


# ---------------------------------------------------------------------------
# 4. Regression guard for the PREVIOUS round's fix
# ---------------------------------------------------------------------------


class TestReadOnlyStillSelectsPostgres:
    def test_read_only_does_not_fall_back_to_sqlite(self, monkeypatch):
        """read_only must never be a backend selector again.

        Gating the backend on it sent every dashboard reader to the local
        state.db while writes went to PostgreSQL.
        """
        import hermes_state_postgres as hsp

        conn = _FakeConn(has_sessions=True)
        monkeypatch.setattr(hsp, "connect_postgres", lambda dsn: conn)
        monkeypatch.setattr(
            hsp, "postgres_migration_version", lambda c: _expected_version()
        )

        assert hsp.maybe_open_postgres(
            True, 1, dsn_override="postgresql://h/db"
        ) is conn, "read_only fell back to SQLite (returned None)"
