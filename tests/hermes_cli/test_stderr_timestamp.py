"""Tests for hermes_cli.stderr_timestamp."""

import re
import sys

from hermes_cli import stderr_timestamp


def test_main_timestamps_each_stderr_line(tmp_path):
    log_path = tmp_path / "gateway.error.log"
    code = (
        "import sys\n"
        "sys.stderr.write('first failure\\n')\n"
        "sys.stderr.write('second failure without newline\\n')\n"
        "sys.stderr.write('2026-07-15 12:34:56,789 already timestamped')\n"
        "sys.exit(7)\n"
    )

    rc = stderr_timestamp.main(
        [
            "--error-log",
            str(log_path),
            "--",
            sys.executable,
            "-c",
            code,
        ]
    )

    assert rc == 7
    lines = log_path.read_text(encoding="utf-8").splitlines()
    timestamp = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}"
    assert len(lines) == 3
    assert re.fullmatch(f"{timestamp} first failure", lines[0])
    assert re.fullmatch(f"{timestamp} second failure without newline", lines[1])
    assert lines[2] == "2026-07-15 12:34:56,789 already timestamped"


def test_child_env_propagates_supervisor_marker_for_launchd_child(monkeypatch):
    # launchd's direct child: PPID 1 and a real job label in XPC_SERVICE_NAME.
    monkeypatch.setattr(stderr_timestamp.os, "getppid", lambda: 1)
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.hermes.gateway")

    env = stderr_timestamp._child_env()

    assert env["HERMES_GATEWAY_EXTERNAL_SUPERVISOR"] == "1"
    assert env["XPC_SERVICE_NAME"] == "ai.hermes.gateway"


def test_child_env_no_marker_when_not_launchd_direct_child(monkeypatch):
    # Detached-fallback path: wrapper spawned by the CLI, not by launchd.
    monkeypatch.setattr(stderr_timestamp.os, "getppid", lambda: 4242)
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.hermes.gateway")

    assert "HERMES_GATEWAY_EXTERNAL_SUPERVISOR" not in stderr_timestamp._child_env()


def test_child_env_no_marker_for_interactive_shell_xpc(monkeypatch):
    # Interactive shells inherit the sentinel XPC_SERVICE_NAME=0.
    monkeypatch.setattr(stderr_timestamp.os, "getppid", lambda: 1)
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")

    assert "HERMES_GATEWAY_EXTERNAL_SUPERVISOR" not in stderr_timestamp._child_env()


def test_child_env_no_marker_without_xpc(monkeypatch):
    monkeypatch.setattr(stderr_timestamp.os, "getppid", lambda: 1)
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)

    assert "HERMES_GATEWAY_EXTERNAL_SUPERVISOR" not in stderr_timestamp._child_env()
