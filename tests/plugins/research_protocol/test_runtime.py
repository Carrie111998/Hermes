"""Fail-closed runtime configuration tests."""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
import types

import pytest

from plugins.research_protocol.planner_tools import (
    check_planner_approvals,
    check_planner_artifacts,
    check_planner_context,
    configure_planner_runtime,
)
from plugins.research_protocol.runtime import (
    LazyAsyncpgPool,
    RuntimeConfigurationError,
    build_runtime_bundle,
)


class _RuntimeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _RuntimeConnection:
    def transaction(self, **_kwargs):
        return _RuntimeTransaction()

    async def execute(self, *_args):
        return None

    async def fetch(self, *_args):
        return []


class _RuntimeAcquire:
    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        if asyncio.get_running_loop() is not self._pool.creation_loop:
            raise RuntimeError("pool used from a different event loop")
        return _RuntimeConnection()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _RuntimeLoopBoundPool:
    def __init__(self, *, block_close=False, fail_close=False):
        self.creation_loop = asyncio.get_running_loop()
        self.close_calls = 0
        self.close_loops = []
        self.fail_close = fail_close
        self.terminate_calls = 0
        self.close_started = asyncio.Event() if block_close else None
        self.allow_close = asyncio.Event() if block_close else None

    def acquire(self):
        return _RuntimeAcquire(self)

    async def close(self):
        if asyncio.get_running_loop() is not self.creation_loop:
            raise RuntimeError("pool closed from a different event loop")
        if self.close_started is not None and self.allow_close is not None:
            self.close_started.set()
            await self.allow_close.wait()
        self.close_loops.append(asyncio.get_running_loop())
        if self.fail_close:
            raise RuntimeError("pool close failed")
        self.close_calls += 1

    def terminate(self):
        self.terminate_calls += 1


def test_runtime_rejects_missing_or_relative_artifact_root(tmp_path):
    with pytest.raises(RuntimeConfigurationError, match="absolute artifact_root"):
        build_runtime_bundle({}, environ={})
    with pytest.raises(RuntimeConfigurationError, match="absolute artifact_root"):
        build_runtime_bundle({"artifact_root": "relative/artifacts"}, environ={})


def test_artifact_only_runtime_does_not_enable_database_tools(tmp_path):
    bundle = build_runtime_bundle(
        {"artifact_root": str(tmp_path / "artifacts")},
        environ={},
    )
    configure_planner_runtime(
        bundle.handlers,
        artifact_available=bundle.artifact_available,
        context_available=bundle.context_available,
        approval_available=bundle.approval_available,
    )
    try:
        assert check_planner_artifacts() is True
        assert check_planner_context() is False
        assert check_planner_approvals() is False
        result = asyncio.run(
            bundle.handlers.context_read({
                "query_id": "approval_status.v1",
                "parameters": {"approval_id": "approval-001"},
            })
        )
        assert result == {"ok": False, "error": "planner database runtime unavailable"}
    finally:
        configure_planner_runtime(None)


def test_database_runtime_uses_distinct_lazy_reader_and_writer_pools(tmp_path):
    bundle = build_runtime_bundle(
        {
            "artifact_root": str(tmp_path / "artifacts"),
            "database": {
                "max_rows": 7,
                "max_bytes": 4096,
                "timeout_seconds": 2.0,
            },
        },
        environ={
            "RESEARCH_PROTOCOL_READER_DATABASE_URL": (
                "postgresql://reader:not-contacted@invalid/read"
            ),
            "RESEARCH_PROTOCOL_WRITER_DATABASE_URL": (
                "postgresql://writer:not-contacted@invalid/write"
            ),
        },
    )

    assert bundle.artifact_available is True
    assert bundle.context_available is True
    assert bundle.approval_available is True
    assert bundle.reader_pool is not None
    assert bundle.writer_pool is not None
    assert bundle.reader_pool is not bundle.writer_pool
    assert bundle.reader_pool.created is False
    assert bundle.writer_pool.created is False


