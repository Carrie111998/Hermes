"""Tests for the parked self-probe backoff ladder.

A parked server self-probes on a timer (its tools are deregistered, so
nothing else can revive it). The original fixed 300 s cadence meant a
server parked on a permanent-looking failure — e.g. a stdio binary
dying at startup without its credentials — crash-looped forever: every
probe spawns the process, it exits before the MCP handshake, and 5
minutes later the cycle repeats. The interval must now double after
each failed probe (capped), reset when a session proves healthy, and
never delay an explicit reconnect.
"""

import asyncio

import pytest

from tools import mcp_tool
from tools.mcp_tool import MCPServerTask


class TestParkedProbeInterval:
    """_parked_probe_interval ladder maths."""

    def test_base_interval_initially(self):
        server = MCPServerTask("srv")
        assert server._parked_probe_streak == 0
        assert server._parked_probe_interval() == mcp_tool._PARKED_RETRY_INTERVAL

    def test_interval_doubles_per_failed_probe(self):
        server = MCPServerTask("srv")
        server._parked_probe_streak = 3
        expected = mcp_tool._PARKED_RETRY_INTERVAL * (2 ** 3)
        assert server._parked_probe_interval() == expected

    def test_interval_capped(self):
        server = MCPServerTask("srv")
        server._parked_probe_streak = 99
        assert server._parked_probe_interval() == mcp_tool._PARKED_RETRY_INTERVAL_MAX

    def test_cap_constant_sane(self):
        # The cap must stay above the base, otherwise the ladder is a no-op.
        assert mcp_tool._PARKED_RETRY_INTERVAL_MAX > mcp_tool._PARKED_RETRY_INTERVAL


class TestParkedProbeStreakLifecycle:
    """The streak grows on timed self-probe wakes and resets on health."""

    def test_timed_wake_increments_streak(self):
        """A self-probe (timeout elapsed, no explicit event) bumps the streak."""
        async def _scenario():
            server = MCPServerTask("srv")
            # Tiny timeout so the timed wake fires immediately.
            result = await server._wait_for_reconnect_or_shutdown(timeout=0.01)
            return server, result

        server, result = asyncio.run(_scenario())
        assert result == "reconnect"
        assert server._parked_probe_streak == 1

    def test_explicit_reconnect_leaves_streak_alone(self):
        """An explicit _reconnect_event wake must not grow the backoff."""
        async def _scenario():
            server = MCPServerTask("srv")
            server._parked_probe_streak = 2
            server._reconnect_event.set()
            # Long timeout — the explicit event must win the race.
            result = await server._wait_for_reconnect_or_shutdown(timeout=5)
            return server, result

        server, result = asyncio.run(_scenario())
        assert result == "reconnect"
        assert server._parked_probe_streak == 2

    def test_shutdown_wake_leaves_streak_alone(self):
        async def _scenario():
            server = MCPServerTask("srv")
            server._parked_probe_streak = 1
            server._shutdown_event.set()
            result = await server._wait_for_reconnect_or_shutdown(timeout=5)
            return server, result

        server, result = asyncio.run(_scenario())
        assert result == "shutdown"
        assert server._parked_probe_streak == 1

    def test_streak_resets_when_session_proves_healthy(self):
        server = MCPServerTask("srv")
        server._parked_probe_streak = 5
        server._mark_session_proven()
        assert server._parked_probe_streak == 0
