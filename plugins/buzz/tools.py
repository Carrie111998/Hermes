"""Buzz tool implementations — runtime-gated handlers for the ``buzz`` toolset.

Design:

- Pure functions returning JSON strings.
- No hard dependency on a running relay; missing prereqs surface
  JSON error payloads so the agent can report them faithfully.
- Docker compose shortcuts are provided for local dev when
  ``vendor/buzz`` contains a compose file.
- ``BUZZ_BIN`` can point at a native ``buzz`` CLI for channel/message/keypair tools.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any


_COMPOSE_DEFAULT = os.environ.get("BUZZ_COMPOSE_DIR", "vendor/buzz")
_BUZZ_BIN = os.environ.get("BUZZ_BIN")


def _buzz_bin() -> str | None:
    candidates = [
        _BUZZ_BIN,
        shutil.which("buzz"),
        os.path.join(os.path.expanduser("~"), ".cargo", "bin", "buzz"),
        os.path.join(os.path.expanduser("~"), ".local", "bin", "buzz"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _compose_path(compose_path: str | None = None) -> str:
    return compose_path or _COMPOSE_DEFAULT


def _docker_compose_cmd(compose_path: str) -> list[str]:
    compose = os.path.join(compose_path, "docker-compose.yml")
    if os.path.exists(compose):
        return ["docker", "compose", "-f", compose]
    return []


def _run(cmd: list[str]) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=180, stdin=subprocess.DEVNULL)
        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except FileNotFoundError as exc:
        return {"ok": False, "code": -1, "stdout": "", "stderr": str(exc)}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "code": -2, "stdout": "", "stderr": f"timeout after {exc.timeout}s"}


def buzz_relay_status(args: dict[str, Any], **_: Any) -> str:
    compose = _compose_path(args.get("compose_path"))
    cmd = _docker_compose_cmd(compose)
    if cmd:
        res = _run(cmd + ["ps", "--format", "{{.Name}}: {{.State}}"])
        if res["ok"] and res["stdout"].strip():
            return json.dumps(
                {"compose_path": compose, "containers": res["stdout"].strip().splitlines()},
                ensure_ascii=False,
            )
    return json.dumps({"compose_path": compose, "status": "unknown"}, ensure_ascii=False)


def buzz_relay_start(args: dict[str, Any], **_: Any) -> str:
    compose = _compose_path(args.get("compose_path"))
    cmd = _docker_compose_cmd(compose)
    if not cmd:
        return json.dumps({"ok": False, "error": "compose file not found", "path": compose}, ensure_ascii=False)
    res = _run(cmd + ["up", "-d"])
    return json.dumps({"ok": res["ok"], "code": res["code"], "path": compose, "stdout": res["stdout"], "stderr": res["stderr"]}, ensure_ascii=False)


def buzz_relay_stop(args: dict[str, Any], **_: Any) -> str:
    compose = _compose_path(args.get("compose_path"))
    cmd = _docker_compose_cmd(compose)
    if not cmd:
        return json.dumps({"ok": False, "error": "compose file not found", "path": compose}, ensure_ascii=False)
    res = _run(cmd + ["down"])
    return json.dumps({"ok": res["ok"], "code": res["code"], "path": compose, "stdout": res["stdout"], "stderr": res["stderr"]}, ensure_ascii=False)


def buzz_channel_create(args: dict[str, Any], **_: Any) -> str:
    bin_ = _buzz_bin()
    if not bin_:
        return json.dumps({"ok": False, "error": "BUZZ_BIN not available; build buzz CLI or set BUZZ_BIN"}, ensure_ascii=False)
    name = str(args.get("name", "")).strip()
    private = bool(args.get("private", False))
    if not name:
        return json.dumps({"ok": False, "error": "name is required"}, ensure_ascii=False)
    cmd = [bin_, "channel", "create", name, "--private" if private else "--public"]
    res = _run(cmd)
    return json.dumps({"ok": res["ok"], "code": res["code"], "stdout": res["stdout"], "stderr": res["stderr"]}, ensure_ascii=False)


def buzz_channel_list(args: dict[str, Any], **_: Any) -> str:
    bin_ = _buzz_bin()
    if not bin_:
        return json.dumps({"ok": False, "error": "BUZZ_BIN not available; build buzz CLI or set BUZZ_BIN"}, ensure_ascii=False)
    limit = int(args.get("limit", 20))
    res = _run([bin_, "channel", "list", "--limit", str(limit)])
    return json.dumps({"ok": res["ok"], "code": res["code"], "stdout": res["stdout"], "stderr": res["stderr"]}, ensure_ascii=False)


def buzz_message_send(args: dict[str, Any], **_: Any) -> str:
    bin_ = _buzz_bin()
    if not bin_:
        return json.dumps({"ok": False, "error": "BUZZ_BIN not available; build buzz CLI or set BUZZ_BIN"}, ensure_ascii=False)
    channel = str(args.get("channel", "")).strip()
    text = str(args.get("text", "")).strip()
    if not channel or not text:
        return json.dumps({"ok": False, "error": "channel and text are required"}, ensure_ascii=False)
    res = _run([bin_, "message", "send", channel, "-m", text])
    return json.dumps({"ok": res["ok"], "code": res["code"], "stdout": res["stdout"], "stderr": res["stderr"]}, ensure_ascii=False)


def buzz_message_read(args: dict[str, Any], **_: Any) -> str:
    bin_ = _buzz_bin()
    if not bin_:
        return json.dumps({"ok": False, "error": "BUZZ_BIN not available; build buzz CLI or set BUZZ_BIN"}, ensure_ascii=False)
    channel = str(args.get("channel", "")).strip()
    limit = int(args.get("limit", 20))
    if not channel:
        return json.dumps({"ok": False, "error": "channel is required"}, ensure_ascii=False)
    res = _run([bin_, "message", "read", channel, "--limit", str(limit)])
    return json.dumps({"ok": res["ok"], "code": res["code"], "stdout": res["stdout"], "stderr": res["stderr"]}, ensure_ascii=False)


def buzz_keypair(args: dict[str, Any], **_: Any) -> str:
    bin_ = _buzz_bin()
    if not bin_:
        return json.dumps({"ok": False, "error": "BUZZ_BIN not available; build buzz CLI or set BUZZ_BIN"}, ensure_ascii=False)
    action = str(args.get("action", "")).strip().lower()
    if action not in {"generate", "inspect", "export"}:
        return json.dumps({"ok": False, "error": "action must be generate|inspect|export"}, ensure_ascii=False)
    res = _run([bin_, "keypair", action])
    return json.dumps({"ok": res["ok"], "code": res["code"], "stdout": res["stdout"], "stderr": res["stderr"]}, ensure_ascii=False)