def test_database_runtime_replaces_pool_from_closed_loop_and_terminates_it(
    tmp_path, monkeypatch
):
    created_pools = []

    async def create_pool(**_kwargs):
        pool = _RuntimeLoopBoundPool()
        created_pools.append(pool)
        return pool

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        types.SimpleNamespace(create_pool=create_pool),
    )
    bundle = build_runtime_bundle(
        {"artifact_root": str(tmp_path / "artifacts")},
        environ={
            "RESEARCH_PROTOCOL_READER_DATABASE_URL": (
                "postgresql://reader.invalid/research"
            )
        },
    )
    request = {
        "query_id": "approval_status.v1",
        "parameters": {"approval_id": "approval-001"},
    }
    reader_pool = bundle.reader_pool
    assert reader_pool is not None

    first = asyncio.run(bundle.handlers.context_read(request))
    first_loop = created_pools[0].creation_loop
    second = asyncio.run(bundle.handlers.context_read(request))

    assert first["ok"] is True
    assert second["ok"] is True
    assert first_loop.is_closed()
    assert len(created_pools) == 2
    assert created_pools[0].terminate_calls == 1
    assert created_pools[1].creation_loop is not first_loop
    asyncio.run(reader_pool.close())
    assert reader_pool.created is False
    assert created_pools[1].terminate_calls == 1


def test_lazy_pool_deduplicates_concurrent_creation_on_same_loop(monkeypatch):
    created_pools = []

    async def create_pool(**_kwargs):
        await asyncio.sleep(0)
        pool = _RuntimeLoopBoundPool()
        created_pools.append(pool)
        return pool

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        types.SimpleNamespace(create_pool=create_pool),
    )

    async def exercise_concurrent_creation():
        lazy_pool = LazyAsyncpgPool("postgresql://reader.invalid/research")
        pools = await asyncio.gather(*(lazy_pool.get_pool() for _ in range(20)))
        assert len({id(pool) for pool in pools}) == 1
        assert lazy_pool.created is True
        await lazy_pool.close()
        assert lazy_pool.created is False

    asyncio.run(exercise_concurrent_creation())

    assert len(created_pools) == 1
    assert created_pools[0].close_calls == 1


def test_lazy_pool_isolates_concurrent_loops_and_closes_on_each_owner(
    monkeypatch,
):
    created_pools = []
    created_guard = threading.Lock()
    ready = threading.Barrier(3)
    release = threading.Event()
    worker_errors = queue.Queue()

    async def create_pool(**_kwargs):
        pool = _RuntimeLoopBoundPool()
        with created_guard:
            created_pools.append(pool)
        return pool

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        types.SimpleNamespace(create_pool=create_pool),
    )
    lazy_pool = LazyAsyncpgPool("postgresql://reader.invalid/research")

    async def hold_owner_loop_open():
        await lazy_pool.get_pool()
        ready.wait(timeout=5)
        while not release.is_set():
            await asyncio.sleep(0.01)

    def run_owner_loop():
        try:
            asyncio.run(hold_owner_loop_open())
        except BaseException as exc:
            worker_errors.put(exc)

    threads = [threading.Thread(target=run_owner_loop) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        ready.wait(timeout=5)
        with created_guard:
            pools = tuple(created_pools)
        assert len(pools) == 2
        assert pools[0] is not pools[1]
        assert pools[0].creation_loop is not pools[1].creation_loop

        asyncio.run(lazy_pool.close())
        assert lazy_pool.created is False
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)

    assert all(thread.is_alive() is False for thread in threads)
    assert worker_errors.empty()
    assert [pool.close_calls for pool in pools] == [1, 1]
    assert [pool.terminate_calls for pool in pools] == [0, 0]
    assert [pool.close_loops for pool in pools] == [
        [pools[0].creation_loop],
        [pools[1].creation_loop],
    ]


def test_lazy_pool_close_blocks_new_pool_until_cleanup_finishes(monkeypatch):
    created_pools = []

    async def create_pool(**_kwargs):
        pool = _RuntimeLoopBoundPool(block_close=not created_pools)
        created_pools.append(pool)
        return pool

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        types.SimpleNamespace(create_pool=create_pool),
    )

    async def exercise_close_barrier():
        lazy_pool = LazyAsyncpgPool("postgresql://reader.invalid/research")
        original = await lazy_pool.get_pool()
        close_task = asyncio.create_task(lazy_pool.close())
        await original.close_started.wait()
        second_close_task = asyncio.create_task(lazy_pool.close())
        acquire_task = asyncio.create_task(lazy_pool.get_pool())
        await asyncio.sleep(0)
        try:
            assert acquire_task.done() is False
            assert second_close_task.done() is False
        finally:
            original.allow_close.set()
            await asyncio.gather(close_task, second_close_task)
            replacement = await acquire_task
            await lazy_pool.close()
        assert replacement is not original

    asyncio.run(exercise_close_barrier())

    assert len(created_pools) == 2
    assert created_pools[0].close_calls == 1
    assert created_pools[1].close_calls == 1


