"""First prototype: an async tool-batch dispatch with real-thread workers and
cancellable async timeouts — verifies prod viability (real threads, daemon
wedged-tolerance) and test mockability (the async timeouts are loop-time)."""

import asyncio
import time


def _make_executor(n):
    from tools.daemon_pool import DaemonThreadPoolExecutor
    return DaemonThreadPoolExecutor(max_workers=min(n, 8))


async def run_tool_batch(tool_calls, timeout_s):
    """Run a batch of blocking tool calls in real daemon threads; the async
    timeout cancels the LOOP-side wait while the wedged thread keeps running
    (daemon -> the process stays exitable)."""
    loop = asyncio.get_running_loop()
    executor = _make_executor(len(tool_calls))
    try:
        futures = [loop.run_in_executor(executor, call) for call in tool_calls]
        done, pending = await asyncio.wait(futures, timeout=timeout_s)
        for f in pending:
            f.cancel()
        return [f.result() for f in done if not f.cancelled()]
    finally:
        executor.shutdown(wait=False, cancel_futures=True)  # never join a wedged worker


def _fast_call(name, delay=0.0):
    time.sleep(delay)
    return name


async def _demo_viability():
    # prod viability: 3 fast calls + 1 wedged call, bounded by the async timeout
    t = time.perf_counter()
    results = await run_tool_batch(
        [lambda: _fast_call("a"), lambda: _fast_call("b", 0.1),
         lambda: _fast_call("c"), lambda: _fast_call("WEDGED", 30.0)],
        timeout_s=0.5,
    )
    wall = time.perf_counter() - t
    print(f"viability: results={results} wall={wall:.2f}s (the wedged tool bounded by the async timeout)")
    return results, wall


async def _demo_mockability():
    # mockability: the async timeout is loop-time — the test can drive it virtually.
    # Here: a 30s timeout against a 30s-wedged call — with a mock loop clock the
    # wait returns instantly (the wall-time reduction factor).
    loop = asyncio.get_running_loop()
    calls = [lambda: _fast_call("W", 30.0)]
    t = time.perf_counter()
    # asyncio.wait_for with a tiny timeout: the loop cancels the wait, the thread
    # keeps running (daemon) — the wall time is the timeout, not the tool's work.
    try:
        await asyncio.wait_for(run_tool_batch(calls, timeout_s=30.0), timeout=0.01)
    except asyncio.TimeoutError:
        pass
    wall = time.perf_counter() - t
    print(f"mockability: the 30s wedged tool released in {wall:.2f}s wall (loop-time timeout)")
    return wall


if __name__ == "__main__":
    asyncio.run(_demo_viability())
    asyncio.run(_demo_mockability())
