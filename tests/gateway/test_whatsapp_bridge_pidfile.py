"""Regression tests: the WhatsApp stale-bridge cleanup must never kill a stranger.

The bridge records its PID in ``bridge.pid``. On the next start the gateway
SIGTERMs that PID to reap an orphaned bridge. The original code checked only
that the PID was *alive* — but once the bridge exits and is reaped the kernel
can recycle its number onto an unrelated process. Because the WhatsApp bridge
crash-loops, this cleanup ran constantly, and a recycled PID that had landed on
the user's browser main process got SIGTERMed, closing the browser at irregular
intervals (no crash, no coredump — a clean kill of a stranger).

These tests prove the identity guard: a PID is only signalled when it is still
our bridge (kernel start time matches, or — for legacy pidfiles — its command
line names node + this session). A recycled PID is left alone.
"""

import subprocess
import sys
import time

import pytest

import os
import signal
import socket
from unittest.mock import patch

from plugins.platforms.whatsapp.adapter import (
    _bridge_pid_is_ours,
    _kill_stale_bridge_by_pidfile,
    _write_bridge_pidfile,
)
from plugins.platforms.whatsapp import adapter as whatsapp_adapter
from gateway.status import get_process_start_time, _pid_exists


def _spawn_sleeper(*extra_argv) -> subprocess.Popen:
    """Spawn a real, short-lived process; optional extra argv shapes its cmdline."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.2)", *extra_argv]
    )


def _wait_dead(proc: subprocess.Popen, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return False


class TestWriteAndRoundTrip:
    def test_pidfile_records_pid_and_start_time(self, tmp_path):
        proc = _spawn_sleeper()
        try:
            _write_bridge_pidfile(tmp_path, proc.pid)
            lines = (tmp_path / "bridge.pid").read_text().split("\n")
            assert int(lines[0]) == proc.pid
            # Line 2 is the kernel start time (present on Linux).
            assert int(lines[1]) == get_process_start_time(proc.pid)
        finally:
            proc.kill()
            proc.wait()


class TestIdentityGuard:
    def test_kills_when_start_time_matches(self, tmp_path):
        """A genuine bridge (recorded start time matches) IS reaped."""
        proc = _spawn_sleeper()
        try:
            _write_bridge_pidfile(tmp_path, proc.pid)
            with patch("plugins.platforms.whatsapp.adapter.os.kill") as raw_kill:
                _kill_stale_bridge_by_pidfile(tmp_path)
                assert all(call.args[1] == 0 for call in raw_kill.call_args_list)
            assert _wait_dead(proc), "the real bridge process should be killed"
            assert not (tmp_path / "bridge.pid").exists()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


    def test_legacy_pidfile_never_authorizes_a_signal(self, tmp_path):
        """A PID-only legacy file cannot prove identity, even with matching argv."""
        proc = _spawn_sleeper("node", str(tmp_path))
        try:
            (tmp_path / "bridge.pid").write_text(str(proc.pid))  # legacy: pid only
            _kill_stale_bridge_by_pidfile(tmp_path)
            assert proc.poll() is None, "an unproven legacy PID must be preserved"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class TestEnsureBridgePortAvailable:
    """Port cleanup never infers process ownership or signals by listener PID."""

    def test_occupied_port_is_reported_without_signalling_any_process(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(1)
        try:
            with patch("plugins.platforms.whatsapp.adapter.os.kill") as kill, patch(
                "plugins.platforms.whatsapp.adapter.subprocess.run"
            ) as run:
                with pytest.raises(RuntimeError, match="still in use"):
                    whatsapp_adapter._ensure_bridge_port_available(port)

            kill.assert_not_called()
            run.assert_not_called()
        finally:
            listener.close()

    def test_free_port_needs_no_process_discovery(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        with patch("plugins.platforms.whatsapp.adapter.os.kill") as kill, patch(
            "plugins.platforms.whatsapp.adapter.subprocess.run"
        ) as run:
            whatsapp_adapter._ensure_bridge_port_available(port)

        kill.assert_not_called()
        run.assert_not_called()

