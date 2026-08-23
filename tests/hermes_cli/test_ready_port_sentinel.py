"""The port-discovery sentinel must reach the process's real stdout.

The desktop learns the backend's ephemeral port by matching
``HERMES_(BACKEND|DASHBOARD)_READY port=<n>`` on the spawned child's stdout
(``apps/desktop/electron/backend-ready.ts``). ``tui_gateway.server`` rebinds
``sys.stdout = sys.stderr`` at import time to protect its JSON-RPC stream, and
that rebind is process-wide, so a plain ``print`` sends the sentinel to stderr
and desktop boot fails on the 90s port-announcement timeout with a healthy
backend running.
"""

from __future__ import annotations

import io
import subprocess
import sys
import textwrap


class TestAnnounceReadyPort:
    def test_writes_to_real_stdout_when_sys_stdout_is_rebound(self, monkeypatch):
        """A rebound ``sys.stdout`` must not capture the sentinel."""
        from hermes_cli.web_server import _announce_ready_port

        rebound = io.StringIO()
        real = io.StringIO()
        monkeypatch.setattr(sys, "stdout", rebound)
        monkeypatch.setattr(sys, "__stdout__", real)

        _announce_ready_port("HERMES_BACKEND_READY", 43210)

        assert real.getvalue() == "HERMES_BACKEND_READY port=43210\n"
        assert rebound.getvalue() == ""

    def test_falls_back_to_sys_stdout_without_a_real_stream(self, monkeypatch):
        """pythonw.exe has ``sys.__stdout__ is None`` — don't crash on it."""
        from hermes_cli.web_server import _announce_ready_port

        fallback = io.StringIO()
        monkeypatch.setattr(sys, "stdout", fallback)
        monkeypatch.setattr(sys, "__stdout__", None)

        _announce_ready_port("HERMES_DASHBOARD_READY", 1)

        assert fallback.getvalue() == "HERMES_DASHBOARD_READY port=1\n"


def test_sentinel_reaches_stdout_after_the_gateway_import():
    """End-to-end over a pipe, the way the desktop actually reads it.

    Importing ``tui_gateway.server`` is what rebinds ``sys.stdout``; doing it
    before the announcement reproduces the real backend's import order. Both
    streams are captured separately so a sentinel that lands on stderr fails
    here instead of looking fine in a merged log.
    """
    script = textwrap.dedent(
        """
        import tui_gateway.server  # rebinds sys.stdout to sys.stderr
        from hermes_cli.web_server import _announce_ready_port
        _announce_ready_port("HERMES_BACKEND_READY", 43210)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=110,
    )

    assert proc.returncode == 0, proc.stderr
    assert "HERMES_BACKEND_READY port=43210" in proc.stdout
    assert "HERMES_BACKEND_READY" not in proc.stderr
