"""Regression tests for MCPServerTask._stdio_children_dead (inverted liveness).

A live stdio child must NOT trip the dead-children fast-fail; only when every
tracked child has exited may the call be failed fast. The original logic
returned True (dead) for a live pid, poisoning every stdio MCP server on its
first call after connect.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.mcp_tool import MCPServerTask  # noqa: E402


def _make_task(pids, http=False):
    task = MCPServerTask.__new__(MCPServerTask)
    task._stdio_child_pids = pids
    # _is_http reads self._config: presence of "url" means HTTP transport
    task._config = {"url": "http://example.invalid"} if http else {"command": "true"}
    return task


def test_alive_child_is_not_dead():
    # our own pid is definitely alive
    task = _make_task([os.getpid()])
    assert task._stdio_children_dead() is False


def test_all_children_exited_is_dead():
    # pids from a range that cannot be alive (max pid + spawn-and-reap)
    import subprocess

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    task = _make_task([proc.pid])
    # NOTE: pid could theoretically be recycled between wait() and the check;
    # psutil.pid_exists on a reaped child of this process is reliably False.
    assert task._stdio_children_dead() is True


def test_one_alive_among_dead_is_not_dead():
    import subprocess

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    task = _make_task([proc.pid, os.getpid()])
    assert task._stdio_children_dead() is False


def test_no_pids_unknown_is_not_dead():
    task = _make_task(None)
    assert task._stdio_children_dead() is False


def test_http_transport_is_not_dead():
    task = _make_task([os.getpid()], http=True)
    assert task._stdio_children_dead() is False