def test_lazy_pool_cancelled_waiters_do_not_cancel_shared_close(monkeypatch):
    created_pools = []

    async def create_pool(**_kwargs):
        pool = _RuntimeLoopBoundPool(block_close=True)
        created_pools.append(pool)
        return pool

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        types.SimpleNamespace(create_pool=create_pool),
    )

    async def exercise_cancelled_waiters():
        lazy_pool = LazyAsyncpgPool("postgresql://reader.invalid/research")
        original = await lazy_pool.get_pool()
        close_task = asyncio.create_task(lazy_pool.close())
        await original.close_started.wait()
        acquire_waiter = asyncio.create_task(lazy_pool.get_pool())
        close_waiter = asyncio.create_task(lazy_pool.close())
        await asyncio.sleep(0)

        acquire_waiter.cancel()
        close_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await acquire_waiter
        with pytest.raises(asyncio.CancelledError):
            await close_waiter

        assert close_task.done() is False
        original.allow_close.set()
        await close_task
        assert lazy_pool.created is False

    asyncio.run(exercise_cancelled_waiters())

    assert len(created_pools) == 1
    assert created_pools[0].close_calls == 1
    assert created_pools[0].terminate_calls == 0


def test_lazy_pool_cancelled_remote_close_waits_for_owner_cleanup(monkeypatch):
    created_pools = []
    created_guard = threading.Lock()
    owner_ready = threading.Event()
    release_owner = threading.Event()
    lock_held = threading.Event()
    release_lock = threading.Event()
    worker_errors = queue.Queue()

    async def create_pool(**_kwargs):
        pool = _RuntimeLoopBoundPool()
        with created_guard:
            created_pools.append(pool)
        return pool

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        types.SimpleNamespace(create_pool=create_pool),
    )
    lazy_pool = LazyAsyncpgPool("postgresql://reader.invalid/research")

    async def hold_owner_loop_open():
        await lazy_pool.get_pool()
        owner_ready.set()
        while not release_owner.is_set():
            await asyncio.sleep(0.01)

    def run_owner_loop():
        try:
            asyncio.run(hold_owner_loop_open())
        except BaseException as exc:
            worker_errors.put(exc)

    owner_thread = threading.Thread(target=run_owner_loop)
    owner_thread.start()
    try:
        assert owner_ready.wait(timeout=5)
        with created_guard:
            original = created_pools[0]
        with lazy_pool._states_guard:
            state = lazy_pool._states[original.creation_loop]

        async def hold_state_lock():
            async with state.lock:
                lock_held.set()
                while not release_lock.is_set():
                    await asyncio.sleep(0.01)

        lock_future = asyncio.run_coroutine_threadsafe(
            hold_state_lock(), original.creation_loop
        )
        assert lock_held.wait(timeout=5)

        async def exercise_cancelled_close():
            close_task = asyncio.create_task(lazy_pool.close())
            await asyncio.sleep(0.05)
            close_task.cancel()
            await asyncio.sleep(0.05)
            assert close_task.done() is False
            release_lock.set()
            with pytest.raises(asyncio.CancelledError):
                await close_task

        asyncio.run(exercise_cancelled_close())
        lock_future.result(timeout=5)
    finally:
        release_lock.set()
        release_owner.set()
        owner_thread.join(timeout=5)

    assert owner_thread.is_alive() is False
    assert worker_errors.empty()
    assert original.close_calls == 1
    assert original.terminate_calls == 0
    assert original.close_loops == [original.creation_loop]


def test_lazy_pool_terminates_when_owner_loop_stops_after_submission(monkeypatch):
    created_pools = []
    owner_loops = []
    owner_ready = threading.Event()
    owner_stopped = threading.Event()
    allow_shutdown = threading.Event()
    worker_errors = queue.Queue()

    async def create_pool(**_kwargs):
        pool = _RuntimeLoopBoundPool()
        created_pools.append(pool)
        return pool

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        types.SimpleNamespace(create_pool=create_pool),
    )
    lazy_pool = LazyAsyncpgPool("postgresql://reader.invalid/research")

    def run_owner_loop():
        loop = asyncio.new_event_loop()
        owner_loops.append(loop)
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(lazy_pool.get_pool())
            loop.call_soon(owner_ready.set)
            loop.run_forever()
            owner_stopped.set()
            allow_shutdown.wait(timeout=5)
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
        except BaseException as exc:
            worker_errors.put(exc)
        finally:
            loop.close()

    owner_thread = threading.Thread(target=run_owner_loop)
    owner_thread.start()
    try:
        assert owner_ready.wait(timeout=5)
        original_submit = asyncio.run_coroutine_threadsafe

        def submit_then_stop(coroutine, owner_loop):
            future = original_submit(coroutine, owner_loop)
            owner_loop.call_soon_threadsafe(owner_loop.stop)
            return future

        monkeypatch.setattr(
            asyncio,
            "run_coroutine_threadsafe",
            submit_then_stop,
        )
        asyncio.run(asyncio.wait_for(lazy_pool.close(), timeout=0.5))
        assert owner_stopped.wait(timeout=5)
    finally:
        allow_shutdown.set()
        owner_thread.join(timeout=5)

    assert owner_thread.is_alive() is False
    assert worker_errors.empty()
    assert len(owner_loops) == 1
    assert len(created_pools) == 1
    assert created_pools[0].terminate_calls == 1
    assert created_pools[0].close_calls == 0


