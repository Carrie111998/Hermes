"""Stable process handles must signal identity, never a recyclable PID."""

from __future__ import annotations

import ctypes
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def test_pidfd_handle_signals_the_opened_process_without_os_kill():
    from hermes_cli.process_safety import StableProcessHandle

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with patch("os.kill") as raw_kill:
            with StableProcessHandle.open(proc.pid) as handle:
                assert handle.is_alive() is True
                handle.send_signal(signal.SIGTERM)
            raw_kill.assert_not_called()

        proc.wait(timeout=5)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_closed_handle_cannot_signal():
    from hermes_cli.process_safety import StableProcessHandle

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        handle = StableProcessHandle.open(proc.pid)
        handle.close()
        with pytest.raises(RuntimeError, match="closed"):
            handle.send_signal(signal.SIGTERM)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_windows_handle_lifecycle_uses_openprocess_and_closehandle():
    from unittest.mock import MagicMock

    from hermes_cli.process_safety import StableProcessHandle

    kernel32 = SimpleNamespace(
        OpenProcess=MagicMock(return_value=1234),
        TerminateProcess=MagicMock(return_value=1),
        WaitForSingleObject=MagicMock(return_value=0x102),
        CloseHandle=MagicMock(return_value=1),
    )

    with patch("hermes_cli.process_safety.sys.platform", "win32"), patch.object(
        ctypes, "WinDLL", return_value=kernel32, create=True
    ):
        handle = StableProcessHandle.open(4321)
        assert handle.is_alive() is True
        handle.send_signal(signal.SIGTERM)
        handle.close()

    kernel32.OpenProcess.assert_called_once_with(0x00101001, False, 4321)
    kernel32.TerminateProcess.assert_called_once_with(1234, 1)
    kernel32.CloseHandle.assert_called_once_with(1234)
