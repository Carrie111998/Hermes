"""
plugin_api.py — Agent Screen dashboard plugin backend.

Mounted at /api/plugins/agent-screen/ by the dashboard plugin system.
Starts/stops the agent-screen native app (a virtual display + native window
+ MJPEG stream on :8788) and reports its status.

macOS only. /start and /stop return HTTP 501 on any other platform.

Routes:
  GET  /status  -> {running, stream, supported, platform, error?}
  POST /start   -> start agent-screen.sh if needed (idempotent)
  POST /stop    -> pkill -x agent-screen-app

Process matching uses ``pgrep -x`` / ``pkill -x`` on the binary name
(``agent-screen-app``). Never ``-f`` — that matches any argv containing the
string (an editor with the source file open, swiftc, etc.).

Security note
-------------
Plugin HTTP routes go through the dashboard's session-token auth middleware
just like core API routes. The MJPEG stream itself binds loopback-only and
has no auth — any local process can watch. That is intentional and documented.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()

# The launcher ships with the plugin. The built .app lives under
# ~/.hermes/agent-screen (see native/build-app.sh). No user-facing env var —
# that directory is the install location, not configuration.
NATIVE_DIR = Path(__file__).resolve().parent.parent / "native"
START_SCRIPT = NATIVE_DIR / "agent-screen.sh"
PING_URL = "http://127.0.0.1:8788/ping"

# Exact process name of the bundle executable. ``-x`` matches this and only this.
PROC_NAME = "agent-screen-app"

# After SIGTERM the app does not release CGVirtualDisplay immediately.
# Restarting inside that window crashes (measured: 0s crashes, 3s is enough).
_DISPLAY_GRACE_S = 2.5


def _supported() -> bool:
    return sys.platform == "darwin"


def _launcher_ok() -> bool:
    return START_SCRIPT.is_file()


def _app_running() -> bool:
    """Is the agent-screen-app process alive? Exact name match only."""
    try:
        r = subprocess.run(
            ["pgrep", "-x", PROC_NAME],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return False


def _stream_ok() -> bool:
    """Does the MJPEG streamer answer on :8788/ping?"""
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "1", PING_URL],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0 and r.stdout.strip() == "ok"
    except Exception:
        return False


def _state(*, error: str | None = None) -> dict:
    payload = {
        "running": _app_running() if _supported() else False,
        "stream": _stream_ok() if _supported() else False,
        "supported": _supported(),
        "platform": sys.platform,
    }
    if error:
        payload["error"] = error
    elif not _supported():
        payload["error"] = "Agent Screen requires macOS."
    return payload


def _wait_until(pred, timeout: float = 6.0, step: float = 0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def _spawn() -> None:
    subprocess.Popen(
        [str(START_SCRIPT)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _require_macos() -> None:
    if not _supported():
        raise HTTPException(status_code=501, detail="Agent Screen requires macOS.")


@router.get("/status")
def status():
    return _state()


@router.post("/start")
def start():
    """Idempotent: a healthy running instance is a no-op."""
    _require_macos()
    if not _launcher_ok():
        return _state(error="launcher missing; run plugins/agent-screen/native/build-app.sh")
    if _app_running() and _stream_ok():
        return _state()
    if _app_running():
        # Process up but stream down — wait for it to die, then start clean.
        _wait_until(lambda: not _app_running())
        time.sleep(_DISPLAY_GRACE_S)
    if not _app_running():
        _spawn()
    if not _wait_until(_stream_ok, timeout=6.0):
        time.sleep(_DISPLAY_GRACE_S)
        if not _app_running():
            _spawn()
        _wait_until(_stream_ok, timeout=6.0)
    return _state()


@router.post("/stop")
def stop():
    _require_macos()
    if not _app_running():
        return _state()
    subprocess.run(
        ["pkill", "-x", PROC_NAME],
        capture_output=True,
        timeout=5,
    )
    _wait_until(lambda: not _app_running())
    time.sleep(_DISPLAY_GRACE_S)
    return _state()
