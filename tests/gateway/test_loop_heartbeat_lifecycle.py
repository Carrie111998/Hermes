from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_start_arms_heartbeat_before_startup_diagnostics():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._background_tasks = set()
    runner._gateway_started_at = 123.0
    runner._loop_heartbeat_task = None
    runner._start_loop_liveness_guards = lambda _loop: None

    with patch("gateway.run.faulthandler.enable", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            await runner.start()

    task = runner._loop_heartbeat_task
    assert task is not None
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_heartbeat_is_logged_and_restarted(monkeypatch, caplog):
    calls = 0
    replacement_started = asyncio.Event()
    hold_replacement = asyncio.Event()

    async def flaky_heartbeat(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("heartbeat write failed")
        replacement_started.set()
        await hold_replacement.wait()

    monkeypatch.setattr("gateway.run.loop_heartbeat_forever", flaky_heartbeat)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._background_tasks = set()
    runner._gateway_started_at = 123.0
    runner._loop_heartbeat_task = None
    runner._draining = False
    runner._shutdown_event = asyncio.Event()

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        runner._start_loop_heartbeat_task()
        failed_task = runner._loop_heartbeat_task
        await asyncio.wait_for(replacement_started.wait(), timeout=2.5)

    replacement_task = runner._loop_heartbeat_task
    assert replacement_task is not failed_task
    assert "Gateway loop heartbeat failed; restarting" in caplog.text
    replacement_task.cancel()
    await asyncio.gather(replacement_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_heartbeat_restart(monkeypatch):
    calls = 0

    async def failed_heartbeat(**_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("heartbeat write failed")

    monkeypatch.setattr("gateway.run.loop_heartbeat_forever", failed_heartbeat)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._background_tasks = set()
    runner._gateway_started_at = 123.0
    runner._loop_heartbeat_task = None
    runner._loop_heartbeat_restart_handle = None
    runner._loop_floor_timer_handle = None
    runner._loop_liveness_watchdog = None
    runner._draining = False
    runner._shutdown_event = asyncio.Event()

    runner._start_loop_heartbeat_task()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    restart_handle = runner._loop_heartbeat_restart_handle
    assert restart_handle is not None

    runner._stop_loop_liveness_guards()

    assert restart_handle.cancelled()
    await asyncio.sleep(1.1)
    assert calls == 1