def test_lazy_pool_close_terminates_failed_pool_and_propagates(monkeypatch):
    created_pools = []

    async def create_pool(**_kwargs):
        pool = _RuntimeLoopBoundPool(fail_close=not created_pools)
        created_pools.append(pool)
        return pool

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        types.SimpleNamespace(create_pool=create_pool),
    )

    async def exercise_failed_close():
        lazy_pool = LazyAsyncpgPool("postgresql://reader.invalid/research")
        original = await lazy_pool.get_pool()
        with pytest.raises(RuntimeError, match="pool close failed"):
            await lazy_pool.close()
        assert lazy_pool.created is False
        replacement = await lazy_pool.get_pool()
        await lazy_pool.close()
        return original, replacement

    original, replacement = asyncio.run(exercise_failed_close())

    assert replacement is not original
    assert original.terminate_calls == 1
    assert replacement.close_calls == 1


def test_lazy_pool_terminates_failed_creation_retired_by_close(monkeypatch):
    created_pools = []

    async def exercise_retired_creation():
        create_started = asyncio.Event()
        allow_create = asyncio.Event()

        async def create_pool(**_kwargs):
            create_started.set()
            await allow_create.wait()
            pool = _RuntimeLoopBoundPool(fail_close=True)
            created_pools.append(pool)
            return pool

        monkeypatch.setitem(
            sys.modules,
            "asyncpg",
            types.SimpleNamespace(create_pool=create_pool),
        )
        lazy_pool = LazyAsyncpgPool("postgresql://reader.invalid/research")
        acquire_task = asyncio.create_task(lazy_pool.get_pool())
        await create_started.wait()
        close_task = asyncio.create_task(lazy_pool.close())
        await asyncio.sleep(0)
        allow_create.set()

        with pytest.raises(RuntimeError, match="pool close failed"):
            await acquire_task
        await close_task
        assert lazy_pool.created is False

    asyncio.run(exercise_retired_creation())

    assert len(created_pools) == 1
    assert created_pools[0].terminate_calls == 1


def test_database_runtime_keeps_reader_and_writer_authority_independent(tmp_path):
    reader_only = build_runtime_bundle(
        {"artifact_root": str(tmp_path / "reader-artifacts")},
        environ={
            "RESEARCH_PROTOCOL_READER_DATABASE_URL": (
                "postgresql://reader:not-contacted@invalid/read"
            )
        },
    )
    assert reader_only.context_available is True
    assert reader_only.approval_available is False

    writer_only = build_runtime_bundle(
        {"artifact_root": str(tmp_path / "writer-artifacts")},
        environ={
            "RESEARCH_PROTOCOL_WRITER_DATABASE_URL": (
                "postgresql://writer:not-contacted@invalid/write"
            )
        },
    )
    assert writer_only.context_available is False
    assert writer_only.approval_available is True


def test_database_runtime_rejects_shared_reader_writer_dsn(tmp_path):
    dsn = "postgresql://combined:not-contacted@invalid/database"
    with pytest.raises(RuntimeConfigurationError, match="must be distinct"):
        build_runtime_bundle(
            {"artifact_root": str(tmp_path / "artifacts")},
            environ={
                "RESEARCH_PROTOCOL_READER_DATABASE_URL": dsn,
                "RESEARCH_PROTOCOL_WRITER_DATABASE_URL": dsn,
            },
        )


def test_runtime_rejects_out_of_range_database_caps(tmp_path):
    with pytest.raises(RuntimeConfigurationError, match="max_rows"):
        build_runtime_bundle(
            {
                "artifact_root": str(tmp_path / "artifacts"),
                "database": {"max_rows": 0},
            },
            environ={
                "RESEARCH_PROTOCOL_READER_DATABASE_URL": "postgresql://example/read"
            },
        )
