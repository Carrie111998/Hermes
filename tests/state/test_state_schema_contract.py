"""Contract-diff primitives behind the OOF-76 stale-schema detection.

``state_schema_mismatches`` is what lets read paths and readiness probes
distinguish "this store is behind the dashboard's read surface" from "this
store legitimately reports an older application ``schema_version``" — the
two were conflated in the original incident (FTS5-unavailable runtimes pin
the version integer below ``SCHEMA_VERSION`` by design even when every
regular table has converged).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from hermes_state_common import (
    SCHEMA_SQL,
    _state_schema_contract,
    state_schema_mismatches,
)


@pytest.fixture()
def store(tmp_path):
    """A schema-converged store built straight from SCHEMA_SQL."""
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA_SQL)
    return path


def _connect(path):
    return closing(sqlite3.connect(path))


class TestStateSchemaContract:
    def test_contract_contains_core_tables_and_columns(self):
        contract = _state_schema_contract()
        assert "sessions" in contract
        assert "messages" in contract
        assert "system_prompts" in contract
        # The exact read-surface columns the OOF-76 incident 500'd on.
        assert {"archived", "pinned", "last_activity_at", "system_prompt_hash"} <= (
            contract["sessions"]
        )
        assert {"active", "compacted"} <= contract["messages"]

    def test_contract_excludes_fts_virtual_tables(self):
        # FTS layout is capability-dependent (fts_storage_version) and
        # managed separately — requiring it here would flag every
        # FTS5-unavailable runtime as permanently stale.
        contract = _state_schema_contract()
        assert not any("fts" in table for table in contract)

    def test_contract_is_cached(self):
        assert _state_schema_contract() is _state_schema_contract()


class TestStateSchemaMismatches:
    def test_converged_store_has_no_mismatches(self, store):
        with _connect(store) as conn:
            assert state_schema_mismatches(conn) == []

    def test_lagging_schema_version_integer_is_not_drift(self, store):
        # The application version integer sitting behind SCHEMA_VERSION is
        # legitimate (FTS5-unavailable runtimes) — only the table contract
        # matters. This is the exact false-positive OOF-76's fix must avoid.
        with _connect(store) as conn:
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (1)")
            conn.commit()
            assert conn.execute("SELECT version FROM schema_version").fetchone() == (1,)
            assert state_schema_mismatches(conn) == []

    def test_missing_column_is_reported(self, store):
        with _connect(store) as conn:
            conn.execute("ALTER TABLE sessions DROP COLUMN archived")
            conn.commit()
            mismatches = state_schema_mismatches(conn)
        assert mismatches == ["table sessions missing columns: archived"]

    def test_missing_table_is_reported(self, store):
        with _connect(store) as conn:
            conn.execute("DROP TABLE system_prompts")
            conn.commit()
            mismatches = state_schema_mismatches(conn)
        assert "missing table system_prompts" in mismatches

    def test_extra_tables_and_columns_are_tolerated(self, store):
        # Reconcile only ever ADDs; newer stores read by older code, or
        # user-created side tables, must not be flagged.
        with _connect(store) as conn:
            conn.execute("CREATE TABLE user_side_table (id INTEGER)")
            conn.execute("ALTER TABLE sessions ADD COLUMN future_col TEXT")
            conn.commit()
            assert state_schema_mismatches(conn) == []

    def test_empty_database_is_uninitialised_not_stale(self, tmp_path):
        path = tmp_path / "fresh.db"
        with _connect(path) as conn:
            assert state_schema_mismatches(conn) == []

    def test_foreign_only_database_is_drift(self, tmp_path):
        # Once ANY table exists the full contract is required: a database
        # holding only unrelated tables is exactly the failure to surface.
        path = tmp_path / "foreign.db"
        with _connect(path) as conn:
            conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
            conn.commit()
            mismatches = state_schema_mismatches(conn)
        assert mismatches
        assert any(m == "missing table sessions" for m in mismatches)

    def test_multiple_missing_columns_sorted_and_grouped(self, store):
        with _connect(store) as conn:
            conn.execute("ALTER TABLE sessions DROP COLUMN pinned")
            conn.execute("ALTER TABLE sessions DROP COLUMN archived")
            conn.commit()
            mismatches = state_schema_mismatches(conn)
        assert mismatches == ["table sessions missing columns: archived, pinned"]
