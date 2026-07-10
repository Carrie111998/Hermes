"""Regression test for the client-side MCP discovery hang (observed 2026-07-10).

A Mission Control dashboard lost its ``mempalace`` MCP connection at 02:59
under GIL pressure and never recovered on its own, sitting tool-less for ~6
hours until a human restart. The mempalace server (:7482) was provably healthy
throughout — synthetic fresh handshakes returned all 25 tools in single-digit
ms. The bug was entirely client-side.

Sequence: the reconnect budget churned ("unhandled errors in a TaskGroup"),
then one attempt got *past* the handshake — ``Received session ID ... Negotiated
protocol version`` — but the server->client GET (SSE) stream dropped immediately
("GET stream disconnected, reconnecting in 1000ms") and then went silent.

Root cause: ``session.initialize()`` is bounded by ``connect_timeout`` (the
#59349 fix), but the *next* call — ``_discover_tools()`` → ``list_tools()`` —
was not. On a half-open transport the discovery response never arrives, so the
call awaits forever. ``run()`` wedges *inside* the transport context managers:
it never returns and never raises, so the reconnect/park state machine — and
its periodic self-probe — is never reached. The server stays parked-less and
tool-less for the life of the process.

The fix bounds tool discovery the same way the handshake is bounded, so a
stalled ``list_tools`` fails instead of hanging. That failure unwinds the
transport and re-enters ``run()``'s reconnect loop, which eventually parks with
a self-probe and self-heals once event-loop pressure clears.

This test drives the *real* ``_run_stdio`` with a fake transport whose
``initialize()`` succeeds but whose ``list_tools()`` hangs, and asserts the
connect is bounded by ``connect_timeout`` rather than blocking forever. It is
fully hermetic — no real subprocess, no network.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

pytest.importorskip("mcp")


class _HangingDiscoverySession:
    """Stand-in ClientSession: handshake completes, discovery never does.

    Models the observed failure — the session ID is assigned and the protocol
    is negotiated (``initialize`` returns), but the transport is half-open so
    the ``tools/list`` response never lands.
    """

    async def initialize(self):
        # Returning None makes ``_advertises_tools()`` fall back to True
        # (legacy: no capability info -> call list_tools), which is what we
        # want to exercise the discovery path.
        return None

    async def list_tools(self):
        await asyncio.sleep(3600)


class _FakeAsyncCM:
    """Minimal async context manager yielding a fixed value; spawns nothing."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *_exc):
        return False


def _fake_stdio_client(*_args, **_kwargs):
    # `async with stdio_client(...) as (read, write)` — no subprocess spawned.
    return _FakeAsyncCM((object(), object()))


def _fake_client_session(*_args, **_kwargs):
    # `async with ClientSession(...) as session` -> discovery hangs.
    return _FakeAsyncCM(_HangingDiscoverySession())


class TestDiscoveryTimeout:
    def test_hanging_list_tools_is_bounded_not_wedged(self):
        """A server that hangs at ``tools/list`` *after* a successful
        handshake must fail within ``connect_timeout`` — not block the run
        coroutine forever (2026-07-10 dashboard mempalace wedge)."""
        from tools import mcp_tool

        server = mcp_tool.MCPServerTask("discover-guard")
        config = {"command": "fake-mcp", "args": [], "connect_timeout": 0.2}

        async def drive():
            with patch.object(mcp_tool, "stdio_client", _fake_stdio_client), \
                 patch.object(mcp_tool, "ClientSession", _fake_client_session), \
                 patch.object(mcp_tool, "_resolve_stdio_command", lambda c, e: (c, e)), \
                 patch.object(mcp_tool, "_write_stderr_log_header", lambda *_a, **_k: None), \
                 patch.object(mcp_tool, "_get_mcp_stderr_log", lambda: None), \
                 patch("tools.osv_check.check_package_for_malware",
                       lambda *_a, **_k: None):
                # Prime _config the way run() does before calling _run_stdio,
                # so _discover_tools can read connect_timeout from it.
                server._config = config
                start = time.monotonic()
                # The outer 5s guard exists ONLY so a regression can't hang the
                # whole suite. With the fix, the inner connect_timeout (0.2s)
                # fires first; the elapsed assertion below is what actually
                # distinguishes "bounded" (fixed) from "wedged" (regressed).
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(server._run_stdio(config), timeout=5.0)
                return time.monotonic() - start

        elapsed = asyncio.run(drive())
        assert elapsed < 2.0, (
            f"_run_stdio blocked {elapsed:.1f}s on a hanging list_tools() — the "
            f"connect_timeout ({config['connect_timeout']}s) bound was not applied "
            f"to tool discovery; the client-side reconnect-wedge has regressed."
        )
