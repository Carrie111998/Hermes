"""Hermes plugin registration for the loopback Hakua TTS bridge."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .bridge import HOST, PORT, _provider

_PLUGIN_DIR = Path(__file__).resolve().parent
_STATE: dict[str, Any] = {"proc": None}


def _url() -> str:
    return f"http://{HOST}:{PORT}"


def _healthy() -> bool:
    try:
        from urllib.request import urlopen
        with urlopen(_url() + "/health", timeout=1.5) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def status(args: dict | None = None, **kwargs: Any) -> str:
    return json.dumps({
        "ok": _healthy(),
        "url": _url(),
        "provider": _provider(),
        "running": _healthy(),
        "pid": getattr(_STATE.get("proc"), "pid", None),
    }, ensure_ascii=False)


def start(args: dict | None = None, **kwargs: Any) -> str:
    if _healthy():
        return json.dumps({"ok": True, "already_running": True, "url": _url(), "provider": _provider()})
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [sys.executable, str(_PLUGIN_DIR / "bridge.py")],
        cwd=str(_PLUGIN_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    _STATE["proc"] = proc
    for _ in range(30):
        time.sleep(0.2)
        if _healthy():
            return json.dumps({"ok": True, "pid": proc.pid, "url": _url(), "provider": _provider()})
    return json.dumps({"ok": False, "pid": proc.pid, "error": "bridge did not become ready"})


def stop(args: dict | None = None, **kwargs: Any) -> str:
    proc = _STATE.get("proc")
    if proc and proc.poll() is None:
        proc.terminate()
        _STATE["proc"] = None
        return json.dumps({"ok": True, "stopped_pid": proc.pid})
    return json.dumps({"ok": True, "running": False})


def register(ctx) -> None:
    empty = {"type": "object", "properties": {}}
    for name, handler, description in (
        ("hakua_tts_bridge_status", status, "Check the local Hakua OpenAI-compatible TTS bridge."),
        ("hakua_tts_bridge_start", start, "Start the local Hakua TTS bridge."),
        ("hakua_tts_bridge_stop", stop, "Stop the local Hakua TTS bridge."),
    ):
        ctx.register_tool(
            name=name,
            toolset="tts",
            schema=empty,
            handler=handler,
            check_fn=lambda: True,
            description=description,
        )
