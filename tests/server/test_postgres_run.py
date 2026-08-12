"""PostgresDatabase._run must accept non-coroutine awaitables.

asyncpg.create_pool() returns a Pool: awaitable via __await__, but not a
coroutine. Passing it to asyncio.run_coroutine_threadsafe raises
TypeError("A coroutine object is required") — which is what the Postgres
backend did on every boot until _run wrapped its argument. No database is
needed to catch that, only an awaitable that isn't a coroutine.
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.postgres import PostgresDatabase


class _PoolLike:
    """Stands in for asyncpg.Pool: awaitable, not a coroutine."""

    def __await__(self):
        async def _init():
            return "pool"

        return _init().__await__()


def _detached_db() -> PostgresDatabase:
    """A PostgresDatabase with only its event loop wired up (no connection)."""
    db = PostgresDatabase.__new__(PostgresDatabase)
    db.loop = asyncio.new_event_loop()
    db.thread = threading.Thread(target=db.loop.run_forever, daemon=True)
    db.thread.start()
    return db


def test_run_accepts_pool_like_awaitable():
    db = _detached_db()
    try:
        assert asyncio.iscoroutine(_PoolLike()) is False, "fixture must not be a coroutine"
        assert db._run(_PoolLike()) == "pool"
    finally:
        db.loop.call_soon_threadsafe(db.loop.stop)


def test_run_still_accepts_plain_coroutines():
    async def answer():
        return 42

    db = _detached_db()
    try:
        assert db._run(answer()) == 42
    finally:
        db.loop.call_soon_threadsafe(db.loop.stop)


def test_pool_is_built_on_the_worker_loop():
    """The pool must be constructed on self.loop, not merely awaited there.

    asyncpg binds internal futures to the running loop, so a Pool built on the
    calling thread and awaited on self.loop attaches futures to the wrong loop
    and the await never completes. Asserting the loop identity at construction
    time catches that without a database.
    """
    db = _detached_db()
    seen = {}

    async def build():
        seen["loop"] = asyncio.get_running_loop()
        return "pool"

    try:
        assert db._run(build()) == "pool"
        assert seen["loop"] is db.loop, "pool was built on the wrong event loop"
    finally:
        db.loop.call_soon_threadsafe(db.loop.stop)


if __name__ == "__main__":
    test_run_accepts_pool_like_awaitable()
    test_run_still_accepts_plain_coroutines()
    print("ok")
