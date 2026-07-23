"""SillyTavern server management plugin for Hermes.

Tools:
  sillytavern_status  - check whether the local SillyTavern server responds
  sillytavern_start   - configure from Hermes providers, then launch server
  sillytavern_stop    - stop the server process
  sillytavern_version - report installed version from package.json

Auto-configures secrets.json and settings.json from Hermes .env / config.yaml
so the local llama server and cloud API keys (OpenAI, Gemini) are available.
"""

import json
import os
import subprocess
import urllib.request
from pathlib import Path

_HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))

DEFAULT_DIR = None  # resolved on first use


def _resolve_dir() -> str:
    env = os.environ.get("SILLYTAVERN_DIR")
    if env:
        return env
    # Repo submodule checkout
    sub = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "vendor",
        "SillyTavern",
    )
    if os.path.isfile(os.path.join(sub, "server.js")):
        return sub
    # Local install fallback
    return r"C:\Users\downl\Documents\SillyTavern"


def _get_dir() -> str:
    global DEFAULT_DIR
    if DEFAULT_DIR is None:
        DEFAULT_DIR = _resolve_dir()
    return DEFAULT_DIR


DEFAULT_HOST = os.environ.get("SILLYTAVERN_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("SILLYTAVERN_PORT", "8000"))
_LOCAL_LLAMA = "http://127.0.0.1:8080/v1"

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
    return os.path.isfile(os.path.join(_get_dir(), "server.js"))


# ── Auto-configuration from Hermes secrets ──────────────────────────

def _load_hermes_env() -> dict:
    """Read ~/.hermes/.env and return a key-value dict."""
    env_path = _HERMES_HOME / ".env"
    if not env_path.exists():
        return {}
    keys = {}
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                keys[k] = v
    return keys


def _configure() -> dict:
    """Write secrets.json and settings.json for SillyTavern.

    Returns a dict summarising which secrets were written (key names only).
    """
    st_dir = _get_dir()
    data_dir = os.path.join(st_dir, "data", "default-user")
    os.makedirs(data_dir, exist_ok=True)

    env = _load_hermes_env()

    # ── secrets.json ────────────────────────────────────────────────
    secrets_path = os.path.join(data_dir, "secrets.json")
    existing = {}
    if os.path.exists(secrets_path):
        with open(secrets_path, encoding="utf-8-sig") as f:
            existing = json.load(f)

    secret_map = {
        "api_key_openai": env.get("OPENAI_API_KEY", ""),
        "api_key_makersuite": env.get("GEMINI_API_KEY", ""),
        "api_key_llamacpp": "local",
    }
    written = []
    for key, val in secret_map.items():
        if val and key not in existing:
            existing[key] = val
            written.append(key)

    if written:
        with open(secrets_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)

    # ── settings.json ───────────────────────────────────────────────
    settings_path = os.path.join(data_dir, "settings.json")
    settings = {}
    if os.path.exists(settings_path):
        with open(settings_path, encoding="utf-8-sig") as f:
            settings = json.load(f)

    changed = []
    oai = settings.get("oai_settings", {})

    # Set primary API to local llama
    if settings.get("main_api") != "openai":
        settings["main_api"] = "openai"
        changed.append("main_api")

    if settings.get("api_server") != _LOCAL_LLAMA:
        settings["api_server"] = _LOCAL_LLAMA
        changed.append("api_server")

    if settings.get("max_context") != 65536:
        settings["max_context"] = 65536
        changed.append("max_context")

    # OpenAI-compatible settings for local llama
    if oai.get("chat_completion_source") != "openai":
        oai["chat_completion_source"] = "openai"
        changed.append("oai.chat_completion_source")

    if oai.get("reverse_proxy") != _LOCAL_LLAMA:
        oai["reverse_proxy"] = _LOCAL_LLAMA
        changed.append("oai.reverse_proxy")

    if oai.get("openai_model") != "Qwen3.6-35B-A3B-Uncensored-IQ3_M":
        oai["openai_model"] = "Qwen3.6-35B-A3B-Uncensored-IQ3_M"
        changed.append("oai.model")

    if oai.get("openai_max_context") != 65536:
        oai["openai_max_context"] = 65536
        changed.append("oai.max_context")

    if oai.get("openai_max_tokens") != 8192:
        oai["openai_max_tokens"] = 8192
        changed.append("oai.max_tokens")

    if not oai.get("stream_openai"):
        oai["stream_openai"] = True
        changed.append("oai.stream")

    settings["oai_settings"] = oai

    # Unlock max context
    pu = settings.get("power_user", {})
    if not pu.get("max_context_unlocked"):
        pu["max_context_unlocked"] = True
        changed.append("power_user.max_context_unlocked")
    settings["power_user"] = pu

    if changed:
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)

    return {"secrets_written": written, "settings_changed": changed}


# ── Tool handlers ───────────────────────────────────────────────────

def sillytavern_status(args, **kwargs) -> str:
    return json.dumps(
        {
            "installed": _installed(),
            "install_dir": _get_dir(),
            "url": _base_url(),
            "running": _is_up(),
        }
    )


def sillytavern_start(args, **kwargs) -> str:
    if not _installed():
        return json.dumps(
            {"ok": False, "error": f"server.js not found in {_get_dir()}"}
        )
    if _is_up():
        return json.dumps(
            {"ok": True, "already_running": True, "url": _base_url()}
        )

    # Auto-configure from Hermes before launch
    config_result = _configure()
    config_note = (
        f"secrets: {len(config_result['secrets_written'])} new, "
        f"settings: {len(config_result['settings_changed'])} changed"
    )

    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [
            "node",
            "server.js",
            "--browserLaunchEnabled",
            "false",
            "--port",
            str(DEFAULT_PORT),
        ],
        cwd=_get_dir(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    _STATE["proc"] = proc
    import time

    for _ in range(45):
        time.sleep(1)
        if _is_up():
            return json.dumps(
                {
                    "ok": True,
                    "pid": proc.pid,
                    "url": _base_url(),
                    "config": config_note,
                }
            )
    return json.dumps(
        {
            "ok": False,
            "pid": proc.pid,
            "error": "did not become ready in 45s",
            "config": config_note,
        }
    )


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
        with open(os.path.join(_get_dir(), "package.json"), encoding="utf-8") as f:
            pkg = json.load(f)
        return json.dumps({"ok": True, "version": pkg.get("version")})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def sillytavern_configure(args, **kwargs) -> str:
    """Explicitly re-run auto-configuration."""
    result = _configure()
    return json.dumps({"ok": True, **result})


# ── Register ────────────────────────────────────────────────────────

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
        description="Start SillyTavern (auto-configures secrets/settings from Hermes).",
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
    ctx.register_tool(
        name="sillytavern_configure",
        description="(Re-)run auto-configuration: sync Hermes API keys and settings into SillyTavern.",
        schema=empty,
        handler=sillytavern_configure,
        check_fn=_installed,
    )
