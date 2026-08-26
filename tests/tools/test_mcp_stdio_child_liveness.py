"""Regression tests for MCP stdio child liveness races."""

import asyncio
import json
from collections.abc import Awaitable
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from tools import mcp_tool


class _TextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _ToolResult:
    isError = False
    structuredContent = None
    meta = None

    def __init__(self, text: str):
        self.content = [_TextBlock(text)]


def _run_in_fresh_loop(coro_or_factory, timeout=30):
    candidate = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    coro = cast(Awaitable[Any], candidate)
    loop = asyncio.new_event_loop()
    try:
        async def install_lock_and_run():
            for server in list(mcp_tool._servers.values()):
                if getattr(server, "_rpc_lock", None) is None:
                    server._rpc_lock = asyncio.Lock()
            return await coro

        return loop.run_until_complete(
            install_lock_and_run()
        )
    finally:
        loop.close()


def test_tool_call_constructs_one_child_watcher_awaitable():
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_ToolResult("ok"))
    watcher_calls = 0

    def watch_children():
        nonlocal watcher_calls
        watcher_calls += 1

        async def wait_forever():
            await asyncio.Event().wait()

        return wait_forever()

    server = SimpleNamespace(
        session=session,
        _rpc_lock=None,
        _watch_stdio_children=watch_children,
    )
    with (
        patch.dict(mcp_tool._servers, {"stdio-liveness": server}),
        patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_in_fresh_loop),
    ):
        handler = mcp_tool._make_tool_handler("stdio-liveness", "read_only", 30.0)
        result = json.loads(handler({}))

    assert result == {"result": "ok"}
    assert watcher_calls == 1


def test_dead_child_pre_call_retires_session_and_next_call_uses_fresh_session(
    monkeypatch,
):
    old_session = MagicMock()
    old_session.call_tool = AsyncMock(return_value=_ToolResult("stale"))
    server = mcp_tool.MCPServerTask("stdio-recovery")
    server.session = old_session
    cast(Any, server)._rpc_lock = None
    server._stdio_child_pids = {101}
    monkeypatch.setattr("psutil.pid_exists", lambda _pid: False)

    with (
        patch.dict(mcp_tool._servers, {"stdio-recovery": server}),
        patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_in_fresh_loop),
    ):
        handler = mcp_tool._make_tool_handler("stdio-recovery", "read_only", 30.0)
        first = json.loads(handler({}))

        assert "exited" in first["error"]
        assert server.session is None
        assert server._reconnect_event.is_set()
        assert old_session.call_tool.await_count == 0

        fresh_session = MagicMock()
        fresh_session.call_tool = AsyncMock(return_value=_ToolResult("fresh"))
        server.session = fresh_session
        server._stdio_child_pids = set()
        server._reconnect_event.clear()
        server._ready.set()

        second = json.loads(handler({}))

    assert second == {"result": "fresh"}
    assert fresh_session.call_tool.await_count == 1
    assert old_session.call_tool.await_count == 0


def test_dead_child_mid_call_retires_session_and_requests_reconnect(monkeypatch):
    async def never_finishes():
        await asyncio.Event().wait()

    session = MagicMock()
    session.call_tool = MagicMock(side_effect=lambda *_args, **_kwargs: never_finishes())
    server = mcp_tool.MCPServerTask("stdio-mid-call")
    server.session = session
    cast(Any, server)._rpc_lock = None
    states = iter((False, True))
    monkeypatch.setattr(
        mcp_tool.MCPServerTask,
        "_stdio_children_dead",
        lambda _self: next(states, True),
    )

    with (
        patch.dict(mcp_tool._servers, {"stdio-mid-call": server}),
        patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_in_fresh_loop),
    ):
        handler = mcp_tool._make_tool_handler("stdio-mid-call", "read_only", 30.0)
        result = json.loads(handler({}))

    assert "exited mid-call" in result["error"]
    assert server.session is None
    assert server._reconnect_event.is_set()
    assert session.call_tool.call_count == 1


def test_parallel_stdio_spawn_capture_keeps_each_server_pid_owned(monkeypatch):
    active_pids: set[int] = set()
    captured: dict[int, set[int]] = {}

    class FakeStdioClient:
        def __init__(self, pid: int):
            self.pid = pid

        async def __aenter__(self):
            active_pids.add(self.pid)
            await asyncio.sleep(0.02)
            return (object(), object())

        async def __aexit__(self, *_exc):
            active_pids.discard(self.pid)

    monkeypatch.setattr(
        mcp_tool,
        "_snapshot_child_pids",
        lambda: set(active_pids),
    )
    monkeypatch.setattr(mcp_tool, "_filter_mcp_children", lambda pids: pids)

    async def capture(pid: int):
        async with mcp_tool._tracked_stdio_spawn(FakeStdioClient(pid)) as (
            _streams,
            owned_pids,
        ):
            captured[pid] = owned_pids
            await asyncio.sleep(0.04)

    async def run_both():
        await asyncio.gather(capture(101), capture(202))

    asyncio.run(run_both())

    assert captured == {101: {101}, 202: {202}}


def test_stdio_lifecycle_uses_process_liveness_without_protocol_ping(monkeypatch):
    server = mcp_tool.MCPServerTask("stdio-no-ping")
    session = MagicMock()
    session.send_ping = AsyncMock()
    server.session = session
    server._config = {"command": "fake", "keepalive_interval": 0.01}
    monkeypatch.setattr(mcp_tool, "_MIN_KEEPALIVE_INTERVAL", 0.01)
    monkeypatch.setattr(
        mcp_tool.MCPServerTask,
        "_stdio_children_dead",
        lambda _self: False,
    )

    async def scenario():
        waiter = asyncio.create_task(server._wait_for_lifecycle_event())
        await asyncio.sleep(0.04)
        server._shutdown_event.set()
        return await waiter

    assert asyncio.run(scenario()) == "shutdown"
    assert session.send_ping.await_count == 0
