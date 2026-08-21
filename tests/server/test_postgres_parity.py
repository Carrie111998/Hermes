"""Schema and dialect parity between the two backends, without a live Postgres.

`server/db.py` is applied on boot, so anything added there works immediately in
every test. Postgres only ever gets what a migration file writes. The two drift
silently and in one direction: the suite is green, production raises.

That is not hypothetical. When this file was written the SQLite schema had two
tables no migration created — `suppressions`, read by the compliance check
before a send, and `daily_digests`, read and written by the digest scheduler —
so both features raised `relation does not exist` on the production backend
while every test passed. It also had an index that existed only in SQLite, added
specifically to stop a per-run table scan that therefore still happened in
production. And `digest.py` used `INSERT OR IGNORE`, which Postgres does not
have.

These tests compare the schemas as text rather than by connecting, so they run
everywhere and fail on the commit that introduces the drift.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from server.db import SCHEMA
from server.postgres import PostgresDatabase, _returns_rows, _sql

MIGRATIONS = Path(__file__).resolve().parents[2] / "server" / "supabase" / "migrations"


def _migration_sql() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS.glob("*.sql"))
    ).lower()


def _sqlite_tables() -> set[str]:
    return set(re.findall(r"create\s+table\s+if\s+not\s+exists\s+(\w+)", SCHEMA, re.I))


def _sqlite_indexes() -> set[str]:
    return set(
        re.findall(r"create\s+(?:unique\s+)?index\s+if\s+not\s+exists\s+(\w+)", SCHEMA, re.I)
    )


# ── schema parity ─────────────────────────────────────────────────────────────

def test_every_sqlite_table_has_a_migration():
    """A table only SQLite knows about is a feature that raises in production."""
    sql = _migration_sql()
    missing = sorted(
        table for table in _sqlite_tables()
        if not re.search(rf"create\s+table\s+if\s+not\s+exists\s+{table}\b", sql)
    )

    assert not missing, (
        f"tables exist in server/db.py but in no migration: {missing}. "
        "Add them to server/supabase/migrations/ with an RLS policy."
    )


def test_every_sqlite_index_has_a_migration():
    """An index only SQLite has is a scan that only production performs."""
    sql = _migration_sql()
    missing = sorted(name for name in _sqlite_indexes() if name.lower() not in sql)

    assert not missing, (
        f"indexes exist in server/db.py but in no migration: {missing}. "
        "The query they were added for still scans on Postgres."
    )


def _rls_enabled(name: str, body: str) -> bool:
    """Whether any migration turns RLS on for this table.

    Two forms are in use and both count. 004 writes `alter table x enable row
    level security` per table; 007 loops `format()` over an array of names.
    Detecting only the first reports most of the schema as unprotected, which is
    a false alarm worth not raising.
    """
    if f"alter table {name} enable row level security" in body:
        return True
    for names, loop_body in re.findall(r"array\[(.*?)\]\s*loop(.*?)end loop", body, re.S):
        if f"'{name}'" in names and "enable row level security" in loop_body:
            return True
    return False


def test_every_tenant_table_in_a_migration_has_row_level_security():
    """A tenant table without RLS is cross-tenant readable, and silently so.

    RLS may be granted by a later migration than the one that creates the table
    — 004 and 005 exist to do exactly that — so this reads all of them together.
    """
    body = _migration_sql()
    tenant = {
        name for name, columns in
        re.findall(r"create table if not exists (\w+)\s*\(([^;]*?)\)\s*;", body, re.S)
        if "company_id" in columns and name != "schema_migrations"
    }
    unprotected = sorted(name for name in tenant if not _rls_enabled(name, body))

    assert tenant, "the tenant-table scan found nothing, so it is not testing anything"
    assert not unprotected, f"tenant tables without row-level security: {unprotected}"


def test_every_migration_is_required_at_startup():
    """A migration nobody requires is a migration production quietly skips."""
    on_disk = {path.stem for path in MIGRATIONS.glob("*.sql")}

    assert not on_disk - set(PostgresDatabase.REQUIRED_MIGRATIONS)


# ── dialect parity ────────────────────────────────────────────────────────────

# Statements SQLite accepts and Postgres does not. Each one is a production
# failure that no test on SQLite can see.
SQLITE_ONLY = (
    "insert or ignore",
    "insert or replace",
    "autoincrement",
    "json_extract(",
    " glob ",
    "ifnull(",
    "group_concat(",
)

REPOSITORY_SOURCES = sorted(
    path for path in (Path(__file__).resolve().parents[2] / "server").rglob("*.py")
    # db.py owns the SQLite schema itself; postgres.py owns the translation.
    if path.name not in {"db.py", "postgres.py"}
)


@pytest.mark.parametrize("path", REPOSITORY_SOURCES, ids=lambda path: path.name)
def test_no_module_uses_sqlite_only_sql(path):
    """The whole product runs on Postgres in production."""
    body = path.read_text(encoding="utf-8")
    # Comments discussing portability are the point, not a violation.
    statements = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    ).lower()

    found = [needle.strip() for needle in SQLITE_ONLY if needle in statements]

    assert not found, f"{path.name} uses SQLite-only SQL: {found}"


# ── the placeholder translation every query depends on ────────────────────────

@pytest.mark.parametrize(("sql", "expected"), [
    ("SELECT 1 WHERE a=? AND b=?", "SELECT 1 WHERE a=$1 AND b=$2"),
    # The shapes added by evidence reuse and candidate selection.
    (
        "SELECT x FROM t WHERE company_id=? AND source_id IN (?,?) AND retrieved_at>=?",
        "SELECT x FROM t WHERE company_id=$1 AND source_id IN ($2,$3) AND retrieved_at>=$4",
    ),
    (
        "SELECT x FROM t WHERE country IN (?) AND ((dataset_id=? AND version=?) "
        "OR (dataset_id=? AND version=?))",
        "SELECT x FROM t WHERE country IN ($1) AND ((dataset_id=$2 AND version=$3) "
        "OR (dataset_id=$4 AND version=$5))",
    ),
])
def test_placeholder_translation_covers_the_shapes_in_use(sql, expected):
    translated = _sql(sql)

    assert translated == expected
    assert "?" not in translated
    # Numbered left to right with no gaps, which is what asyncpg binds against.
    assert [
        int(match) for match in re.findall(r"\$(\d+)", translated)
    ] == list(range(1, sql.count("?") + 1))


def test_upsert_translation_keeps_the_conflict_target():
    """`ON CONFLICT ... DO UPDATE` is the one upsert both backends share."""
    translated = _sql(
        "INSERT INTO t(a,b) VALUES(?,?) ON CONFLICT(a) DO UPDATE SET b=excluded.b"
    )

    assert translated.endswith("ON CONFLICT(a) DO UPDATE SET b=excluded.b")
    assert "VALUES($1,$2)" in translated


def test_a_literal_question_mark_would_be_rewritten():
    """A known limit of the translation, pinned so it is not discovered live.

    `_sql` rewrites every `?`, including one inside a string literal. No query in
    this repository contains one — `test_no_repository_sql_contains_a_literal_question_mark`
    keeps it that way — but the day one does, this is the failure to expect.
    """
    assert _sql("SELECT ? WHERE label='why?'") == "SELECT $1 WHERE label='why$2'"


# A line is SQL for this purpose if it carries a clause keyword. URLs with query
# strings and regexes containing `?` are everywhere and are not SQL.
SQL_LINE = re.compile(
    r"\b(select|insert into|update|delete from|where|values|set|join)\b", re.I
)


def test_no_repository_sql_contains_a_literal_question_mark():
    """The guard that keeps the limit above harmless.

    Only lines that look like SQL are examined: `?` in a URL query string or a
    regex is not a placeholder, and flagging those would make this test noise
    that gets deleted rather than a guard that gets kept.
    """
    offenders = []
    for path in REPOSITORY_SOURCES:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "?" not in stripped:
                continue
            if not SQL_LINE.search(stripped):
                continue
            # A placeholder sits next to punctuation or whitespace; a literal
            # inside a string sits next to a letter.
            if re.search(r"\?[A-Za-z]|[A-Za-z]\?", stripped):
                offenders.append(f"{path.name}:{number}: {stripped[:70]}")

    assert not offenders, (
        "a literal '?' inside SQL would be rewritten to a placeholder: "
        + "; ".join(offenders)
    )


@pytest.mark.parametrize(("sql", "expected"), [
    ("SELECT 1", True),
    ("  with x as (select 1) select * from x", True),
    ("INSERT INTO t VALUES(1) RETURNING id", True),
    ("INSERT INTO t VALUES(1)", False),
    ("UPDATE t SET a=1", False),
    ("DELETE FROM t", False),
])
def test_row_returning_classification(sql, expected):
    """A row-returning statement must not go through asyncpg's execute()."""
    assert _returns_rows(sql) is expected
