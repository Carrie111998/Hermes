"""Race-safe signalling through stable kernel process handles.

A PID can be recycled between identity validation and ``os.kill(pid, ...)``.
This module opens a kernel handle first, lets callers validate while that
identity is pinned, and sends signals through the same handle. Unsupported
platforms fail closed instead of falling back to a recyclable PID.
"""

from __future__ import annotations

import os
import signal
import sys
from types import TracebackType
from typing import Any, Self


class StableProcessHandleUnavailable(RuntimeError):
    """The current platform cannot safely pin a process identity."""


class StableProcessHandle:
    """A Linux pidfd or Windows process handle bound to one process identity."""

    def __init__(self, pid: int, kind: str, handle: Any, owner: Any = None):
        self.pid = pid
        self._kind = kind
        self._handle = handle
        self._owner = owner
        self._closed = False

    @classmethod
    def open(cls, pid: int) -> Self:
        if pid <= 0:
            raise ProcessLookupError(pid)

        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            # PROCESS_TERMINATE | SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
            access = 0x0001 | 0x100000 | 0x1000
            handle = kernel32.OpenProcess(access, False, pid)
            if not handle:
                error = getattr(ctypes, "get_last_error")()
                if error == 5:
                    raise PermissionError(error, "OpenProcess denied", pid)
                raise ProcessLookupError(pid)
            return cls(pid, "windows", handle, kernel32)

        pidfd_open = getattr(os, "pidfd_open", None)
        pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
        if pidfd_open is not None and pidfd_send_signal is not None:
            return cls(pid, "pidfd", pidfd_open(pid, 0))

        raise StableProcessHandleUnavailable(
            "stable process handles require Linux pidfd or Windows OpenProcess"
        )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("stable process handle is closed")

    def send_signal(self, sig: int) -> None:
        self._require_open()
        if self._kind == "pidfd":
            sender = getattr(signal, "pidfd_send_signal")
            sender(self._handle, sig, None, 0)
            return

        import ctypes
        from ctypes import wintypes

        kernel32 = self._owner
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        if not kernel32.TerminateProcess(self._handle, 1):
            error = getattr(ctypes, "get_last_error")()
            if error == 5:
                raise PermissionError(error, "TerminateProcess denied", self.pid)
            raise ProcessLookupError(self.pid)

    def is_alive(self) -> bool:
        self._require_open()
        if self._kind == "pidfd":
            try:
                sender = getattr(signal, "pidfd_send_signal")
                sender(self._handle, 0, None, 0)
                return True
            except ProcessLookupError:
                return False

        import ctypes
        from ctypes import wintypes

        kernel32 = self._owner
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        result = kernel32.WaitForSingleObject(self._handle, 0)
        if result == 0x00000102:  # WAIT_TIMEOUT
            return True
        if result == 0x00000000:  # WAIT_OBJECT_0
            return False
        error = getattr(ctypes, "get_last_error")()
        raise OSError(error, "WaitForSingleObject failed", self.pid)

    def close(self) -> None:
        if self._closed:
            return
        if self._kind == "pidfd":
            os.close(self._handle)
        else:
            self._owner.CloseHandle(self._handle)
        self._closed = True

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
