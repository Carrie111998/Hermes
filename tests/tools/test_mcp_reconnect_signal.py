"""Tests for the MCPServerTask reconnect signal.

When the OAuth layer cannot recover in-place (e.g., external refresh of a
single-use refresh_token made the SDK's in-memory refresh fail), the tool
handler signals MCPServerTask to tear down the current MCP session and
reconnect with fresh credentials. This file exercises the signal plumbing
in isolation from the full stdio/http transport machinery.
"""
import asyncio
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_reconnect_event_attribute_exists():
    """MCPServerTask has a _reconnect_event alongside _shutdown_event."""
    from tools.mcp_tool import MCPServerTask
    task = MCPServerTask("test")
    assert hasattr(task, "_reconnect_event")
    assert isinstance(task._reconnect_event, asyncio.Event)
    assert not task._reconnect_event.is_set()


@pytest.mark.asyncio
async def test_wait_for_lifecycle_event_shutdown_wins_when_both_set():
    """If both events are set simultaneously, shutdown takes precedence."""
    from tools.mcp_tool import MCPServerTask
    task = MCPServerTask("test")

    task._shutdown_event.set()
    task._reconnect_event.set()
    reason = await task._wait_for_lifecycle_event()
    assert reason == "shutdown"


@pytest.mark.asyncio
async def test_keepalive_failure_with_live_stdio_child_keeps_session(monkeypatch):
    """A keepalive failure on a healthy stdio subprocess (ping non-response)
    must NOT trigger a reconnect/respawn loop. Only when the stdio child has
    actually exited should a keepalive failure tear the session down.
    Regression for #97245."""
    from tools.mcp_tool import MCPServerTask

    def task_with(pids):
        t = MCPServerTask("srv")
        t._stdio_child_pids = set(pids)
        t._is_http = lambda: False
        return t

    with patch("psutil.pid_exists") as pexists:
        # Live child pid → _stdio_children_dead() False → keep session.
        pexists.side_effect = [True]
        assert task_with({111})._keepalive_failure_should_reconnect() is False

        # Child exited → _stdio_children_dead() True → reconnect.
        pexists.side_effect = [False]
        assert task_with({111})._keepalive_failure_should_reconnect() is True

    # HTTP/remote transport → always reconnect (no stdio child to probe).
    http = MCPServerTask("srv")
    http._stdio_child_pids = {111}
    http._is_http = lambda: True
    assert http._keepalive_failure_should_reconnect() is True

    # No tracked child (unknown) → historical behavior, reconnect.
    assert task_with(set())._keepalive_failure_should_reconnect() is True
