"""SillyTavern server management plugin for Hermes.

Tools:
  sillytavern_status  - check whether the local SillyTavern server responds
  sillytavern_start   - launch server.js as a detached background process
  sillytavern_stop    - stop the server process listening on the configured port
  sillytavern_version - report installed version from package.json
"""

import json
import os
import subprocess
import urllib.request

def _default_dir() -> str:
    env = os.environ.get("SILLYTAVERN_DIR")
    if env:
        return env
    # Prefer the repo submodule checkout when present.
    sub = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "vendor", "SillyTavern")
    if os.path.isfile(os.path.join(sub, "server.js")):
        return sub
    return r"C:\Users\downl\Documents\SillyTavern"


DEFAULT_DIR = _default_dir()
DEFAULT_HOST = os.environ.get("SILLYTAVERN_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("SILLYTAVERN_PORT", "8000"))

_STATE: dict = {"proc": None}


def _base_url() -> str:
    return f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"


def _is_up(timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(_base_url() + "/", timeout=timeout) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def _installed() -> bool:
    return os.path.isfile(os.path.join(DEFAULT_DIR, "server.js"))


def sillytavern_status(args, **kwargs) -> str:
    return json.dumps(
        {
            "installed": _installed(),
            "install_dir": DEFAULT_DIR,
            "url": _base_url(),
            "running": _is_up(),
        }
    )


def sillytavern_start(args, **kwargs) -> str:
    if not _installed():
        return json.dumps({"ok": False, "error": f"server.js not found in {DEFAULT_DIR}"})
    if _is_up():
        return json.dumps({"ok": True, "already_running": True, "url": _base_url()})
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        ["node", "server.js", "--browserLaunchEnabled", "false",
         "--port", str(DEFAULT_PORT)],
        cwd=DEFAULT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    _STATE["proc"] = proc
    import time

    for _ in range(45):
        time.sleep(1)
        if _is_up():
            return json.dumps({"ok": True, "pid": proc.pid, "url": _base_url()})
    return json.dumps({"ok": False, "pid": proc.pid, "error": "did not become ready in 45s"})


def sillytavern_stop(args, **kwargs) -> str:
    proc = _STATE.get("proc")
    if proc and proc.poll() is None:
        proc.terminate()
        _STATE["proc"] = None
        return json.dumps({"ok": True, "stopped_pid": proc.pid})
    # Fallback: find the PID bound to the port (Windows)
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            text=True,
            encoding="cp932",
            errors="replace",
            timeout=15,
        )
        pids = {
            line.split()[-1]
            for line in out.splitlines()
            if f":{DEFAULT_PORT}" in line and "LISTENING" in line
        }
        for pid in pids:
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
        return json.dumps({"ok": True, "killed_pids": sorted(pids)})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def sillytavern_version(args, **kwargs) -> str:
    try:
        with open(os.path.join(DEFAULT_DIR, "package.json"), encoding="utf-8") as f:
            pkg = json.load(f)
        return json.dumps({"ok": True, "version": pkg.get("version")})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def register(ctx):
    empty = {"type": "object", "properties": {}}
    ctx.register_tool(
        name="sillytavern_status",
        description="Check whether the local SillyTavern server is installed and running.",
        schema=empty,
        handler=sillytavern_status,
        check_fn=_installed,
    )
    ctx.register_tool(
        name="sillytavern_start",
        description="Start the local SillyTavern server in the background (no browser launch).",
        schema=empty,
        handler=sillytavern_start,
        check_fn=_installed,
    )
    ctx.register_tool(
        name="sillytavern_stop",
        description="Stop the local SillyTavern server.",
        schema=empty,
        handler=sillytavern_stop,
        check_fn=_installed,
    )
    ctx.register_tool(
        name="sillytavern_version",
        description="Report the installed SillyTavern version.",
        schema=empty,
        handler=sillytavern_version,
        check_fn=_installed,
    )
