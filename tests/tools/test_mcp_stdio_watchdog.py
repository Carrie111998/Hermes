"""Contract tests for the direct POSIX stdio MCP child watchdog."""

import os
import subprocess
import sys

import pytest

from tools import mcp_stdio_watchdog, mcp_tool


def test_is_orphaned_is_false_while_direct_parent_is_unchanged():
    original_ppid = 1234

    assert mcp_stdio_watchdog._is_orphaned(
        original_ppid,
        getppid=lambda: original_ppid,
    ) is False


@pytest.mark.skipif(os.name != "posix", reason="watchdog wrapping is POSIX-only")
def test_wrap_command_uses_stable_parent_pid_and_preserves_command_tail():
    # ponytail: single shared watchdog replaces N per-server wrappers (3→1)
    # old per-server wrapper test replaced: now _wrap returns the original
    # command and ensures a single shared watchdog process exists.
    import tools.mcp_tool as m
    # reset singleton for isolation
    orig_proc = m._single_watchdog_proc
    orig_file = m._single_watchdog_pgid_file
    m._single_watchdog_proc = None
    m._single_watchdog_pgid_file = None
    count = 0
    orig_popen = subprocess.Popen

    class FakePopen:
        def __init__(self, *a, **kw):
            nonlocal count
            count += 1
            self.args = a
            self.kw = kw

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    m.subprocess.Popen = FakePopen
    try:
        command = "/opt/hermes/bin/mcp-server"
        command_args = ["--label", "value with spaces", "--", "literal-tail"]

        # 3 servers should share 1 watchdog process (ponytail 3→1)
        for i in range(3):
            wrapped_command, wrapped_args = m._wrap_command_with_watchdog(
                f"{command}{i}",
                command_args,
            )
            assert wrapped_command == f"{command}{i}"
            assert wrapped_args == command_args

        assert count == 1, f"expected 1 watchdog for 3 servers, got {count}"
        # singleton file should exist and be empty initially
        assert m._single_watchdog_pgid_file is not None
        assert m._single_watchdog_pgid_file.exists()
    finally:
        m.subprocess.Popen = orig_popen
        try:
            m._stop_single_watchdog()
        except Exception:
            pass
        # restore (best-effort)
        m._single_watchdog_proc = orig_proc
        m._single_watchdog_pgid_file = orig_file
