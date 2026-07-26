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


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_COMPOSE_DEFAULT = os.environ.get(
    "BUZZ_COMPOSE_DIR", os.path.join(_REPO_ROOT, "vendor", "buzz", "deploy", "compose")
)
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
    for filename in ("compose.yml", "docker-compose.yml"):
        compose = os.path.join(compose_path, filename)
        if os.path.exists(compose):
            return ["docker", "compose", "-p", "buzz", "-f", compose]
    return []


def _buzz_env() -> dict[str, str]:
    """Build an env dict for the buzz CLI, injecting relay URL + key when set.

    The buzz CLI defaults BUZZ_RELAY_URL to http://localhost:3000, which matches
    a local relay started from vendor/buzz/deploy/compose with RELAY_URL=ws://localhost:3000.
    """
    env = dict(os.environ)
    # The native CLI does not load dotenv files itself. Load only the two
    # Buzz client settings needed by the child process; never print them.
    dotenv_candidates = [
        os.path.join(_REPO_ROOT, "vendor", "buzz", ".env"),
        os.path.join(_REPO_ROOT, "vendor", "buzz", "deploy", "compose", ".env"),
    ]
    for dotenv in dotenv_candidates:
        try:
            with open(dotenv, encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key, value = key.strip(), value.strip().strip('"').strip("'")
                    if key in {"BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY"} and key not in env:
                        env[key] = value
        except OSError:
            continue
    if not env.get("BUZZ_RELAY_URL"):
        env["BUZZ_RELAY_URL"] = "http://localhost:3000"
    if os.environ.get("BUZZ_PRIVATE_KEY"):
        env["BUZZ_PRIVATE_KEY"] = os.environ["BUZZ_PRIVATE_KEY"]
    return env


def _run(cmd: list[str]) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=180, env=_buzz_env()
        )
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
    channel_type = "stream"
    visibility = "private" if private else "open"
    cmd = [
        bin_,
        "channels",
        "create",
        "--name", name,
        "--type", channel_type,
        "--visibility", visibility,
    ]
    description = str(args.get("description", "")).strip()
    if description:
        cmd += ["--description", description]
    res = _run(cmd)
    return json.dumps({"ok": res["ok"], "code": res["code"], "stdout": res["stdout"], "stderr": res["stderr"]}, ensure_ascii=False)


def buzz_channel_list(args: dict[str, Any], **_: Any) -> str:
    bin_ = _buzz_bin()
    if not bin_:
        return json.dumps({"ok": False, "error": "BUZZ_BIN not available; build buzz CLI or set BUZZ_BIN"}, ensure_ascii=False)
    limit = int(args.get("limit", 20))
    res = _run([bin_, "channels", "list", "--limit", str(limit)])
    return json.dumps({"ok": res["ok"], "code": res["code"], "stdout": res["stdout"], "stderr": res["stderr"]}, ensure_ascii=False)


def buzz_message_send(args: dict[str, Any], **_: Any) -> str:
    bin_ = _buzz_bin()
    if not bin_:
        return json.dumps({"ok": False, "error": "BUZZ_BIN not available; build buzz CLI or set BUZZ_BIN"}, ensure_ascii=False)
    channel = str(args.get("channel", "")).strip()
    text = str(args.get("text", "")).strip()
    if not channel or not text:
        return json.dumps({"ok": False, "error": "channel and text are required"}, ensure_ascii=False)
    cmd = [bin_, "messages", "send", "--channel", channel, "--content", text]
    res = _run(cmd)
    return json.dumps({"ok": res["ok"], "code": res["code"], "stdout": res["stdout"], "stderr": res["stderr"]}, ensure_ascii=False)


def buzz_message_read(args: dict[str, Any], **_: Any) -> str:
    bin_ = _buzz_bin()
    if not bin_:
        return json.dumps({"ok": False, "error": "BUZZ_BIN not available; build buzz CLI or set BUZZ_BIN"}, ensure_ascii=False)
    channel = str(args.get("channel", "")).strip()
    limit = int(args.get("limit", 20))
    if not channel:
        return json.dumps({"ok": False, "error": "channel is required"}, ensure_ascii=False)
    cmd = [bin_, "messages", "get", "--channel", channel, "--limit", str(limit)]
    res = _run(cmd)
    return json.dumps({"ok": res["ok"], "code": res["code"], "stdout": res["stdout"], "stderr": res["stderr"]}, ensure_ascii=False)


def buzz_keypair(args: dict[str, Any], **_: Any) -> str:
    """Inspect the configured Nostr identity.

    The buzz CLI has no `keypair` subcommand; the relay identity is supplied via
    the BUZZ_PRIVATE_KEY environment variable (hex or nsec). This handler reports
    the public key derived from the current BUZZ_PRIVATE_KEY, or instructs the
    caller to set it.
    """
    pk = os.environ.get("BUZZ_PRIVATE_KEY", "").strip()
    if not pk:
        return json.dumps(
            {"ok": False, "error": "BUZZ_PRIVATE_KEY is not set; set it to your Nostr private key (hex or nsec) to use Buzz."},
            ensure_ascii=False,
        )
    # Best-effort public key derivation using the nostr SDK if available.
    try:
        from nostr.key import PrivateKey  # type: ignore

        pub = PrivateKey.from_hex(pk).public_key().bech32() if len(pk) == 64 else PrivateKey.from_nsec(pk).public_key().bech32()
        return json.dumps({"ok": True, "public_key": pub, "has_private_key": True}, ensure_ascii=False)
    except Exception:
        return json.dumps(
            {"ok": True, "has_private_key": True, "note": "BUZZ_PRIVATE_KEY is set; install `nostr` to derive the public key."},
            ensure_ascii=False,
        )
