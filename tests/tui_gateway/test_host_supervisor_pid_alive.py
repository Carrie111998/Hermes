"""_pid_alive must never kill the process it probes.

``os.kill(pid, 0)`` is a POSIX liveness probe, but on Windows sig=0 is
not a no-op (bpo-14484): it terminates the target / Ctrl+C's its console
group. tui_gateway/host_supervisor.py used the bare POSIX form, so on a
Windows host every registry staleness check terminated a healthy TUI
host. These tests pin the contract: probing a live PID must succeed
without ever reaching ``os.kill``.
"""

import os
import subprocess
import sys

from tui_gateway import host_supervisor


def test_pid_alive_never_calls_os_kill(monkeypatch):
    def _boom(*_a, **_kw):  # noqa: ANN002, ANN003
        raise AssertionError("os.kill reached — Windows would kill the probe target")

    monkeypatch.setattr(os, "kill", _boom)
    assert host_supervisor._pid_alive(os.getpid()) is True


def test_pid_alive_rejects_nonpositive_pids(monkeypatch):
    def _boom(*_a, **_kw):  # noqa: ANN002, ANN003
        raise AssertionError("os.kill reached for non-positive pid")

    monkeypatch.setattr(os, "kill", _boom)
    assert host_supervisor._pid_alive(0) is False
    assert host_supervisor._pid_alive(-1) is False


def test_pid_alive_reports_dead_pid_false():
    # A fully reaped child PID must not be reported alive.
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait()  # reaped -> definitely dead
    assert host_supervisor._pid_alive(proc.pid) is False
