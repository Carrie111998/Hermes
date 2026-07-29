"""Buzz tool implementations — runtime-gated handlers for the ``buzz`` toolset.

Design:

- Pure functions returning JSON strings.
- No hard dependency on a running relay; missing prereqs surface
  JSON error payloads so the agent can report them faithfully.
- Docker compose shortcuts are provided for local dev when
  ``vendor/buzz`` contains a compose file.
- ``BUZZ_BIN`` can point at a native ``buzz`` CLI for channel/message/workflow tools.
- Observer frames (Kind 24200) are built locally via NIP-44 + POST /events
  (buzz-sdk parity) when the CLI has no dedicated observer subcommand.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_COMPOSE_DEFAULT = os.environ.get(
    "BUZZ_COMPOSE_DIR", os.path.join(_REPO_ROOT, "vendor", "buzz")
)
_BUZZ_BIN = os.environ.get("BUZZ_BIN")
_DEFAULT_RELAY = "http://localhost:8080"


def _buzz_bin() -> str | None:
    home = os.path.expanduser("~")
    candidates = [
        _BUZZ_BIN,
        shutil.which("buzz"),
        shutil.which("buzz.exe"),
        os.path.join(home, ".cargo", "bin", "buzz.exe"),
        os.path.join(home, ".cargo", "bin", "buzz"),
        os.path.join(home, ".local", "bin", "buzz.exe"),
        os.path.join(home, ".local", "bin", "buzz"),
        # Spaceless Windows build path used by this host
        r"C:\c\buzz_target\release\buzz.exe",
        "/c/c/buzz_target/release/buzz.exe",
        os.path.join(_REPO_ROOT, "vendor", "buzz", "target", "release", "buzz.exe"),
        os.path.join(_REPO_ROOT, "vendor", "buzz", "target", "release", "buzz"),
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
        # also check deploy/compose layout
        nested = os.path.join(compose_path, "deploy", "compose", filename)
        if os.path.exists(nested):
            return ["docker", "compose", "-p", "buzz", "-f", nested]
    return []


def _load_dotenv_keys(path: str, keys: set[str], env: dict[str, str]) -> None:
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key in keys and key not in env:
                    env[key] = value
    except OSError:
        return


def _buzz_env() -> dict[str, str]:
    """Build an env dict for the buzz CLI, injecting relay URL + key when set."""
    env = dict(os.environ)
    dotenv_candidates = [
        os.path.join(_REPO_ROOT, "vendor", "buzz", ".env"),
        os.path.join(_REPO_ROOT, "vendor", "buzz", "deploy", "compose", ".env"),
        os.path.join(os.path.expanduser("~"), ".hermes", ".env"),
    ]
    wanted = {"BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY", "BUZZ_AUTH_TAG", "BUZZ_OWNER_PUBKEY"}
    for dotenv in dotenv_candidates:
        _load_dotenv_keys(dotenv, wanted, env)
    if not env.get("BUZZ_RELAY_URL"):
        env["BUZZ_RELAY_URL"] = _DEFAULT_RELAY
    return env


def _relay_url(env: dict[str, str] | None = None) -> str:
    e = env or _buzz_env()
    return (e.get("BUZZ_RELAY_URL") or _DEFAULT_RELAY).rstrip("/")


def _private_key(env: dict[str, str] | None = None) -> str:
    e = env or _buzz_env()
    return (e.get("BUZZ_PRIVATE_KEY") or "").strip()


def _run(cmd: list[str]) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
            env=_buzz_env(),
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
        return {
            "ok": False,
            "code": -2,
            "stdout": "",
            "stderr": f"timeout after {exc.timeout}s",
        }


def _json_result(**kwargs: Any) -> str:
    # Always include the standard envelope fields when present.
    return json.dumps(kwargs, ensure_ascii=False)


def _need_bin() -> tuple[str | None, str | None]:
    bin_ = _buzz_bin()
    if not bin_:
        return None, "BUZZ_BIN not available; build buzz CLI or set BUZZ_BIN"
    return bin_, None


# ---------------------------------------------------------------------------
# Relay lifecycle
# ---------------------------------------------------------------------------


def buzz_relay_status(args: dict[str, Any], **_: Any) -> str:
    compose = _compose_path(args.get("compose_path"))
    cmd = _docker_compose_cmd(compose)
    if cmd:
        res = _run(cmd + ["ps", "--format", "{{.Name}}: {{.State}}"])
        if res["ok"] and res["stdout"].strip():
            return _json_result(
                ok=True,
                code=0,
                compose_path=compose,
                containers=res["stdout"].strip().splitlines(),
                stdout=res["stdout"],
                stderr=res["stderr"],
            )
    # Fallback: probe health endpoint
    import urllib.request

    relay = _relay_url()
    health = relay.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(health, timeout=3) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return _json_result(
                ok=True,
                code=0,
                compose_path=compose,
                health_url=health,
                stdout=body,
                stderr="",
            )
    except Exception as exc:
        return _json_result(
            ok=False,
            code=-1,
            compose_path=compose,
            status="unknown",
            stdout="",
            stderr=str(exc),
        )


def buzz_relay_start(args: dict[str, Any], **_: Any) -> str:
    compose = _compose_path(args.get("compose_path"))
    cmd = _docker_compose_cmd(compose)
    if not cmd:
        return _json_result(
            ok=False,
            code=-1,
            error="compose file not found",
            path=compose,
            stdout="",
            stderr="",
        )
    res = _run(cmd + ["up", "-d"])
    return _json_result(
        ok=res["ok"],
        code=res["code"],
        path=compose,
        stdout=res["stdout"],
        stderr=res["stderr"],
    )


def buzz_relay_stop(args: dict[str, Any], **_: Any) -> str:
    compose = _compose_path(args.get("compose_path"))
    cmd = _docker_compose_cmd(compose)
    if not cmd:
        return _json_result(
            ok=False,
            code=-1,
            error="compose file not found",
            path=compose,
            stdout="",
            stderr="",
        )
    res = _run(cmd + ["down"])
    return _json_result(
        ok=res["ok"],
        code=res["code"],
        path=compose,
        stdout=res["stdout"],
        stderr=res["stderr"],
    )


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def buzz_channel_create(args: dict[str, Any], **_: Any) -> str:
    bin_, err = _need_bin()
    if err:
        return _json_result(ok=False, code=-1, error=err, stdout="", stderr="")
    name = str(args.get("name", "")).strip()
    private = bool(args.get("private", False))
    if not name:
        return _json_result(
            ok=False, code=1, error="name is required", stdout="", stderr=""
        )
    channel_type = str(args.get("type", "stream") or "stream")
    visibility = "private" if private else str(args.get("visibility", "open") or "open")
    assert bin_ is not None
    cmd = [
        bin_,
        "channels",
        "create",
        "--name",
        name,
        "--type",
        channel_type,
        "--visibility",
        visibility,
    ]
    description = str(args.get("description", "")).strip()
    if description:
        cmd += ["--description", description]
    res = _run(cmd)
    return _json_result(
        ok=res["ok"], code=res["code"], stdout=res["stdout"], stderr=res["stderr"]
    )


def buzz_channel_list(args: dict[str, Any], **_: Any) -> str:
    bin_, err = _need_bin()
    if err:
        return _json_result(ok=False, code=-1, error=err, stdout="", stderr="")
    limit = int(args.get("limit", 20))
    assert bin_ is not None
    res = _run([bin_, "channels", "list", "--limit", str(limit)])
    return _json_result(
        ok=res["ok"], code=res["code"], stdout=res["stdout"], stderr=res["stderr"]
    )


# ---------------------------------------------------------------------------
# Messages — Kind::Custom(9) via buzz-sdk build_message
# ---------------------------------------------------------------------------


def buzz_message_send(args: dict[str, Any], **_: Any) -> str:
    """Send a Kind 9 channel message (buzz-sdk ``build_message``).

    Maps to: ``buzz messages send --channel <UUID> --content <...> [--kind 9]
    [--reply-to <ID>] [--broadcast]``.
    """
    bin_, err = _need_bin()
    if err:
        return _json_result(ok=False, code=-1, error=err, stdout="", stderr="")
    channel = str(args.get("channel", "")).strip()
    text = str(args.get("text") or args.get("content") or "").strip()
    if not channel or not text:
        return _json_result(
            ok=False,
            code=1,
            error="channel and text are required",
            stdout="",
            stderr="",
        )
    assert bin_ is not None
    kind = args.get("kind", 9)
    try:
        kind_i = int(kind) if kind is not None else 9
    except (TypeError, ValueError):
        kind_i = 9
    cmd = [
        bin_,
        "messages",
        "send",
        "--channel",
        channel,
        "--content",
        text,
        "--kind",
        str(kind_i),
    ]
    reply_to = str(args.get("reply_to") or args.get("reply-to") or "").strip()
    if reply_to:
        cmd += ["--reply-to", reply_to]
    if bool(args.get("broadcast", False)):
        cmd.append("--broadcast")
    files = args.get("files") or args.get("file") or []
    if isinstance(files, str):
        files = [files]
    for f in files:
        f = str(f).strip()
        if f:
            cmd += ["--file", f]
    res = _run(cmd)
    return _json_result(
        ok=res["ok"],
        code=res["code"],
        kind=kind_i,
        channel=channel,
        stdout=res["stdout"],
        stderr=res["stderr"],
    )


def buzz_message_read(args: dict[str, Any], **_: Any) -> str:
    bin_, err = _need_bin()
    if err:
        return _json_result(ok=False, code=-1, error=err, stdout="", stderr="")
    channel = str(args.get("channel", "")).strip()
    limit = int(args.get("limit", 20))
    if not channel:
        return _json_result(
            ok=False, code=1, error="channel is required", stdout="", stderr=""
        )
    assert bin_ is not None
    cmd = [bin_, "messages", "get", "--channel", channel, "--limit", str(limit)]
    kinds = str(args.get("kinds", "")).strip()
    if kinds:
        cmd += ["--kinds", kinds]
    res = _run(cmd)
    return _json_result(
        ok=res["ok"], code=res["code"], stdout=res["stdout"], stderr=res["stderr"]
    )


# ---------------------------------------------------------------------------
# Workflows — trigger from task completions (buzz-sdk build_workflow_trigger)
# ---------------------------------------------------------------------------


def buzz_workflow_trigger(args: dict[str, Any], **_: Any) -> str:
    """Trigger a workflow run (Kind 46020 / buzz-sdk ``build_workflow_trigger``).

    Maps to: ``buzz workflows trigger --workflow <UUID> [--inputs '{...}']``.
    Use after task completions to kick multi-agent orchestration.
    """
    bin_, err = _need_bin()
    if err:
        return _json_result(ok=False, code=-1, error=err, stdout="", stderr="")
    workflow = str(args.get("workflow") or args.get("workflow_id") or "").strip()
    if not workflow:
        return _json_result(
            ok=False, code=1, error="workflow is required", stdout="", stderr=""
        )
    assert bin_ is not None
    cmd = [bin_, "workflows", "trigger", "--workflow", workflow]
    inputs = args.get("inputs")
    if inputs is not None:
        if isinstance(inputs, (dict, list)):
            inputs_s = json.dumps(inputs, ensure_ascii=False, separators=(",", ":"))
        else:
            inputs_s = str(inputs).strip()
        if inputs_s:
            # Validate JSON object early for clearer errors
            try:
                parsed = json.loads(inputs_s)
            except json.JSONDecodeError as exc:
                return _json_result(
                    ok=False,
                    code=1,
                    error=f"--inputs is not valid JSON: {exc}",
                    stdout="",
                    stderr="",
                )
            if not isinstance(parsed, dict):
                return _json_result(
                    ok=False,
                    code=1,
                    error="--inputs must be a JSON object",
                    stdout="",
                    stderr="",
                )
            cmd += ["--inputs", inputs_s]
    res = _run(cmd)
    return _json_result(
        ok=res["ok"],
        code=res["code"],
        workflow=workflow,
        stdout=res["stdout"],
        stderr=res["stderr"],
    )


def buzz_workflow_list(args: dict[str, Any], **_: Any) -> str:
    bin_, err = _need_bin()
    if err:
        return _json_result(ok=False, code=-1, error=err, stdout="", stderr="")
    channel = str(args.get("channel", "")).strip()
    if not channel:
        return _json_result(
            ok=False, code=1, error="channel is required", stdout="", stderr=""
        )
    assert bin_ is not None
    res = _run([bin_, "workflows", "list", "--channel", channel])
    return _json_result(
        ok=res["ok"], code=res["code"], stdout=res["stdout"], stderr=res["stderr"]
    )


def buzz_workflow_create(args: dict[str, Any], **_: Any) -> str:
    bin_, err = _need_bin()
    if err:
        return _json_result(ok=False, code=-1, error=err, stdout="", stderr="")
    channel = str(args.get("channel", "")).strip()
    yaml_def = str(args.get("yaml") or args.get("definition") or "").strip()
    if not channel or not yaml_def:
        return _json_result(
            ok=False,
            code=1,
            error="channel and yaml are required",
            stdout="",
            stderr="",
        )
    assert bin_ is not None
    res = _run(
        [bin_, "workflows", "create", "--channel", channel, "--yaml", yaml_def]
    )
    return _json_result(
        ok=res["ok"], code=res["code"], stdout=res["stdout"], stderr=res["stderr"]
    )


# ---------------------------------------------------------------------------
# Observer frames — Kind 24200 (NIP-44 encrypted telemetry/control)
# ---------------------------------------------------------------------------


def buzz_observer_emit(args: dict[str, Any], **_: Any) -> str:
    """Emit an encrypted agent observer frame (Kind 24200 / buzz-sdk builder).

    Builds NIP-44 v2 ciphertext + NIP-01 signed event and POSTs to ``/events``.
    Frame types: ``telemetry`` (agent→owner) or ``control`` (owner→agent).
    """
    env = _buzz_env()
    pk = str(args.get("private_key") or _private_key(env) or "").strip()
    if not pk:
        return _json_result(
            ok=False,
            code=3,
            error="BUZZ_PRIVATE_KEY is required (env or args.private_key)",
            stdout="",
            stderr="",
        )
    recipient = str(
        args.get("recipient_pubkey")
        or args.get("owner_pubkey")
        or env.get("BUZZ_OWNER_PUBKEY")
        or ""
    ).strip()
    if not recipient:
        return _json_result(
            ok=False,
            code=1,
            error="recipient_pubkey (or BUZZ_OWNER_PUBKEY) is required",
            stdout="",
            stderr="",
        )
    frame = str(args.get("frame") or "telemetry").strip().lower()
    if frame not in {"telemetry", "control"}:
        return _json_result(
            ok=False,
            code=1,
            error="frame must be 'telemetry' or 'control'",
            stdout="",
            stderr="",
        )
    payload = args.get("payload")
    if payload is None:
        # Convenience: accept free-form fields as the payload body
        payload = {
            k: v
            for k, v in args.items()
            if k
            not in {
                "private_key",
                "recipient_pubkey",
                "owner_pubkey",
                "agent_pubkey",
                "frame",
                "payload",
                "relay_url",
            }
        }
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {"text": payload}
    if not isinstance(payload, dict):
        return _json_result(
            ok=False,
            code=1,
            error="payload must be a JSON object",
            stdout="",
            stderr="",
        )
    # Enrich with standard observer envelope fields when missing
    if "timestamp" not in payload:
        from datetime import datetime, timezone

        payload = {
            **payload,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    relay = str(args.get("relay_url") or _relay_url(env)).strip()
    agent_pubkey = str(args.get("agent_pubkey") or "").strip() or None

    try:
        from plugins.buzz.nostr_crypto import build_and_submit_observer_frame
    except Exception:
        try:
            from .nostr_crypto import build_and_submit_observer_frame  # type: ignore
        except Exception as exc:
            return _json_result(
                ok=False,
                code=-1,
                error=f"nostr_crypto import failed: {exc}",
                stdout="",
                stderr="",
            )

    result = build_and_submit_observer_frame(
        private_key=pk,
        recipient_pubkey=recipient,
        frame=frame,
        payload=payload,
        relay_url=relay,
        agent_pubkey=agent_pubkey,
    )
    # Normalize envelope
    out = {
        "ok": bool(result.get("ok")),
        "code": result.get("code", 0 if result.get("ok") else -1),
        "stdout": result.get("body") or result.get("stdout") or "",
        "stderr": result.get("error") or result.get("stderr") or "",
        "kind": 24200,
        "frame": frame,
        "event_id": result.get("event_id"),
        "agent_pubkey": result.get("agent_pubkey"),
        "recipient_pubkey": result.get("recipient_pubkey"),
        "relay_url": relay,
    }
    if result.get("error") and not out["stderr"]:
        out["stderr"] = str(result["error"])
        out["ok"] = False
    return json.dumps(out, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def buzz_keypair(args: dict[str, Any], **_: Any) -> str:
    """Inspect the configured Nostr identity (BUZZ_PRIVATE_KEY)."""
    env = _buzz_env()
    pk = _private_key(env)
    if not pk:
        return _json_result(
            ok=False,
            code=3,
            error="BUZZ_PRIVATE_KEY is not set; set it to your Nostr private key (hex or nsec).",
            stdout="",
            stderr="",
        )
    try:
        from plugins.buzz.nostr_crypto import normalize_sk, pubkey_from_sk
    except Exception:
        try:
            from .nostr_crypto import normalize_sk, pubkey_from_sk  # type: ignore
        except Exception:
            return _json_result(
                ok=True,
                code=0,
                has_private_key=True,
                note="BUZZ_PRIVATE_KEY is set; install coincurve to derive the public key.",
                stdout="",
                stderr="",
            )
    try:
        sk = normalize_sk(pk)
        pub = pubkey_from_sk(sk)
        return _json_result(
            ok=True,
            code=0,
            public_key=pub,
            has_private_key=True,
            stdout=pub,
            stderr="",
        )
    except Exception as exc:
        return _json_result(
            ok=False,
            code=-1,
            error=str(exc),
            has_private_key=True,
            stdout="",
            stderr=str(exc),
        )


def buzz_version(args: dict[str, Any], **_: Any) -> str:
    bin_, err = _need_bin()
    if err:
        return _json_result(ok=False, code=-1, error=err, stdout="", stderr="")
    assert bin_ is not None
    res = _run([bin_, "--help"])
    # --help always exits 0; also report path
    return _json_result(
        ok=True,
        code=0,
        buzz_bin=bin_,
        relay_url=_relay_url(),
        stdout=res["stdout"][:2000],
        stderr=res["stderr"],
    )
