"""Sync facade over asyncpg for Supabase-hosted Postgres.

The product domain is synchronous because Hermes tools and workers are
synchronous. A dedicated event-loop thread owns the asyncpg pool, allowing the
same repository contract to serve FastAPI threadpool handlers and workers.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import threading
from concurrent.futures import Future
from typing import Any, Iterator, Sequence

from .db import json_dump, new_id, now


def _sql(sql: str) -> str:
    index = 0

    def replace(_: re.Match) -> str:
        nonlocal index
        index += 1
        return f"${index}"

    return re.sub(r"\?", replace, sql)


def _row(value):
    return dict(value) if value is not None else None


# A row-returning statement has to go through asyncpg's fetch(); execute()
# hands back only a status string ("SELECT 1"). Writes stay on execute() so
# their behaviour — including multi-statement strings — is unchanged.
def _returns_rows(sql: str) -> bool:
    head = sql.lstrip().lstrip("(").lstrip()[:6].upper()
    return head.startswith(("SELECT", "WITH")) or " RETURNING " in f" {sql.upper()} "


class _Result:
    """Cursor-shaped view of one statement, matching the sqlite3 API.

    Callers share one repository contract across both backends, so a
    transaction here must answer fetchone()/fetchall() exactly as
    sqlite3.Connection.execute() does.
    """

    __slots__ = ("_rows",)

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict]:
        return list(self._rows)


class _TransactionProxy:
    def __init__(self, db: "PostgresDatabase"):
        self.db = db
        self.conn = None
        self.tx = None

    async def _start(self):
        self.conn = await self.db.pool.acquire()
        self.tx = self.conn.transaction()
        await self.tx.start()

    def __enter__(self):
        self.db._run(self._start())
        return self

    def execute(self, sql: str, params: Sequence[Any] = ()):
        statement = _sql(sql)
        if _returns_rows(sql):
            rows = self.db._run(self.conn.fetch(statement, *params))
            return _Result([_row(row) for row in rows])
        self.db._run(self.conn.execute(statement, *params))
        return _Result([])

    def executemany(self, sql: str, rows):
        return self.db._run(self.conn.executemany(_sql(sql), rows))

    async def _finish(self, commit: bool):
        try:
            await self.tx.commit() if commit else await self.tx.rollback()
        finally:
            await self.db.pool.release(self.conn)

    def __exit__(self, exc_type, exc, traceback):
        self.db._run(self._finish(exc_type is None))
        return False


class PostgresDatabase:
    def __init__(self, url: str):
        try:
            import asyncpg
        except ImportError as exc:
            raise RuntimeError(
                "Supabase Postgres requires the 'interfaze' package extra: "
                "pip install 'hermes-agent[interfaze]'"
            ) from exc
        self.url = url
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True,
                                       name="interfaze-postgres")
        self.thread.start()
        # The pool must be BUILT on the loop it will be used from, not merely
        # awaited there. asyncpg binds internal futures to the running loop, so
        # calling create_pool() on this thread and awaiting the Pool on
        # self.loop attaches those futures to the wrong loop — the await then
        # never completes and __init__ dies on the _run timeout, 60s after boot.
        #
        # statement_cache_size=0 so the Supabase transaction pooler (port 6543)
        # works: it multiplexes connections, so asyncpg's cached prepared
        # statements collide with DuplicatePreparedStatementError.
        # ponytail: unconditional, not pooler-sniffing — re-parsing each query
        # is invisible next to a hosted-Postgres round trip.
        async def _open_pool():
            return await asyncpg.create_pool(url, min_size=1, max_size=10,
                                             command_timeout=30,
                                             statement_cache_size=0)

        self.pool = self._run(_open_pool())
        try:
            self.one("SELECT id FROM companies LIMIT 1")
        except Exception as exc:
            raise RuntimeError(
                "Supabase schema is missing. Apply server/supabase/migrations/001_initial.sql first."
            ) from exc
        self._assert_migrations_applied()

    # Every migration file records itself in schema_migrations. Booting with a
    # partial set is how a database ends up with lead-research tables that have
    # no RLS, or credential tables that are world-readable — both silent.
    REQUIRED_MIGRATIONS = ("001_initial", "002_chat_sessions", "003_lead_research",
                           "004_lead_research_rls", "005_auth_table_rls",
                           "006_message_supersession", "007_document_artifacts",
                           "008_candidate_corpus", "009_research_results",
                           "010_digest_suppression_parity")

    def _assert_migrations_applied(self) -> None:
        try:
            applied = {row["version"] for row in self.all("SELECT version FROM schema_migrations")}
        except Exception as exc:
            raise RuntimeError(
                "schema_migrations is missing. Re-apply server/supabase/migrations/ in order; "
                "001_initial.sql creates it."
            ) from exc
        missing = [name for name in self.REQUIRED_MIGRATIONS if name not in applied]
        if missing:
            raise RuntimeError(
                "Unapplied Supabase migrations: " + ", ".join(missing)
                + ". Apply server/supabase/migrations/ in order before serving traffic; "
                  "004 and 005 enable row-level security."
            )

    def _run(self, awaitable):
        """Run any awaitable on the background loop and return its result.

        run_coroutine_threadsafe accepts *coroutines only*. asyncpg's
        create_pool() returns a Pool — awaitable via __await__, but not a
        coroutine — so passing it straight through raised
        TypeError("A coroutine object is required") before a single query ran.
        Wrapping in a coroutine covers both shapes.
        """
        async def _await():
            return await awaitable

        return asyncio.run_coroutine_threadsafe(_await(), self.loop).result(timeout=60)

    async def _one(self, sql: str, params):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(_sql(sql), *params)

    def one(self, sql: str, params: Sequence[Any] = ()):
        return _row(self._run(self._one(sql, params)))

    async def _all(self, sql: str, params):
        async with self.pool.acquire() as conn:
            return await conn.fetch(_sql(sql), *params)

    def all(self, sql: str, params: Sequence[Any] = ()):
        return [_row(row) for row in self._run(self._all(sql, params))]

    async def _execute(self, sql: str, params):
        async with self.pool.acquire() as conn:
            return await conn.execute(_sql(sql), *params)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        status = self._run(self._execute(sql, params))
        try:
            return int(status.rsplit(" ", 1)[-1])
        except ValueError:
            return 0

    def transaction(self):
        return _TransactionProxy(self)

    def activity(self, company_id, actor_id, action, entity_type=None,
                 entity_id=None, data=None):
        activity_id = new_id("act")
        self.execute("INSERT INTO activity_log VALUES(?,?,?,?,?,?,?,?)",
                     (activity_id, company_id, actor_id, action, entity_type, entity_id,
                      json_dump(data or {}), now()))
        return activity_id

    def close(self) -> None:
        self._run(self.pool.close())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


def create_database(settings):
    if settings.database_url:
        return PostgresDatabase(settings.database_url)
    from .db import Database
    return Database(settings.database_path)
