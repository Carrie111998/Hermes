"""Regression test for #53175: gateway event loop wedged by synchronous
agent-resource cleanup run inline from loop coroutines.

#35994 fixed the /new reset path, but the same synchronous
``_cleanup_agent_resources`` (agent.close() tears down terminal sandboxes /
browser daemons / background processes; shutdown_memory_provider() may do
SQLite / network IO via a memory plugin) was still called INLINE on the event
loop from three other places:

  * ``_session_expiry_watcher`` (the 5-minute idle sweep) — live loop
  * ``_handle_message_with_agent`` cache-hygiene re-eviction — live loop
  * ``_finalize_shutdown_agents`` / ``stop()`` idle-cache loop — shutdown

A wedged provider on any of these froze the whole loop: the bot went silent,
the runtime-status ``updated_at`` heartbeat stopped advancing (the symptom the
reporter's watchdog keyed on), and SIGTERM could not be serviced (requiring
``kill -9``).

The fix routes all four call sites through ``_cleanup_agent_resources_off_loop``
which offloads to a worker thread under a bounded ``asyncio.wait_for``, so the
loop is never blocked and a stuck teardown degrades gracefully.

These tests drive that shared helper directly — it is the single chokepoint
every fixed call site now uses.
"""
import asyncio
import logging
import threading
from contextvars import copy_context
from types import SimpleNamespace

import pytest


