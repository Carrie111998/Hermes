"""Regression coverage for background delegation shutdown draining."""

import asyncio

import pytest

from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.mark.asyncio
async def test_drain_waits_for_in_flight_async_delegation(monkeypatch):
    """A detached delegate_task must remain visible after its parent turn ends."""
    import tools.async_delegation as async_delegation

    runner, _adapter = make_restart_runner()
    active = [1]
    drain_checked = asyncio.Event()

    def active_count():
        drain_checked.set()
        return active[0]

    monkeypatch.setattr(async_delegation, "active_count", active_count)

    assert runner._active_work_count() == 1
    drain_checked.clear()

    drain_task = asyncio.create_task(runner._drain_active_agents(2.0))
    await asyncio.wait_for(drain_checked.wait(), timeout=2.0)
    assert not drain_task.done(), (
        "drain returned with active_at_start=0 while a background delegation "
        "was still running"
    )

    active[0] = 0
    _snapshot, timed_out = await drain_task

    assert timed_out is False


@pytest.mark.asyncio
async def test_drain_waits_for_terminal_background_process(monkeypatch):
    """Process-registry work must not be killed before the drain budget."""
    from tools.process_registry import process_registry

    runner, _adapter = make_restart_runner()
    active = [True]
    drain_checked = asyncio.Event()

    def has_any_active():
        drain_checked.set()
        return active[0]

    monkeypatch.setattr(process_registry, "has_any_active", has_any_active)

    assert runner._active_work_count() == 1
    drain_checked.clear()

    drain_task = asyncio.create_task(runner._drain_active_agents(2.0))
    await asyncio.wait_for(drain_checked.wait(), timeout=2.0)
    assert not drain_task.done()

    active[0] = False
    _snapshot, timed_out = await drain_task

    assert timed_out is False


@pytest.mark.asyncio
async def test_background_work_drain_is_bounded(monkeypatch):
    """Live detached work past the deadline must report a timeout."""
    import tools.async_delegation as async_delegation
    from tools.process_registry import process_registry

    runner, _adapter = make_restart_runner()
    monkeypatch.setattr(async_delegation, "active_count", lambda: 1)
    monkeypatch.setattr(process_registry, "has_any_active", lambda: True)

    _snapshot, timed_out = await runner._drain_active_agents(0.05)

    assert timed_out is True


@pytest.mark.asyncio
async def test_permanent_supervised_watcher_does_not_block_drain():
    """Gateway-lifetime watchers cannot be part of the shutdown work count."""
    runner, _adapter = make_restart_runner()
    blocker = asyncio.Event()
    watcher = asyncio.create_task(blocker.wait())
    watcher._hermes_supervised_watcher = True
    runner._background_tasks.add(watcher)

    try:
        assert runner._active_work_count() == 0
        _snapshot, timed_out = await runner._drain_active_agents(0.05)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)

    assert timed_out is False
