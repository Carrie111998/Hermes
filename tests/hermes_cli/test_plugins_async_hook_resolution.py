"""Async hook/middleware coroutine resolution.

``invoke_hook()``/``invoke_middleware()`` call plugin callbacks from sync
call sites.  A callback declared ``async def`` used to produce a coroutine
that was never awaited: the hook silently did nothing beyond a
``RuntimeWarning: coroutine ... was never awaited``.  These tests cover the
bounded resolver that awaits such callbacks on every thread configuration.

Every test carries an explicit pytest timeout: a resolver regression on the
loop-thread path would otherwise hang the suite rather than fail it (naively
scheduling onto the caller's own loop and blocking deadlocks forever).
"""

import asyncio

import pytest

from hermes_cli.plugins import PluginManager


def _manager_with_hook(cb):
    mgr = PluginManager.__new__(PluginManager)
    mgr._hooks = {"test_hook": [cb]}
    return mgr


def _manager_with_middleware(cb):
    mgr = PluginManager.__new__(PluginManager)
    mgr._middleware = {"test_mw": [cb]}
    return mgr


# ---------------------------------------------------------------- invoke_hook

@pytest.mark.timeout(60)
def test_hook_async_callback_no_running_loop():
    """Plain thread, no loop: coroutine result lands in results."""
    async def cb(**kwargs):
        return "hook-ok"

    assert _manager_with_hook(cb).invoke_hook("test_hook") == ["hook-ok"]


@pytest.mark.timeout(60)
def test_hook_async_callback_on_loop_thread_does_not_deadlock():
    """invoke_hook called on the event-loop thread must not deadlock.

    ``get_running_loop()`` succeeding means this thread IS the loop thread;
    a resolver that schedules onto that loop and blocks on the future waits
    on itself forever.
    """
    async def cb(**kwargs):
        await asyncio.sleep(0.05)
        return "loop-ok"

    mgr = _manager_with_hook(cb)

    async def main():
        results = mgr.invoke_hook("test_hook")
        # the loop must still be usable afterwards
        await asyncio.sleep(0)
        return results

    assert asyncio.run(main()) == ["loop-ok"]


@pytest.mark.timeout(120)
def test_hook_hung_callback_times_out_and_other_callbacks_survive(monkeypatch):
    """A never-returning coroutine raises TimeoutError into the existing
    per-callback except, and the remaining callbacks still run."""
    monkeypatch.setattr(PluginManager, "_HOOK_AWAIT_TIMEOUT_SECS", 0.5)

    async def hung(**kwargs):
        await asyncio.sleep(3600)

    async def healthy(**kwargs):
        return "survivor"

    mgr = PluginManager.__new__(PluginManager)
    mgr._hooks = {"test_hook": [hung, healthy]}

    async def main():
        return mgr.invoke_hook("test_hook")

    assert asyncio.run(main()) == ["survivor"]


@pytest.mark.timeout(120)
def test_hook_hung_callback_times_out_on_plain_thread_too(monkeypatch):
    """The no-running-loop path (the common thread-pool path) is bounded
    as well, not just the loop-thread branch."""
    monkeypatch.setattr(PluginManager, "_HOOK_AWAIT_TIMEOUT_SECS", 0.5)

    async def hung(**kwargs):
        await asyncio.sleep(3600)

    async def healthy(**kwargs):
        return "survivor"

    mgr = PluginManager.__new__(PluginManager)
    mgr._hooks = {"test_hook": [hung, healthy]}
    assert mgr.invoke_hook("test_hook") == ["survivor"]


@pytest.mark.timeout(120)
def test_timed_out_helper_thread_exits(monkeypatch):
    """On timeout the hook coroutine is cancelled via wait_for inside the
    helper thread, so the thread exits instead of leaking one
    'hermes-hook-await' thread per timed-out invocation."""
    import threading as _threading
    import time as _time

    monkeypatch.setattr(PluginManager, "_HOOK_AWAIT_TIMEOUT_SECS", 0.5)

    async def hung(**kwargs):
        await asyncio.sleep(3600)

    mgr = _manager_with_hook(hung)

    async def main():
        return mgr.invoke_hook("test_hook")

    assert asyncio.run(main()) == []
    # give the cancelled helper a moment to unwind, then assert it is gone
    deadline = _time.time() + 5
    while _time.time() < deadline:
        if not any(t.name == "hermes-hook-await" and t.is_alive()
                   for t in _threading.enumerate()):
            break
        _time.sleep(0.1)
    leaked = [t.name for t in _threading.enumerate()
              if t.name == "hermes-hook-await" and t.is_alive()]
    assert not leaked, f"helper thread(s) leaked after timeout: {leaked}"


# ---------------------------------------------------------- invoke_middleware

@pytest.mark.timeout(60)
def test_middleware_async_callback_no_running_loop():
    async def cb(**kwargs):
        return {"mw": True}

    assert _manager_with_middleware(cb).invoke_middleware("test_mw") == [{"mw": True}]


@pytest.mark.timeout(60)
def test_middleware_async_callback_on_loop_thread_does_not_deadlock():
    async def cb(**kwargs):
        await asyncio.sleep(0.05)
        return "mw-loop-ok"

    mgr = _manager_with_middleware(cb)

    async def main():
        results = mgr.invoke_middleware("test_mw")
        await asyncio.sleep(0)
        return results

    assert asyncio.run(main()) == ["mw-loop-ok"]


@pytest.mark.timeout(120)
def test_middleware_hung_callback_times_out(monkeypatch):
    monkeypatch.setattr(PluginManager, "_HOOK_AWAIT_TIMEOUT_SECS", 0.5)

    async def hung(**kwargs):
        await asyncio.sleep(3600)

    mgr = _manager_with_middleware(hung)

    async def main():
        return mgr.invoke_middleware("test_mw")

    assert asyncio.run(main()) == []


# ------------------------------------------------------------------- details

@pytest.mark.timeout(60)
def test_callback_exception_propagates_not_swallowed_by_resolver():
    """An async callback that raises must surface its real exception type
    (the resolver re-raises; invoke_hook's except logs it and moves on)."""
    async def boom(**kwargs):
        raise ValueError("real error")

    async def healthy(**kwargs):
        return "still-here"

    mgr = PluginManager.__new__(PluginManager)
    mgr._hooks = {"test_hook": [boom, healthy]}
    assert mgr.invoke_hook("test_hook") == ["still-here"]


@pytest.mark.timeout(60)
def test_sync_callbacks_unaffected():
    def sync_cb(**kwargs):
        return "sync-ok"

    assert _manager_with_hook(sync_cb).invoke_hook("test_hook") == ["sync-ok"]
