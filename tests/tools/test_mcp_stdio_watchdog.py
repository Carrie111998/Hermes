"""Contract tests for the direct POSIX stdio MCP child watchdog."""

import json
import os
import sys

import pytest

from tools import mcp_stdio_watchdog, mcp_tool


def test_is_orphaned_is_false_while_direct_parent_is_unchanged():
    original_ppid = 1234

    assert mcp_stdio_watchdog._is_orphaned(
        original_ppid,
        getppid=lambda: original_ppid,
    ) is False


def test_is_orphaned_is_true_after_direct_parent_changes():
    assert mcp_stdio_watchdog._is_orphaned(
        1234,
        getppid=lambda: 5678,
    ) is True


@pytest.mark.skipif(os.name != "posix", reason="watchdog wrapping is POSIX-only")
def test_wrap_command_uses_stable_parent_pid_and_preserves_command_tail():
    parent_pid = os.getpid()
    command = "/opt/hermes/bin/mcp-server"
    command_args = ["--label", "value with spaces", "--", "literal-tail"]

    wrapped_command, wrapped_args = mcp_tool._wrap_command_with_watchdog(
        command,
        command_args,
    )

    assert wrapped_command == sys.executable
    assert wrapped_args == [
        os.path.join(os.path.dirname(mcp_tool.__file__), "mcp_stdio_watchdog.py"),
        "--ppid",
        str(parent_pid),
        "--",
        command,
        *command_args,
    ]
    assert "--create-time" not in wrapped_args


@pytest.mark.skipif(os.name != "posix", reason="lock cleanup is POSIX-only")
class TestCleanupStaleMcpRemoteLocks:
    def _write_lock(self, tmp_path, server_dir, name, pid):
        d = tmp_path / server_dir
        d.mkdir(parents=True, exist_ok=True)
        lock_path = d / name
        lock_path.write_text(json.dumps({"pid": pid, "port": 1234, "timestamp": 0}))
        return lock_path

    def test_removes_lock_with_dead_pid(self, tmp_path):
        # A PID essentially guaranteed not to exist.
        dead_pid = 2**30
        lock_path = self._write_lock(
            tmp_path, "mcp-remote-0.1.37", "abc_lock.json", dead_pid
        )

        mcp_stdio_watchdog._cleanup_stale_mcp_remote_locks(str(tmp_path))

        assert not lock_path.exists()

    def test_keeps_lock_with_live_pid(self, tmp_path):
        live_pid = os.getpid()
        lock_path = self._write_lock(
            tmp_path, "mcp-remote-0.1.37", "abc_lock.json", live_pid
        )

        mcp_stdio_watchdog._cleanup_stale_mcp_remote_locks(str(tmp_path))

        assert lock_path.exists()

    def test_ignores_malformed_lock_file(self, tmp_path):
        d = tmp_path / "mcp-remote-0.1.37"
        d.mkdir(parents=True, exist_ok=True)
        lock_path = d / "garbage_lock.json"
        lock_path.write_text("not json")

        # Should not raise.
        mcp_stdio_watchdog._cleanup_stale_mcp_remote_locks(str(tmp_path))

        assert lock_path.exists()

    def test_no_mcp_auth_dir_is_a_noop(self, tmp_path):
        missing = tmp_path / "does-not-exist"

        # Should not raise even though the directory doesn't exist.
        mcp_stdio_watchdog._cleanup_stale_mcp_remote_locks(str(missing))

