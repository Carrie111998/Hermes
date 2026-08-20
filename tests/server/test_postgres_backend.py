"""Postgres parity contracts that don't need a live Postgres.

The Postgres backend is the production one but the suite runs on SQLite, so the
two schemas drift silently: a table added to ``server/db.py`` reaches production
only if someone also writes the migration, wires it into
``REQUIRED_MIGRATIONS``, and gives it an RLS policy. These tests assert exactly
that, plus the ``?`` → ``$n`` translation the repository SQL depends on.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from server.document_artifacts import _ARTIFACT_COLUMNS, _ATTEMPT_COLUMNS
from server.postgres import PostgresDatabase, _TransactionProxy, _returns_rows, _sql

MIGRATIONS = Path(__file__).resolve().parents[2] / "server" / "supabase" / "migrations"
MIGRATION_007 = (MIGRATIONS / "007_document_artifacts.sql").read_text(encoding="utf-8")


def test_every_migration_file_is_required_at_startup():
    """A migration nobody requires is a migration production quietly skips."""
    on_disk = {
        path.stem
        for path in MIGRATIONS.glob("*.sql")
    }
    missing = on_disk - set(PostgresDatabase.REQUIRED_MIGRATIONS)
    assert not missing, f"migrations exist but are never required: {sorted(missing)}"


def test_each_migration_records_itself_in_the_ledger():
    for path in MIGRATIONS.glob("*.sql"):
        body = path.read_text(encoding="utf-8")
        assert f"'{path.stem}'" in body, f"{path.name} never inserts its own version"
        assert "schema_migrations" in body


def test_document_tables_are_declared_with_postgres_types():
    assert "content bytea not null" in MIGRATION_007
    assert "metadata jsonb not null" in MIGRATION_007
    assert "size_bytes bigint not null" in MIGRATION_007


def test_document_tables_have_tenant_row_level_security():
    assert "'document_artifacts','document_processing_attempts'" in MIGRATION_007
    assert "interfaze_company_access(company_id)" in MIGRATION_007
    assert "enable row level security" in MIGRATION_007


def test_document_indexes_match_the_sqlite_schema():
    from server.db import SCHEMA

    for index in ("ix_document_artifacts_scope", "ix_document_attempts_scope"):
        assert index in SCHEMA, f"{index} missing from SQLite schema"
        assert index in MIGRATION_007, f"{index} missing from the Postgres migration"


def test_additive_document_columns_exist_in_both_backends():
    from server.db import COLUMN_MIGRATIONS

    document_columns = [
        column for table, column, _ in COLUMN_MIGRATIONS if table == "documents"
    ]
    assert document_columns, "expected additive document columns"
    for column in document_columns:
        assert f"add column if not exists {column}" in MIGRATION_007, column


def test_public_status_vocabulary_is_identical_in_both_backends():
    from server.document_artifacts import PUBLIC_STATUSES

    for status in PUBLIC_STATUSES:
        assert f"'{status}'" in MIGRATION_007, status


def test_placeholder_translation_numbers_every_parameter():
    sql = f"INSERT INTO document_artifacts({_ARTIFACT_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
    translated = _sql(sql)
    assert "?" not in translated
    assert re.findall(r"\$\d+", translated) == [f"${i}" for i in range(1, 14)]


def test_repository_column_lists_are_explicit_and_aligned():
    """Explicit column lists, so a new column can't shift a positional INSERT."""
    for columns in (_ARTIFACT_COLUMNS, _ATTEMPT_COLUMNS):
        names = [part.strip() for part in columns.split(",")]
        assert len(names) == 13
        assert names[:3] == ["id", "document_id", "company_id"]
        assert all(name.isidentifier() for name in names)


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT 1 WHERE a=? AND b=?", "SELECT 1 WHERE a=$1 AND b=$2"),
        ("UPDATE t SET a=? WHERE id=? AND company_id=?",
         "UPDATE t SET a=$1 WHERE id=$2 AND company_id=$3"),
    ],
)
def test_sql_translation_examples(sql, expected):
    assert _sql(sql) == expected


# provision_demo_account SELECTs inside db.transaction(), and the proxy used to
# hand back asyncpg's status string ("SELECT 1"), so .fetchone() raised
# AttributeError on Postgres while passing on SQLite.
class _FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.fetched: list[tuple] = []
        self.executed: list[tuple] = []

    async def fetch(self, sql, *params):
        self.fetched.append((sql, params))
        return self.rows

    async def execute(self, sql, *params):
        self.executed.append((sql, params))
        return "INSERT 0 1"


class _FakeDb:
    @staticmethod
    def _run(awaitable):
        return asyncio.run(awaitable)


def _proxy(rows):
    proxy = _TransactionProxy(_FakeDb())
    proxy.conn = _FakeConn(rows)
    return proxy


def test_transaction_select_answers_the_cursor_api():
    proxy = _proxy([{"id": "usr_1", "company_id": None}])
    result = proxy.execute(
        "SELECT id,company_id FROM users WHERE lower(email)=lower(?)", ("a@b.test",)
    )
    assert result.fetchone() == {"id": "usr_1", "company_id": None}
    assert proxy.conn.fetched[0][0].endswith("lower($1)"), "? was not translated"
    assert not proxy.conn.executed, "a SELECT must not go through execute()"


def test_transaction_select_with_no_rows_returns_none():
    assert _proxy([]).execute("SELECT 1 FROM leads WHERE company_id=?", ("c",)).fetchone() is None


def test_transaction_write_stays_on_execute():
    proxy = _proxy([])
    result = proxy.execute("INSERT INTO companies(id) VALUES(?)", ("cmp_1",))
    assert result.fetchone() is None
    assert proxy.conn.executed and not proxy.conn.fetched


@pytest.mark.parametrize("sql,expected", [
    ("SELECT 1", True),
    ("  select id from users", True),
    ("WITH x AS (SELECT 1) SELECT * FROM x", True),
    ("INSERT INTO t(id) VALUES(?) RETURNING id", True),
    ("INSERT INTO t(id) VALUES(?)", False),
    ("UPDATE t SET a=?", False),
    ("DELETE FROM t WHERE id=?", False),
])
def test_row_returning_classification(sql, expected):
    assert _returns_rows(sql) is expected
