"""
Tests for the #73830 gateway-prewarm-subprocess follow-up to #71226.

_spawn_gateway_prewarm_subprocess() replaces the removed thread-based
_warm_gateway_module() prewarm for Desktop backends (HERMES_DESKTOP=1): it
warms hermes_cli.gateway's cold-import cost (.pyc compile + OS page cache) in
an isolated subprocess instead of a thread, so it structurally cannot retain
this process's GIL the way the removed thread-based prewarm did.
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import hermes_cli.web_server as web_server_mod


def test_spawn_gateway_prewarm_subprocess_uses_subprocess_not_thread():
    """The prewarm must use subprocess.Popen, not a thread — a thread shares
    this process's GIL and can starve the event loop; a subprocess cannot."""
    with patch.object(web_server_mod.subprocess, "Popen") as mock_popen:
        mock_popen.return_value = MagicMock()
        result = web_server_mod._spawn_gateway_prewarm_subprocess()

    assert mock_popen.called
    args, _kwargs = mock_popen.call_args
    cmd = args[0]
    assert cmd[0] == web_server_mod.sys.executable
    assert "import hermes_cli.gateway" in cmd
    assert result is mock_popen.return_value


def test_spawn_gateway_prewarm_subprocess_is_best_effort():
    """If spawning fails (e.g. permission error, no python on PATH), the
    prewarm degrades to None instead of raising — the cold import just
    happens lazily on first real use instead."""
    with patch.object(web_server_mod.subprocess, "Popen", side_effect=OSError("boom")):
        result = web_server_mod._spawn_gateway_prewarm_subprocess()

    assert result is None


def test_desktop_lifespan_spawns_prewarm_subprocess_and_health_stays_fast():
    """Under HERMES_DESKTOP=1, the lifespan spawns the prewarm subprocess but
    /api/health must still respond immediately — spawning a subprocess is a
    non-blocking syscall, unlike the removed thread-based prewarm."""
    from fastapi.testclient import TestClient

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # still "running"

    with patch.object(web_server_mod.subprocess, "Popen", return_value=mock_proc) as mock_popen:
        with patch.dict(os.environ, {"HERMES_DESKTOP": "1"}):
            t0 = time.perf_counter()
            with TestClient(web_server_mod.app, raise_server_exceptions=False) as client:
                response = client.get("/api/health")
            health_ms = (time.perf_counter() - t0) * 1000

    assert mock_popen.called
    assert response.status_code == 200
    assert health_ms < 500, f"/api/health took {health_ms:.0f}ms under HERMES_DESKTOP=1 prewarm"


def test_desktop_lifespan_terminates_prewarm_subprocess_on_shutdown():
    """A still-running prewarm subprocess must be terminated AND reaped when
    the lifespan shuts down — terminate() alone leaves a zombie/defunct
    process on POSIX until something calls wait() on it."""
    from fastapi.testclient import TestClient

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # still running at shutdown time

    with patch.object(web_server_mod.subprocess, "Popen", return_value=mock_proc):
        with patch.dict(os.environ, {"HERMES_DESKTOP": "1"}):
            with TestClient(web_server_mod.app, raise_server_exceptions=False):
                pass

    mock_proc.terminate.assert_called_once()
    mock_proc.wait.assert_called_once()


def test_desktop_lifespan_does_not_terminate_prewarm_subprocess_that_already_exited():
    """A prewarm subprocess that already finished on its own must not be
    terminated again — poll() returning an exit code means it's done."""
    from fastapi.testclient import TestClient

    mock_proc = MagicMock()
    mock_proc.poll.return_value = 0  # already exited cleanly

    with patch.object(web_server_mod.subprocess, "Popen", return_value=mock_proc):
        with patch.dict(os.environ, {"HERMES_DESKTOP": "1"}):
            with TestClient(web_server_mod.app, raise_server_exceptions=False):
                pass

    mock_proc.terminate.assert_not_called()


def test_lifespan_does_not_spawn_prewarm_subprocess_without_desktop_env():
    """Non-desktop boots (`hermes serve`/dashboard, and the test suite by
    default) must not spawn the prewarm subprocess at all."""
    from fastapi.testclient import TestClient

    with patch.object(web_server_mod.subprocess, "Popen") as mock_popen:
        with patch.dict(os.environ):
            os.environ.pop("HERMES_DESKTOP", None)
            with TestClient(web_server_mod.app, raise_server_exceptions=False):
                pass

    mock_popen.assert_not_called()
