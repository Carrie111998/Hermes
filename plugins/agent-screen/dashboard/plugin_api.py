"""
plugin_api.py — Agent Screen dashboard plugin backend.

Mounted at /api/plugins/agent-screen/ by the dashboard plugin system.
Starts/stops the agent-screen native app (a virtual display + native window
+ MJPEG stream on :8788) and reports its status.

Routes:
  GET  /status  -> {running, stream}
  POST /start   -> start agent-screen.sh (idempotent)
  POST /stop    -> pkill -f agent-screen-app

Layout
------
The native companion lives in ``<plugin>/native/``: Swift sources,
``build-app.sh`` (compiles + codesigns the .app bundle) and
``agent-screen.sh`` (launcher). ``build-app.sh`` installs the built app to
``$AGENT_SCREEN_DIR`` (default ``~/.hermes/agent-screen``); this backend
resolves the launcher the same way, so a bundled and a user-installed plugin
behave identically.

Security note
-------------
Plugin HTTP routes go through the dashboard's session-token auth middleware
just like core API routes — every request must present the session bearer
token (see the kanban plugin docs for details).
"""
import os
import subprocess
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()

# The launcher script ships with the plugin (native/agent-screen.sh); the
# built .app bundle lives wherever $AGENT_SCREEN_DIR points (default:
# ~/.hermes/agent-screen). Keeping both sides on the same resolution rule
# means "build with AGENT_SCREEN_DIR=X, then run with AGENT_SCREEN_DIR=X".
NATIVE_DIR = Path(__file__).resolve().parent.parent / "native"
START_SCRIPT = NATIVE_DIR / "agent-screen.sh"
PING_URL = "http://127.0.0.1:8788/ping"


def _app_running() -> bool:
    """Is the agent-screen-app process alive (pgrep on the binary name)?"""
    try:
        r = subprocess.run(
            ["pgrep", "-f", "agent-screen-app"],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return False


def _stream_ok() -> bool:
    """Does the MJPEG streamer answer on :8788/ping?"""
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "1", PING_URL],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and r.stdout.strip() == "ok"
    except Exception:
        return False


def _state() -> dict:
    return {"running": _app_running(), "stream": _stream_ok()}


def _wait_until(pred, timeout=6.0, step=0.2) -> bool:
    """Poll until pred() is true (or timeout). pkill is asynchronous — the
    process dies a moment after the signal; an immediate /start right after
    would still find it via pgrep and wrongly skip starting."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


# After pkill (SIGTERM) the app does NOT release the virtual display
# (CGVirtualDisplay) immediately. Restarting within that window crashes — the
# app starts, cannot create the display, and dies after ~2-4s. Only after this
# pause is the display really free (measured: 3s suffices, 0s crashes).
_DISPLAY_GRACE_S = 2.5


@router.get("/status")
def status():
    return _state()


@router.post("/start")
def start():
    # Wait out any still-dying instance, THEN start cleanly.
    if _app_running():
        _wait_until(lambda: not _app_running())
        # Wait for the display to free up (CGVirtualDisplay lags behind pkill)
        import time
        time.sleep(_DISPLAY_GRACE_S)
    if not _app_running():
        # start_new_session=True: the child survives the serve process
        # (parent-death watchdog); stdout/stderr to DEVNULL — the launcher
        # redirects app logs to /tmp/agent-screen-app.log.
        subprocess.Popen(
            [str(START_SCRIPT)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    # Wait for the STREAM (not just the process): the app can briefly start
    # and crash again because the display is still busy — the stream proves it
    # really came up. Retry once after a short pause.
    if not _wait_until(_stream_ok, timeout=6.0):
        import time
        time.sleep(_DISPLAY_GRACE_S)
        subprocess.Popen(
            [str(START_SCRIPT)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_until(_stream_ok, timeout=6.0)
    return _state()


@router.post("/stop")
def stop():
    subprocess.run(
        ["pkill", "-f", "agent-screen-app"],
        capture_output=True, timeout=5,
    )
    # Wait until really dead — otherwise /status still reports the dying app
    # as "running" and the chip toggles wrong on the next click.
    _wait_until(lambda: not _app_running())
    # Wait for display release — an immediate restart would crash.
    import time
    time.sleep(_DISPLAY_GRACE_S)
    return _state()
