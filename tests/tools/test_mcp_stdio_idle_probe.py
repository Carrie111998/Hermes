"""Regression tests for stdio MCP idle liveness probing."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tools import mcp_tool
from tools.mcp_tool import MCPServerTask


async def _run_one_idle_cycle(task: MCPServerTask, monkeypatch):
    real_wait = asyncio.wait
    cycles = {"count": 0}

    async def fake_wait(tasks, timeout=None, return_when=None):
        cycles["count"] += 1
        if cycles["count"] == 1:
            return set(), set(tasks)
        task._shutdown_event.set()
        return await real_wait(
            tasks,
            timeout=0.5,
            return_when=return_when or asyncio.FIRST_COMPLETED,
        )

    monkeypatch.setattr(mcp_tool.asyncio, "wait", fake_wait)
    return await task._wait_for_lifecycle_event()


def test_alive_stdio_uses_process_liveness_without_protocol_ping(monkeypatch):
    task = MCPServerTask("stdio-alive")
    task._config = {"command": "fake", "keepalive_interval": 0.01}
    task.session = SimpleNamespace(send_ping=AsyncMock(), list_tools=AsyncMock())
    probes = []
    monkeypatch.setattr(
        MCPServerTask,
        "_stdio_children_dead",
        lambda self: probes.append(self) or False,
    )
    monkeypatch.setattr(mcp_tool, "_MIN_KEEPALIVE_INTERVAL", 0.01)

    assert asyncio.run(_run_one_idle_cycle(task, monkeypatch)) == "shutdown"
    assert probes == [task]
    task.session.send_ping.assert_not_awaited()
    task.session.list_tools.assert_not_awaited()


def test_dead_stdio_idle_probe_retires_session_and_reconnects(monkeypatch):
    task = MCPServerTask("stdio-dead")
    task._config = {"command": "fake", "keepalive_interval": 0.01}
    session = SimpleNamespace(send_ping=AsyncMock(), list_tools=AsyncMock())
    task.session = session
    task._ready.set()
    probes = []
    monkeypatch.setattr(
        MCPServerTask,
        "_stdio_children_dead",
        lambda self: probes.append(self) or True,
    )
    monkeypatch.setattr(mcp_tool, "_MIN_KEEPALIVE_INTERVAL", 0.01)

    assert asyncio.run(_run_one_idle_cycle(task, monkeypatch)) == "reconnect"
    assert probes == [task]
    assert task.session is None
    assert not task._ready.is_set()
    session.send_ping.assert_not_awaited()
    session.list_tools.assert_not_awaited()
