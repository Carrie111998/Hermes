"""Suppress one known-benign macOS libmalloc diagnostic at stderr's fd boundary.

The allocator writes this message directly to file descriptor 2 in forked
children, bypassing Python logging and ``sys.stderr``.  A narrow fd-level filter
keeps it out of Hermes' TUI and service logs while forwarding every other stderr
line unchanged.  Malloc stack logging remains disabled; enabling that expensive
debug facility merely to silence a warning would be the wrong fix.
"""

from __future__ import annotations

import atexit
import os
import sys
import threading


_MALLOC_WARNING = (
    b"MallocStackLogging: can't turn off malloc stack logging because it was not enabled."
)
_MAX_FILTERABLE_LINE = 1024


def _is_benign_malloc_warning(line: bytes) -> bool:
    body = line.rstrip(b"\r\n")
    if not body.endswith(_MALLOC_WARNING):
        return False

    process_prefix = body[: -len(_MALLOC_WARNING)]
    if not process_prefix.endswith(b") "):
        return False

    open_paren = process_prefix.rfind(b"(")
    return open_paren > 0 and process_prefix[open_paren + 1 : -2].isdigit()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        except OSError:
            return
        view = view[written:]


def _drain_stderr(read_fd: int, forward_fd: int) -> None:
    pending = b""
    passthrough_line = False
    try:
        while True:
            try:
                chunk = os.read(read_fd, 8192)
            except InterruptedError:
                continue
            except OSError:
                break
            if not chunk:
                break

            if passthrough_line:
                newline = chunk.find(b"\n")
                if newline < 0:
                    _write_all(forward_fd, chunk)
                    continue
                _write_all(forward_fd, chunk[: newline + 1])
                passthrough_line = False
                pending += chunk[newline + 1 :]
            else:
                pending += chunk

            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    break
                line, pending = pending[: newline + 1], pending[newline + 1 :]
                if not _is_benign_malloc_warning(line):
                    _write_all(forward_fd, line)

            # The allocator diagnostic is a short single line. Do not make
            # progress bars, tracebacks with binary payloads, or other large
            # non-newline stderr writes wait indefinitely for a terminator.
            if not passthrough_line and len(pending) >= _MAX_FILTERABLE_LINE:
                _write_all(forward_fd, pending)
                pending = b""
                passthrough_line = True

        if pending and not _is_benign_malloc_warning(pending):
            _write_all(forward_fd, pending)
    finally:
        for fd in (read_fd, forward_fd):
            try:
                os.close(fd)
            except OSError:
                pass


class _StderrFilter:
    def __init__(self, restore_fd: int, thread: threading.Thread) -> None:
        self._restore_fd = restore_fd
        self._thread = thread
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        try:
            os.dup2(self._restore_fd, 2, inheritable=True)
        except OSError:
            pass
        try:
            os.close(self._restore_fd)
        except OSError:
            pass

        # Restoring fd 2 closes this process's pipe writer. The reader normally
        # reaches EOF immediately; a short timeout avoids shutdown hangs if a
        # still-running descendant inherited fd 2.
        self._thread.join(timeout=1.0)


def _install_stderr_filter() -> _StderrFilter:
    restore_fd = os.dup(2)
    forward_fd = os.dup(2)
    read_fd, write_fd = os.pipe()
    try:
        os.set_inheritable(read_fd, False)
        os.set_inheritable(forward_fd, False)
        os.dup2(write_fd, 2, inheritable=True)
    except BaseException:
        for fd in (restore_fd, forward_fd, read_fd, write_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    finally:
        try:
            os.close(write_fd)
        except OSError:
            pass

    thread = threading.Thread(
        target=_drain_stderr,
        args=(read_fd, forward_fd),
        name="hermes-macos-stderr-filter",
        daemon=True,
    )
    thread.start()
    return _StderrFilter(restore_fd, thread)


_active_filter: _StderrFilter | None = None
_install_lock = threading.Lock()


def install_macos_malloc_stderr_filter() -> bool:
    """Install the filter once on macOS; return whether this call installed it."""
    global _active_filter

    if sys.platform != "darwin":
        return False
    if os.environ.get("HERMES_DISABLE_MACOS_STDERR_FILTER") == "1":
        return False

    with _install_lock:
        if _active_filter is not None:
            return False
        try:
            _active_filter = _install_stderr_filter()
        except OSError:
            return False
        atexit.register(_active_filter.close)
        return True
