"""Regression tests for ``MCPServerTask._stdio_children_dead`` (#94637).

Commit ``786f37071`` inverted the liveness check: the function returned
``True`` ("every stdio child has exited") the moment it found a single
LIVE pid, so the #81995 pre-call fast-fail raised
``TimeoutError: MCP stdio subprocess for '<server>' has exited`` for every
stdio ``tools/call`` even while the subprocess was demonstrably healthy.
These tests pin the corrected polarity and the documented best-effort
contract ("returns False — unknown, don't fail fast — otherwise").
"""

import builtins
import os
import subprocess
import sys

from tools.mcp_tool import MCPServerTask


def _task_with_pids(pids):
    task = MCPServerTask("liveness-test")
    task._stdio_child_pids = set(pids)
    return task


class TestStdioChildrenDead:
    def test_live_child_is_not_dead(self):
        # The pytest process itself is a live tracked pid: must report
        # "not all dead" (pre-fix code returned True → instant fast-fail).
        task = _task_with_pids([os.getpid()])
        assert task._stdio_children_dead() is False

    def test_spawned_live_child_is_not_dead(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        try:
            task = _task_with_pids([proc.pid])
            assert task._stdio_children_dead() is False
        finally:
            proc.kill()
            proc.wait()

    def test_all_dead_reports_dead(self, monkeypatch):
        import psutil

        monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)
        task = _task_with_pids([1, 2, 3])
        assert task._stdio_children_dead() is True

    def test_mixed_alive_and_dead_is_not_all_dead(self, monkeypatch):
        import psutil

        def fake_exists(pid):
            return pid != 424242

        monkeypatch.setattr(psutil, "pid_exists", fake_exists)
        # All tracked pids dead → True.
        assert _task_with_pids([424242])._stdio_children_dead() is True
        # At least one alive → False (pre-fix returned True on the live pid).
        task = _task_with_pids([424242, os.getpid()])
        assert task._stdio_children_dead() is False

    def test_no_pids_is_unknown_not_dead(self):
        assert _task_with_pids([])._stdio_children_dead() is False

    def test_http_server_never_dead(self):
        task = MCPServerTask("http-test")
        task._config = {"url": "http://127.0.0.1:9"}
        task._stdio_child_pids = {os.getpid()}
        assert task._stdio_children_dead() is False

    def test_psutil_missing_is_unknown_not_dead(self, monkeypatch):
        # The pre-fix code ran a bare `import psutil` inside the loop: with
        # psutil unavailable it raised ImportError instead of honoring the
        # "unknown → don't fail fast" contract.
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("psutil unavailable")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        task = _task_with_pids([os.getpid()])
        assert task._stdio_children_dead() is False
