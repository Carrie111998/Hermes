import asyncio
import threading
import time

import pytest

import gateway.run as gateway_run
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.mark.asyncio
async def test_request_restart_rejects_calls_without_explicit_authorization():
    """A raw in-process call must not be able to initiate a gateway restart."""
    runner, _ = make_restart_runner()
    runner._restart_capability = object()
    runner._active_restart_capability = None

    try:
        accepted = runner.request_restart(detached=False, via_service=True)

        assert accepted is False
        assert runner._restart_requested is False
        assert runner._restart_task_started is False
    finally:
        if runner._restart_task is not None:
            runner._restart_task.cancel()
        for task in list(runner._background_tasks):
            task.cancel()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_external_restart_authorization_is_single_use():
    """An explicitly authorized updater signal can restart exactly once."""
    runner, _ = make_restart_runner()
    runner._restart_capability = object()
    runner._active_restart_capability = None
    runner.config.restart_signal_policy = "explicit_only"
    runner._authorize_external_restart_signal(ttl_seconds=60)

    try:
        assert runner._request_restart_from_external_signal() is True
        assert runner._request_restart_from_external_signal() is False
    finally:
        if runner._restart_task is not None:
            runner._restart_task.cancel()
        for task in list(runner._background_tasks):
            task.cancel()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_explicit_only_policy_rejects_unapproved_sigusr1():
    runner, _ = make_restart_runner()
    runner._restart_capability = object()
    runner._active_restart_capability = None
    runner.config.restart_signal_policy = "explicit_only"

    assert runner._request_restart_from_external_signal() is False
    assert runner._restart_requested is False
    assert runner._restart_task_started is False


@pytest.mark.asyncio
async def test_legacy_policy_preserves_external_sigusr1_restart():
    runner, _ = make_restart_runner()
    runner._restart_capability = object()
    runner._active_restart_capability = None
    runner.config.restart_signal_policy = "legacy"

    try:
        assert runner._request_restart_from_external_signal() is True
    finally:
        if runner._restart_task is not None:
            runner._restart_task.cancel()
        await asyncio.sleep(0)


def test_authorized_restart_watchdog_forces_service_restart_exit(monkeypatch):
    runner, _ = make_restart_runner()
    runner._restart_exit_watchdog_started = False
    exited = threading.Event()
    exit_codes = []

    def fake_exit(code):
        exit_codes.append(code)
        exited.set()

    monkeypatch.setattr(gateway_run, "_exit_after_graceful_shutdown", fake_exit)

    runner._arm_restart_exit_watchdog(via_service=True, timeout_seconds=0.01)

    assert exited.wait(1)
    assert exit_codes == [gateway_run.GATEWAY_SERVICE_RESTART_EXIT_CODE]


@pytest.mark.asyncio
async def test_wait_for_shutdown_waits_for_complete_stop_task():
    runner, _ = make_restart_runner()
    release_stop = asyncio.Event()

    async def finish_stop():
        await release_stop.wait()

    runner._stop_task = asyncio.create_task(finish_stop())
    runner._shutdown_event.set()
    waiter = asyncio.create_task(runner.wait_for_shutdown())

    await asyncio.sleep(0)
    assert waiter.done() is False

    release_stop.set()
    await waiter


@pytest.mark.asyncio
async def test_mcp_shutdown_is_bounded_when_cleanup_deadlocks(monkeypatch):
    release_cleanup = threading.Event()

    def stuck_shutdown():
        release_cleanup.wait()

    monkeypatch.setattr("tools.mcp_tool.shutdown_mcp_servers", stuck_shutdown)
    started = time.monotonic()
    try:
        stopped = await gateway_run._shutdown_mcp_servers_bounded(timeout=0.02)
    finally:
        release_cleanup.set()

    assert stopped is False
    assert time.monotonic() - started < 0.5
