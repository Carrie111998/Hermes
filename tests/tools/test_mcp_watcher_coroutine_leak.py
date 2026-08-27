"""Regression tests for the _watch_stdio_children coroutine leak (#95938).

The fast-fail race (#81995) used to call ``_watch_children()`` once inside
``inspect.isawaitable(...)`` — creating a coroutine that was immediately
discarded — and then a SECOND time for ``asyncio.ensure_future(...)``. Every
fast-fail-raced MCP call therefore leaked one never-awaited coroutine and
emitted ``RuntimeWarning: coroutine MCPServerTask._watch_stdio_children was
never awaited``.

The fix creates the watcher awaitable once (after the cheap ``_call_coro``
coroutine check, so no watcher coroutine is created at all when it could
never be scheduled) and schedules that same object.
"""

import asyncio
from types import SimpleNamespace

import pytest

import tools.mcp_tool as mcp_tool


class _StubSession:
    def __init__(self, *, hang=False):
        self._hang = hang

    async def call_tool(self, name, arguments=None):
        if self._hang:
            await asyncio.sleep(30)
        return SimpleNamespace(content=[SimpleNamespace(text="ok")], is_error=False)


class _StubServer:
    """Minimal stdio-server double for the fast-fail race path."""

    def __init__(self, *, watcher_returns_fast=False):
        self.session = _StubSession(hang=watcher_returns_fast)
        self._rpc_lock = asyncio.Lock()
        self._watcher_returns_fast = watcher_returns_fast
        # Counts coroutine CREATIONS (the factory below), not executions:
        # a watcher coroutine that is created and then discarded never runs
        # its body, so only the factory can see the leak (#95938).
        self.watcher_calls = 0

    def _stdio_children_dead(self):
        return False

    def _watch_stdio_children(self):
        self.watcher_calls += 1
        return self._watcher_coro()

    async def _watcher_coro(self):
        if self._watcher_returns_fast:
            return
        await asyncio.sleep(30)


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setattr(mcp_tool, "_trust_gate_check", lambda *a, **k: None)
    monkeypatch.setattr(mcp_tool, "_server_error_counts", {})
    monkeypatch.setattr(mcp_tool, "_server_breaker_opened_at", {})
    monkeypatch.setattr(
        mcp_tool, "_run_on_mcp_loop", lambda fn, timeout: asyncio.run(fn())
    )
    return mcp_tool._make_tool_handler("stub", "echo", 5.0)


def _install_server(monkeypatch, server):
    monkeypatch.setattr(
        mcp_tool, "_get_connected_server_for_call", lambda name: server
    )


class TestWatcherCoroutineLeak:
    def test_raced_call_creates_watcher_coroutine_once(self, handler, monkeypatch):
        """A fast-fail-raced call must create exactly one watcher coroutine.

        The orphaned coroutine's RuntimeWarning ("was never awaited") fires
        from ``coroutine.__del__``, where warnings can't be turned into
        errors — so the observable red/green signal is the CREATION count:
        the stub's watcher factory counts every ``_watch_children()``
        invocation, and the pre-fix code called it twice (once inside
        ``inspect.isawaitable``, once for ``ensure_future``) while scheduling
        only the second.
        """
        server = _StubServer()
        _install_server(monkeypatch, server)

        result = handler({})

        assert '"result": "ok"' in result
        assert server.watcher_calls == 1

    def test_fast_fail_semantics_unchanged(self, handler, monkeypatch):
        """A dead subprocess still fails the call fast after the refactor."""
        server = _StubServer(watcher_returns_fast=True)
        _install_server(monkeypatch, server)

        result = handler({})

        assert "exited mid-call; failing the call fast" in result

    def test_non_coroutine_call_coro_creates_no_watcher(self, handler, monkeypatch):
        """The cheap ``iscoroutine(_call_coro)`` gate must run first.

        A stubbed (MagicMock-style) session returning a non-coroutine used to
        still create the watcher coroutine inside isawaitable() before the
        third condition discarded it. With the gate first, no watcher
        coroutine is created at all.
        """
        server = _StubServer()
        server.session = SimpleNamespace(call_tool=lambda *a, **k: "plain")
        _install_server(monkeypatch, server)

        result = handler({})

        assert server.watcher_calls == 0
