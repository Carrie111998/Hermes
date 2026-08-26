"""Regression tests for MCPServer._stdio_children_dead (#81995 follow-up).

The fast-fail guard added for #81995 asks this predicate whether every stdio
child we spawned has exited. An inverted early return made it answer True
("all dead") as soon as it found the FIRST *live* PID, so every stdio MCP
tools/call was rejected with "subprocess has exited" while the server was
running normally. The trailing `return False` was unreachable dead code.

These tests pin the contract in both directions so the inversion cannot
silently come back.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest


def _make_server(pids, *, is_http=False):
    """Build a bare object exposing the real predicate under test.

    Binding the unbound function onto a stub avoids constructing a full
    MCPServer (which would need a live transport) while still exercising the
    exact production code path.
    """
    from tools.mcp_tool import MCPServerTask

    stub = types.SimpleNamespace()
    stub._stdio_child_pids = pids
    stub._is_http = lambda: is_http
    stub._stdio_children_dead = MCPServerTask._stdio_children_dead.__get__(stub)
    return stub


@pytest.fixture
def fake_psutil(monkeypatch):
    """Install a fake psutil whose pid_exists is driven by a set of live PIDs."""
    live = set()
    module = types.ModuleType("psutil")
    module.pid_exists = lambda pid: pid in live
    monkeypatch.setitem(sys.modules, "psutil", module)
    return live


def test_live_child_is_not_reported_dead(fake_psutil):
    """A single live PID must yield False — this is the #81995 regression."""
    fake_psutil.add(4242)
    server = _make_server([4242])
    assert server._stdio_children_dead() is False


def test_all_children_dead(fake_psutil):
    """Every PID gone -> True, so the caller can fast-fail the RPC."""
    server = _make_server([1, 2, 3])  # fake_psutil is empty: none exist
    assert server._stdio_children_dead() is True


def test_mixed_children_with_one_alive(fake_psutil):
    """Dead PIDs are skipped; one survivor keeps the transport usable."""
    fake_psutil.add(777)
    server = _make_server([111, 222, 777])
    assert server._stdio_children_dead() is False


def test_dead_pid_before_live_pid_still_alive(fake_psutil):
    """Ordering must not matter — a later live PID still wins."""
    fake_psutil.add(999)
    server = _make_server([100, 999])
    assert server._stdio_children_dead() is False


def test_no_pids_is_unknown_not_dead():
    """Without captured PIDs the answer is 'unknown' -> never fast-fail."""
    server = _make_server([])
    assert server._stdio_children_dead() is False


def test_http_transport_never_fast_fails():
    """HTTP transports have no stdio children; must not fast-fail."""
    server = _make_server([1234], is_http=True)
    assert server._stdio_children_dead() is False
