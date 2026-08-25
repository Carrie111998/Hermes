"""Regression tests for MCPServerTask._stdio_children_dead (#81995 follow-up).

The fast-fail path added for #81995 must only trip when EVERY tracked stdio
child has exited. An inverted branch made the helper return ``True`` (all
dead) as soon as it saw the first *live* pid, so every stdio MCP call on a
perfectly healthy server was rejected with::

    MCP stdio subprocess for '<name>' has exited; failing the call fast

and ``_watch_stdio_children`` returned immediately, cancelling in-flight RPCs.

These tests pin the contract in both directions so the branch cannot invert
again: a live child means "not dead", and only an all-dead set fast-fails.
"""

import asyncio
import os
import subprocess

import pytest

from tools.mcp_tool import MCPServerTask


def _stdio_task(pids):
    task = MCPServerTask("regression-server")
    task._config = {"command": "/bin/true", "args": []}  # stdio, not http
    task._stdio_child_pids = set(pids)
    return task


@pytest.fixture(scope="module")
def dead_pid() -> int:
    """A pid that really existed and really exited.

    A synthetic out-of-range constant is not usable here: ``psutil.pid_exists``
    calls ``os.kill(pid, 0)``, which raises ``OverflowError`` above the platform
    pid range instead of reporting "gone".
    """
    proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    proc.wait()
    return proc.pid


def test_live_child_is_not_reported_dead():
    """One live pid must mean 'not dead' — this is the inversion that broke."""
    task = _stdio_task([os.getpid()])
    assert task._stdio_children_dead() is False


def test_all_dead_children_are_reported_dead(dead_pid):
    task = _stdio_task([dead_pid])
    assert task._stdio_children_dead() is True


def test_mixed_children_are_not_reported_dead(dead_pid):
    """A dead sibling must not condemn a still-serving child."""
    task = _stdio_task([dead_pid, os.getpid()])
    assert task._stdio_children_dead() is False


def test_unknown_pids_do_not_fast_fail():
    """No captured pids → unknown → never fast-fail."""
    task = _stdio_task([])
    assert task._stdio_children_dead() is False


def test_http_transport_never_fast_fails(dead_pid):
    task = MCPServerTask("http-server")
    task._config = {"url": "https://example.invalid/mcp"}
    task._stdio_child_pids = {dead_pid}
    assert task._stdio_children_dead() is False


def test_watcher_does_not_resolve_while_child_is_alive():
    """_watch_stdio_children must keep polling, not cancel a healthy RPC."""

    async def _run():
        task = _stdio_task([os.getpid()])
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task._watch_stdio_children(), timeout=0.6)

    asyncio.run(_run())


def test_watcher_resolves_once_every_child_is_gone(dead_pid):
    async def _run():
        task = _stdio_task([dead_pid])
        await asyncio.wait_for(task._watch_stdio_children(), timeout=2.0)

    asyncio.run(_run())
