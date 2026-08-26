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
