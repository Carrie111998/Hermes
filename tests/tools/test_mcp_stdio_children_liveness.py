"""`_stdio_children_dead()` must only be True when every child has exited.

The fast-fail gate at the `call_tool` site (#81995) turns a True here into an
immediate ``TimeoutError`` for the whole call, so an inverted answer does not
degrade gracefully — it takes out every tool call on a perfectly healthy stdio
MCP server before the RPC is even issued.

The liveness probe was rewritten to ``psutil.pid_exists`` (#85125 CI) because
``os.kill(pid, 0)`` cannot answer the question on Windows: an exited-but-
unreaped child raises nothing, and a vanished PID raises ``OSError`` WinError 87
rather than ``ProcessLookupError``. The rewrite kept the "dead" branch but
returned True from the "alive" branch, stranding the correct ``return False``
below it as unreachable code — so the helper answered True unconditionally.
Nothing covered it. These tests do.
"""

import subprocess
import sys
import time

import psutil
import pytest

from tools.mcp_tool import MCPServerTask


def _stdio_task(pids, config=None):
    """A bare MCPServerTask carrying just what the liveness probe reads."""
    task = object.__new__(MCPServerTask)
    # `_is_http()` is `"url" in self._config`; no url == stdio transport.
    task._config = {} if config is None else config
    task._stdio_child_pids = set(pids)
    return task


class TestStdioChildrenDead:
    def test_live_child_is_not_reported_dead(self, monkeypatch):
        monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)

        assert _stdio_task([4242])._stdio_children_dead() is False

    def test_exited_child_is_reported_dead(self, monkeypatch):
        monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)

        assert _stdio_task([4242])._stdio_children_dead() is True

    def test_one_live_child_among_dead_ones_keeps_the_transport_alive(self, monkeypatch):
        # "every child has exited" — a single survivor means the answer is False.
        monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid == 43)

        assert _stdio_task([41, 42, 43])._stdio_children_dead() is False

    def test_all_children_exited_is_dead(self, monkeypatch):
        monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)

        assert _stdio_task([41, 42, 43])._stdio_children_dead() is True

    @pytest.mark.parametrize("pids", [set(), None])
    def test_unknown_without_captured_pids(self, pids):
        # Unknown must not fail fast.
        assert _stdio_task(pids or [])._stdio_children_dead() is False

    def test_http_transport_never_fails_fast(self, monkeypatch):
        monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)
        task = _stdio_task([4242], config={"url": "https://example.invalid/mcp"})

        assert task._stdio_children_dead() is False


class TestStdioChildrenDeadAgainstRealProcesses:
    """The same contract against a real OS process, no monkeypatching."""

    def test_real_child_flips_from_alive_to_dead_when_killed(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            deadline = time.monotonic() + 5
            while not psutil.pid_exists(child.pid) and time.monotonic() < deadline:
                time.sleep(0.05)

            assert _stdio_task([child.pid])._stdio_children_dead() is False
        finally:
            child.kill()
            child.wait()

        # psutil reports a reaped PID as gone; give the OS a moment on slower CI.
        deadline = time.monotonic() + 5
        while psutil.pid_exists(child.pid) and time.monotonic() < deadline:
            time.sleep(0.05)

        assert _stdio_task([child.pid])._stdio_children_dead() is True
