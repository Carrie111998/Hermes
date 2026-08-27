"""Regression tests for concurrent stdio MCP PID attribution."""

import asyncio

from tools import mcp_tool


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

    # Hermes tears down and recreates its MCP event loop. The spawn guard must
    # remain usable after the loop that first contended on it has closed.
    captured.clear()
    asyncio.run(run_both())
    assert captured == {101: {101}, 202: {202}}
