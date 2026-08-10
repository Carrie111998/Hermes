"""MCP startup cleanup regressions for failed and cancelled connections."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from tools.mcp_tool import MCPServerTask


@pytest.mark.asyncio
async def test_failed_start_awaits_parked_owner_task_cleanup():
    server = MCPServerTask("broken")
    cleanup_finished = asyncio.Event()
    failure = RuntimeError("connect failed")

    async def failed_run(self, _config):
        try:
            self._error = failure
            self._ready.set()
            await self._wait_for_reconnect_or_shutdown()
        finally:
            await asyncio.sleep(0)
            cleanup_finished.set()

    with patch.object(MCPServerTask, "run", failed_run):
        with pytest.raises(RuntimeError, match="connect failed"):
            await server.start({})

    assert cleanup_finished.is_set()
    assert server._task is not None
    assert server._task.done()


@pytest.mark.asyncio
async def test_cancelled_start_awaits_owner_task_cleanup():
    server = MCPServerTask("slow")
    owner_started = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def parked_run(self, _config):
        owner_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleanup_finished.set()

    with patch.object(MCPServerTask, "run", parked_run):
        start_task = asyncio.create_task(server.start({}))
        await owner_started.wait()
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task

    assert cleanup_finished.is_set()
    assert server._task is not None
    assert server._task.done()


@pytest.mark.asyncio
async def test_cancelled_start_preserves_cancellation_when_owner_cleanup_raises():
    server = MCPServerTask("broken-cleanup")
    owner_started = asyncio.Event()

    async def cleanup_raises(self, _config):
        owner_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise RuntimeError("owner cleanup failed")

    with patch.object(MCPServerTask, "run", cleanup_raises):
        start_task = asyncio.create_task(server.start({}))
        await owner_started.wait()
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task

    assert server._task is not None
    assert server._task.done()


@pytest.mark.asyncio
async def test_failed_start_preserves_connection_error_when_shutdown_raises():
    server = MCPServerTask("broken-shutdown")
    failure = RuntimeError("original connection failure")

    async def failed_run(self, _config):
        self._error = failure
        self._ready.set()

    with (
        patch.object(MCPServerTask, "run", failed_run),
        patch.object(
            MCPServerTask,
            "shutdown",
            AsyncMock(side_effect=RuntimeError("secondary shutdown failure")),
        ),
    ):
        with pytest.raises(RuntimeError, match="original connection failure") as exc:
            await server.start({})

    assert exc.value is failure


@pytest.mark.asyncio
async def test_claimed_failed_start_preserves_parked_owner_for_adoption():
    server = MCPServerTask("claimed-park")
    failure = RuntimeError("connect failed")

    async def failed_run(self, _config):
        self._error = failure
        self._ready.set()
        await self._wait_for_reconnect_or_shutdown()

    with patch.object(MCPServerTask, "run", failed_run):
        with pytest.raises(RuntimeError, match="connect failed"):
            await server.start({}, shutdown_on_error=False)

    assert server._task is not None
    assert not server._task.done()
    try:
        await server.shutdown()
    finally:
        assert server._task.done()
