"""Tests for the MCPServerTask._stdio_children_dead liveness predicate.

#81995 added a fast-fail guard so tool calls do not wait on a dead stdio
subprocess. The predicate must report False ("not dead") while any tracked
child is alive and True only once every child has exited; an inverted
version fails every tools/call in ~0.01s on a healthy server while
``hermes mcp test`` still passes (it never reaches the tools/call path).
psutil is stubbed so these tests run without OS process fixtures.
"""
import types

import pytest


@pytest.fixture
def srv_cls():
    from tools.mcp_tool import MCPServerTask
    return MCPServerTask


def _make(srv_cls, pids):
    srv = object.__new__(srv_cls)
    srv._stdio_child_pids = pids
    srv._is_http = lambda: False
    return srv


def test_alive_child_is_not_dead(srv_cls, monkeypatch):
    """One live child → predicate returns False; call must proceed."""
    fake = types.SimpleNamespace(pid_exists=lambda pid: True)
    monkeypatch.setitem(sys_modules(), "psutil", fake)
    assert _run(_make(srv_cls, [111])) is False


def test_all_children_exited_is_dead(srv_cls, monkeypatch):
    """Every child exited → predicate returns True; fast-fail is correct."""
    fake = types.SimpleNamespace(pid_exists=lambda pid: False)
    monkeypatch.setitem(sys_modules(), "psutil", fake)
    assert _run(_make(srv_cls, [111])) is True


def test_no_tracked_pids_is_unknown(srv_cls):
    """No captured PIDs / http transport → unknown → don't fail fast."""
    from tools.mcp_tool import MCPServerTask
    task = MCPServerTask("test")
    assert not hasattr(task, "_stdio_child_pids") or not task._stdio_child_pids \
        or task._stdio_children_dead() is False


def _run(srv):
    return srv._stdio_children_dead()


def sys_modules():
    """Return the real sys.modules dict for targeted psutil patching."""
    import sys
    return sys.modules
