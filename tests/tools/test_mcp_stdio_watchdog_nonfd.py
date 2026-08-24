"""The stdio MCP watchdog must tolerate non-fd std streams (#93529).

When Hermes runs headless / inside the asyncio gateway, ``sys.stdin`` and
``sys.stdout`` can be ``None`` or wrapper objects with no OS-level file
descriptor. The watchdog used to pass those objects straight into
``subprocess.Popen``, so the supervised MCP server died before it ever
spoke JSON-RPC ("Connection closed" on every stdio server). The fix
resolves real fds with a raw 0/1/2 fallback — the supervisor process
itself was spawned with the right inherited handles.
"""

import os

import pytest

from tools import mcp_stdio_watchdog


def test_safe_fd_prefers_real_fileno():
    assert mcp_stdio_watchdog._safe_fd(os.sys.__stdout__ or open(os.devnull), 1) >= 0


def test_safe_fd_falls_back_when_fileno_raises():
    class _NoFd:
        def fileno(self):
            raise OSError("not a real file")

    assert mcp_stdio_watchdog._safe_fd(_NoFd(), 2) == 2


def test_safe_fd_falls_back_on_none_stream():
    # sys.stdin is None in some headless contexts; attribute access on None
    # must not escape _safe_fd either way.
    assert mcp_stdio_watchdog._safe_fd(None, 0) == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only watchdog path")
def test_main_runs_child_with_nonfd_streams(monkeypatch, capsys):
    """End-to-end: even with fake non-fd streams bound to sys.std*, the
    supervised child runs to completion (previously died pre-exec)."""

    class _FakeStream:
        def fileno(self):
            raise OSError("wrapped stream has no fd")

        def write(self, *_a, **_k):
            return None

        def flush(self):
            return None

        def read(self, *_a, **_k):
            return ""

    monkeypatch.setattr(mcp_stdio_watchdog.sys, "stdin", _FakeStream())
    monkeypatch.setattr(mcp_stdio_watchdog.sys, "stdout", _FakeStream())
    monkeypatch.setattr(mcp_stdio_watchdog.sys, "stderr", _FakeStream())

    rc = mcp_stdio_watchdog.main(
        ["--ppid", str(os.getppid()), "--", "/bin/sh", "-c", "exit 0"]
    )
    assert rc == 0
