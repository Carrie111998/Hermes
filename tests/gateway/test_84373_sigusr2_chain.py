"""Regression tests for #84373: SIGUSR2 faulthandler must not chain to
the default signal action and terminate the gateway.

``faulthandler.register(signal, chain=True)`` re-raises the signal after
dumping tracebacks. With no previous handler, that falls through to the
process-default action and kills the gateway. The fix is to use
``chain=False`` so the diagnostic is non-fatal.

These tests exercise real subprocesses so the OS signal behavior is
preserved. They are skipped automatically on Windows.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest

_SIGUSR2 = getattr(signal, "SIGUSR2", None)
_HAS_REGISTER = hasattr(__import__("faulthandler"), "register")


def _run_child(chain: bool) -> tuple[bool, int, str]:
    code = (
        "import faulthandler, os, signal, sys;"
        "faulthandler.register(getattr(signal, 'SIGUSR2'), file=open(sys.argv[1], 'a'), all_threads=True, chain={chain});"
        "os.kill(os.getpid(), getattr(signal, 'SIGUSR2'));"
        "print('SURVIVED')"
    ).format(chain=str(chain))
    import tempfile
    with tempfile.NamedTemporaryFile("a", delete=False, suffix=".log") as f:
        log_path = f.name
    proc = subprocess.Popen(
        [sys.executable, "-c", code, log_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = proc.communicate(timeout=3)
        survived = proc.returncode == 0 and b"SURVIVED" in stdout
        try:
            with open(log_path) as f:
                log = f.read()
        except Exception:
            log = ""
        return survived, proc.returncode, log
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return False, -1, ""
    finally:
        try:
            os.unlink(log_path)
        except OSError:
            pass


@pytest.mark.skipif(
    _SIGUSR2 is None or not _HAS_REGISTER,
    reason="SIGUSR2/faulthandler.register unavailable on this platform",
)
class TestSigusr2FaulthandlerChain:
    """POSIX-only regression for #84373."""

    def test_chain_true_kills_process(self):
        survived, rc, _ = _run_child(True)
        assert not survived
        assert rc != 0

    def test_chain_false_keeps_process_alive(self):
        survived, rc, log = _run_child(False)
        assert survived
        assert rc == 0
        assert len(log) == 0 or "Current thread" in log