def _make_runner():
    """Bare GatewayRunner with a real thread-pool-backed executor helper."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    from concurrent.futures import ThreadPoolExecutor

    executor = ThreadPoolExecutor(max_workers=2)
    runner._get_executor = lambda: executor

    async def _run_in_executor_with_context(func, *args):
        loop = asyncio.get_running_loop()
        ctx = copy_context()
        return await loop.run_in_executor(executor, lambda: ctx.run(func, *args))

    runner._run_in_executor_with_context = _run_in_executor_with_context
    return runner, executor


def _agent_with_close(close_fn):
    return SimpleNamespace(
        close=close_fn,
        shutdown_memory_provider=lambda *a, **k: None,
        _session_messages=None,
    )


# How many heartbeat ticks must land while close() is blocked. Reached under a
# DEADLINE, never counted inside a fixed wall-clock window -- see the comment in
# test_cleanup_off_loop_does_not_block_event_loop for why that distinction is
# the whole difference between this test and a flaky one.
_TICKS_REQUIRED = 5
# Each bounded wait must stay comfortably under slow_close()'s 5s block: in an
# inline-cleanup regression the loop is frozen for that whole block, so a
# deadline shorter than it is what turns the freeze into a TimeoutError.
_WAIT_BUDGET_S = 2.0


@pytest.mark.asyncio
async def test_cleanup_off_loop_does_not_block_event_loop():
    """A slow agent.close() must NOT freeze the loop. A concurrent heartbeat
    keeps ticking WHILE close() blocks in its worker thread — proving the
    cleanup was offloaded, not run inline (which would freeze the loop and
    stall the runtime-status updated_at heartbeat, #53175)."""
    runner, executor = _make_runner()
    close_started = threading.Event()
    close_returned = threading.Event()
    release = threading.Event()

    def slow_close():
        close_started.set()
        release.wait(timeout=5)  # block the WORKER thread, not the loop
        close_returned.set()

    agent = _agent_with_close(slow_close)

    ticks = {"n": 0}
    stop = threading.Event()

    async def _heartbeat():
        while not stop.is_set():
            ticks["n"] += 1
            await asyncio.sleep(0.005)

    async def _until(pred):
        while not pred():
            await asyncio.sleep(0.005)

    async def _advance(n):
        target = ticks["n"] + n
        while ticks["n"] < target:
            await asyncio.sleep(0.005)

    hb = asyncio.create_task(_heartbeat())
    cleanup_task = asyncio.create_task(
        runner._cleanup_agent_resources_off_loop(agent, context="test")
    )

    # Wait for a tick COUNT under a deadline; do NOT count ticks inside a fixed
    # wall-clock window. `asyncio.sleep(0.005)` is nominally 20 ticks per 100ms,
    # but Windows' ProactorEventLoop timer granularity delivers ~7 on an IDLE
    # box and 3 under the nightly gate's 12 workers -- so the old
    # "ticks in 0.1s >= 5" form was asserting timer resolution, not loop
    # liveness, on a 1.4x margin. It went RED on 2026-08-17 at 3 ticks. Reaching
    # 5 ticks costs ~60ms of a 2s budget, a ~33x margin, and measures the
    # property the test is actually named for.
    try:
        await asyncio.wait_for(
            _until(close_started.is_set), timeout=_WAIT_BUDGET_S
        )
        await asyncio.wait_for(
            _advance(_TICKS_REQUIRED), timeout=_WAIT_BUDGET_S
        )
        # Both waits above already fail closed on an inline regression (a frozen
        # loop leaves their deadlines long overdue by the time it resumes). This
        # asserts the same thing positively: the ticks were sampled while
        # close() was STILL in flight, not after it had returned. Without it the
        # test passes an inline cleanup, because a frozen loop cannot observe
        # close_started until close() has already finished.
        assert not close_returned.is_set(), (
            "close() had already returned before the heartbeat was sampled "
            "(#53175): the loop was frozen for the whole cleanup, so ticking "
            "afterwards proves nothing"
        )
    except asyncio.TimeoutError:
        raise AssertionError(
            f"event loop was blocked during agent cleanup (#53175): the "
            f"heartbeat did not reach {_TICKS_REQUIRED} ticks within "
            f"{_WAIT_BUDGET_S}s while close() was running"
        ) from None
    finally:
        release.set()
        stop.set()
        await cleanup_task
        await hb
        executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_cleanup_off_loop_times_out_gracefully(caplog):
    """A cleanup that exceeds the bounded timeout logs a warning and returns —
    the caller (sweep / shutdown / hygiene) proceeds rather than hanging."""
    runner, executor = _make_runner()

    async def _instant_timeout(aw, timeout=None):
        if asyncio.iscoroutine(aw):
            aw.close()
        raise asyncio.TimeoutError

    import gateway.run as _run

    agent = _agent_with_close(lambda: None)
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        # Patch the wait_for the helper uses so we don't actually wait 30s.
        orig = _run.asyncio.wait_for
        _run.asyncio.wait_for = _instant_timeout
        try:
            await runner._cleanup_agent_resources_off_loop(agent, context="sweep")
        finally:
            _run.asyncio.wait_for = orig
    executor.shutdown(wait=False)

    assert any(
        "exceeded" in r.message and "#53175" in r.message for r in caplog.records
    ), "expected the timeout warning to be logged"


@pytest.mark.asyncio
async def test_cleanup_off_loop_swallows_executor_failure(caplog):
    """If the offloaded cleanup raises, the helper logs and returns — a
    teardown failure must never abort the loop coroutine that triggered it."""
    runner, executor = _make_runner()

    def boom():
        raise RuntimeError("provider shutdown blew up")

    # _cleanup_agent_resources swallows its own internal errors, so to reach
    # the helper's except branch make the offloaded call itself raise.
    def _boom_cleanup(agent):
        raise RuntimeError("boom")

    runner._cleanup_agent_resources = _boom_cleanup

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        await runner._cleanup_agent_resources_off_loop(
            _agent_with_close(boom), context="shutdown finalize"
        )
    executor.shutdown(wait=False)

    assert any(
        "failed" in r.message and "#53175" in r.message for r in caplog.records
    ), "expected the cleanup-failure warning to be logged"


@pytest.mark.asyncio
async def test_cleanup_off_loop_none_agent_is_noop():
    """A None agent (None cache entry) is a no-op and never touches the loop."""
    runner, executor = _make_runner()
    await runner._cleanup_agent_resources_off_loop(None)
    executor.shutdown(wait=False)
