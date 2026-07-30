"""Configuration, sync, lifecycle, and local VRChat OSC for the AIRI plugin.

Architecture (intentionally narrow):
  AIRI  = VRM / TTS / desktop shell (Hermes-managed process worker)
  Hermes gateway `api_server` (:8642/v1) = OpenAI-compatible AI core
  Desktop `hermes serve` (:9119) = session backend (not AIRI's LLM endpoint)
  This plugin = worker supervisor + provider/TTS sync + CDP seed + local OSC

Worker contract:
  - State under ``~/.hermes/airi/worker-state.json`` (not TEMP)
  - start / stop / status / sync / restart via CLI + tools
  - Sync only: read existing ``API_SERVER_KEY`` from ``~/.hermes/.env`` and
    seed it into AIRI openai-compatible credentials via CDP (Hermes already
    owns api_server auth — this plugin does not invent an auth subsystem)
  - CDP seed + renderer reload so Pinia picks up localStorage
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PLUGIN = "airi"
REPO_DEFAULT = Path(__file__).resolve().parents[2] / "vendor" / "airi"
PLUGIN_DIR = Path(__file__).resolve().parent
PLUGIN_ICON = PLUGIN_DIR / "assets" / "icon.png"
LEGACY_STATE_FILE = Path(os.environ.get("TEMP", ".")) / "hermes-airi-process.json"
# Distinct from Hermes Desktop perf CDP (:9222 / :9333) so both Electrons can run together.
DEFAULT_CDP_PORT = 9455
WORKER_ROLE = "airi-desktop-shell"
# Hermes Desktop app id is com.nousresearch.hermes — keep AIRI on its own id + userdata.
CONCURRENT_WITH_DESKTOP = True


def _hermes_airi_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "airi"
    except Exception:
        return Path.home() / ".hermes" / "airi"


HERMES_AIRI_HOME = _hermes_airi_home()


def _state_file() -> Path:
    return HERMES_AIRI_HOME / "worker-state.json"


def _cfg() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        data = load_config_readonly()
        return dict(((data.get("plugins") or {}).get("entries") or {}).get(PLUGIN) or {})
    except Exception:
        return {}


def _repo(values: dict[str, Any] | None = None) -> Path:
    values = values or {}
    value = values.get("repo_root") or _cfg().get("repo_root")
    path = Path(str(value)).expanduser() if value else REPO_DEFAULT
    if not path.is_absolute():
        path = REPO_DEFAULT.parents[1] / path
    return path


def _normalize_base_url(raw: str) -> str:
    """AIRI requires a trailing slash on OpenAI-compatible baseUrl."""
    return str(raw or "").strip().rstrip("/") + "/"


def _candidate_base_urls(values: dict[str, Any] | None = None) -> list[str]:
    """Prefer explicit config, then Hermes gateway API server (:8642), then Desktop serve."""
    values = values or {}
    ordered: list[str] = []
    for raw in (
        values.get("hermes_base_url"),
        _cfg().get("hermes_base_url"),
        os.environ.get("HERMES_AIRI_BASE_URL"),
        "http://127.0.0.1:8642/v1/",
        "http://127.0.0.1:9119/v1/",
    ):
        if not raw:
            continue
        url = _normalize_base_url(str(raw))
        if url not in ordered:
            ordered.append(url)
    return ordered


def _base_url(values: dict[str, Any] | None = None) -> str:
    values = values or {}
    return _candidate_base_urls(values)[0]


def _model(values: dict[str, Any] | None = None) -> str:
    values = values or {}
    return str(
        values.get("hermes_model")
        or _cfg().get("hermes_model")
        or os.environ.get("API_SERVER_MODEL_NAME")
        or "hermes-agent"
    )


def _load_hermes_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path.home() / ".hermes" / ".env", override=False)
    except Exception:
        pass


def _api_key_info(values: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve gateway bearer for CDP seed only.

    Prefers ephemeral ``values.api_key`` (tests/CLI), else ``API_SERVER_KEY``
    from ``~/.hermes/.env``. No plugin-owned secret store or alternate key env.
    Never invent secrets; never echo the raw key in status payloads.
    """
    values = values or {}
    _load_hermes_dotenv()
    candidates = (
        ("values.api_key", values.get("api_key")),
        ("API_SERVER_KEY", os.environ.get("API_SERVER_KEY")),
    )
    for source, key in candidates:
        if key and str(key).strip():
            token = str(key).strip()
            return {
                "api_key": token,
                "source": source,
                "configured": True,
                "is_placeholder": False,
            }
    return {
        "api_key": "hermes-local",
        "source": "placeholder",
        "configured": False,
        "is_placeholder": True,
    }


def _api_key(values: dict[str, Any] | None = None) -> str:
    return str(_api_key_info(values)["api_key"])


def _write_state(data: dict[str, Any]) -> None:
    HERMES_AIRI_HOME.mkdir(parents=True, exist_ok=True)
    path = _state_file()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_state() -> dict[str, Any]:
    for path in (_state_file(), LEGACY_STATE_FILE):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                if path == LEGACY_STATE_FILE and not _state_file().exists():
                    try:
                        _write_state(data)
                    except OSError:
                        pass
                return data
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _clear_state() -> None:
    for path in (_state_file(), LEGACY_STATE_FILE):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except Exception:
        return False


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _probe_hermes(base_url: str, timeout: float = 3.0, api_key: str | None = None) -> dict[str, Any]:
    """Probe OpenAI-compatible /models for sync readiness (not a plugin auth API).

    ``live`` — endpoint reachable (incl. 401/403 when gateway is up but key wrong)
    ``ok``   — Bearer accepted (2xx); seed can talk to Hermes core
    """
    models_url = base_url.rstrip("/") + "/models"
    headers: dict[str, str] = {}
    key = (api_key or "").strip()
    if key and key != "hermes-local":
        headers["Authorization"] = f"Bearer {key}"
    try:
        req = urllib.request.Request(models_url, method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", errors="replace")
            status = int(resp.status)
            accepted = 200 <= status < 300
            return {
                "ok": accepted,
                "live": True,
                "status": status,
                "url": models_url,
                "preview": body[:200] if accepted else "",
            }
    except urllib.error.HTTPError as exc:
        gated = exc.code in {401, 403}
        return {
            "ok": False,
            "live": gated or exc.code < 500,
            "status": int(exc.code),
            "url": models_url,
        }
    except Exception as exc:
        return {
            "ok": False,
            "live": False,
            "url": models_url,
            "error": str(exc),
        }


def _resolve_live_core(values: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pick the first live Hermes OpenAI-compatible base URL."""
    values = values or {}
    key = _api_key(values)
    probes = []
    for base in _candidate_base_urls(values):
        probe = _probe_hermes(base, api_key=key)
        probes.append({"base_url": base, **probe})
        if probe.get("live") or probe.get("ok"):
            return {"ok": True, "base_url": base, "probe": probe, "probes": probes}
    return {"ok": False, "base_url": _candidate_base_urls(values)[0], "probes": probes}


def provider_payload(values: dict[str, Any] | None = None) -> dict[str, Any]:
    values = values or {}
    return {
        "definitionId": "openai-compatible",
        "name": "Hermes Agent",
        "config": {
            "apiKey": _api_key(values),
            "baseUrl": _base_url(values),
            "model": _model(values),
        },
        "notes": (
            "Synced by hermes airi sync/start. AIRI chat core = Hermes Agent "
            "OpenAI-compatible /v1. Secrets stay in ~/.hermes/.env."
        ),
    }


def _hermes_tts_section() -> dict[str, Any]:
    """Read non-secret TTS behaviour from config.yaml (provider + nested sections)."""
    try:
        from hermes_cli.config import load_config_readonly

        data = load_config_readonly() or {}
        tts = data.get("tts") or {}
        return dict(tts) if isinstance(tts, dict) else {}
    except Exception:
        return {}


def _probe_http(url: str, timeout: float = 2.0) -> dict[str, Any]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": 200 <= int(resp.status) < 300, "live": True, "status": int(resp.status), "url": url}
    except urllib.error.HTTPError as exc:
        gated = exc.code in {401, 403}
        return {
            "ok": False,
            "live": gated or exc.code < 500,
            "status": int(exc.code),
            "url": url,
            "auth_required": gated,
        }
    except Exception as exc:
        return {"ok": False, "live": False, "url": url, "error": str(exc)}


def tts_payload(values: dict[str, Any] | None = None) -> dict[str, Any]:
    """Map Hermes TTS config → AIRI speech provider seed (no secrets echoed).

    Hermes ``tts.provider: irodori-tts`` → AIRI ``openai-compatible-audio-speech``
    pointed at the local irodori OpenAI-compatible ``/v1/audio/speech`` server.
    Stage.vue prefers credentials ``model`` / ``voice`` for that provider id.
    """
    values = values or {}
    tts = _hermes_tts_section()
    provider = str(
        values.get("tts_provider")
        or _cfg().get("tts_provider")
        or tts.get("provider")
        or ""
    ).strip().lower()

    # Optional explicit overrides from plugin config / CLI values.
    if values.get("tts_base_url") or _cfg().get("tts_base_url"):
        base = _normalize_base_url(str(values.get("tts_base_url") or _cfg().get("tts_base_url")))
        model = str(values.get("tts_model") or _cfg().get("tts_model") or "tts-1")
        voice = str(values.get("tts_voice") or _cfg().get("tts_voice") or "alloy")
        return {
            "ok": True,
            "source": "plugin_override",
            "hermes_provider": provider or "override",
            "airi_provider": "openai-compatible-audio-speech",
            "config": {
                "apiKey": str(values.get("tts_api_key") or "local"),
                "baseUrl": base,
                "model": model,
                "voice": voice,
            },
        }

    if provider in {"irodori-tts", "irodori", "irodori_tts"}:
        irodori = tts.get("irodori") if isinstance(tts.get("irodori"), dict) else {}
        try:
            from plugins.irodori_tts.core import settings as irodori_settings

            cfg = irodori_settings(tts)
            base = _normalize_base_url(cfg.base_url.rstrip("/") + "/v1")
            model = str(cfg.model)
            voice = str(cfg.voice)
        except Exception:
            base = _normalize_base_url(
                str(irodori.get("base_url") or irodori.get("url") or "http://127.0.0.1:8088") + "/v1"
            )
            model = str(irodori.get("model") or "irodori-tts")
            voice = str(irodori.get("voice") or "hakua")
        return {
            "ok": True,
            "source": "hermes_tts.irodori",
            "hermes_provider": provider,
            "airi_provider": "openai-compatible-audio-speech",
            "config": {
                # irodori is local; AIRI still wants a non-empty apiKey field.
                "apiKey": "local",
                "baseUrl": base,
                "model": model,
                "voice": voice,
            },
        }

    if provider in {"openai", "openai-tts"}:
        _load_hermes_dotenv()
        openai_cfg = tts.get("openai") if isinstance(tts.get("openai"), dict) else {}
        key = (
            str(values.get("tts_api_key") or "").strip()
            or str(os.environ.get("VOICE_TOOLS_OPENAI_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
            or "local"
        )
        return {
            "ok": bool(key and key != "local"),
            "source": "hermes_tts.openai",
            "hermes_provider": provider,
            "airi_provider": "openai-audio-speech",
            "config": {
                "apiKey": key,
                "baseUrl": _normalize_base_url(
                    str(openai_cfg.get("base_url") or "https://api.openai.com/v1/")
                ),
                "model": str(openai_cfg.get("model") or "gpt-4o-mini-tts"),
                "voice": str(openai_cfg.get("voice") or "alloy"),
            },
        }

    if provider in {"edge", "edge-tts"}:
        # AIRI has no Edge TTS provider; keep speech-noop and report honestly.
        return {
            "ok": False,
            "source": "hermes_tts.edge",
            "hermes_provider": provider,
            "airi_provider": "speech-noop",
            "config": {},
            "hint": "Hermes edge TTS has no AIRI counterpart; set tts.provider to irodori-tts",
        }

    return {
        "ok": False,
        "source": "unconfigured",
        "hermes_provider": provider or None,
        "airi_provider": "speech-noop",
        "config": {},
        "hint": "Set tts.provider (irodori-tts recommended) in ~/.hermes/config.yaml",
    }


def _tts_status(values: dict[str, Any] | None = None) -> dict[str, Any]:
    """TTS sync readiness for status — never echoes secrets."""
    values = values or {}
    payload = tts_payload(values)
    cfg = payload.get("config") or {}
    base = str(cfg.get("baseUrl") or "")
    probe: dict[str, Any] = {}
    if base and payload.get("airi_provider") not in {None, "speech-noop"}:
        # irodori exposes /health on the host root (not under /v1).
        root = base.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        probe = _probe_http(root.rstrip("/") + "/health")
        if not probe.get("live"):
            probe = _probe_http(base.rstrip("/") + "/models")
    ready = bool(
        payload.get("ok")
        and payload.get("airi_provider") not in {None, "speech-noop"}
        and cfg.get("baseUrl")
        and cfg.get("model")
    )
    return {
        "ready": ready,
        "synced_provider": payload.get("airi_provider"),
        "hermes_provider": payload.get("hermes_provider"),
        "source": payload.get("source"),
        "base_url": base or None,
        "model": cfg.get("model"),
        "voice": cfg.get("voice"),
        "server_live": bool(probe.get("live") or probe.get("ok")) if probe else None,
        "server_status": probe.get("status") if probe else None,
        "hint": None if ready else payload.get("hint"),
    }


def _provider_runtime_status(
    values: dict[str, Any] | None = None,
    probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """AI-provider sync readiness (API_SERVER_KEY present + Hermes /models probe)."""
    values = values or {}
    info = _api_key_info(values)
    probe = probe or _probe_hermes(_base_url(values), api_key=info["api_key"])
    ready = bool(info["configured"] and probe.get("ok"))
    return {
        "ready": ready,
        "definition_id": "openai-compatible",
        "base_url": _base_url(values),
        "model": _model(values),
        "api_key_configured": bool(info["configured"]),
        "api_key_source": info["source"],
        "core_live": bool(probe.get("live")),
        "core_ok": bool(probe.get("ok")),
        "core_status": probe.get("status"),
        "hint": (
            None
            if ready
            else (
                "Set API_SERVER_KEY in ~/.hermes/.env and restart gateway"
                if not info["configured"]
                else "Hermes /v1/models rejected Bearer — check API_SERVER_KEY / api_server"
            )
        ),
    }


def configure_hermes(values: dict[str, Any] | None = None, **_: Any) -> str:
    values = values or {}
    repo = _repo(values)
    if not repo.is_dir() or not (repo / "package.json").exists():
        return _json(
            {
                "ok": False,
                "error": "AIRI submodule is not initialized",
                "repo_root": str(repo),
                "hint": "git submodule update --init --recursive vendor/airi",
            }
        )

    HERMES_AIRI_HOME.mkdir(parents=True, exist_ok=True)
    provider = provider_payload(values)
    tts = tts_payload(values)
    # Never persist the raw API key on disk; CDP seed keeps it in-memory only.
    disk = json.loads(json.dumps(provider))
    disk["config"].pop("apiKey", None)
    disk["config"]["apiKeyEnv"] = "API_SERVER_KEY"
    path = HERMES_AIRI_HOME / "hermes-provider.json"
    path.write_text(json.dumps(disk, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    tts_disk = json.loads(json.dumps(tts))
    if isinstance(tts_disk.get("config"), dict):
        tts_disk["config"].pop("apiKey", None)
        if tts.get("ok"):
            tts_disk["config"]["apiKeyEnv"] = "local-or-VOICE_TOOLS_OPENAI_KEY"
    tts_path = HERMES_AIRI_HOME / "hermes-tts.json"
    tts_path.write_text(json.dumps(tts_disk, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    seed = _localstorage_seed(provider, tts)
    return _json(
        {
            "ok": True,
            "provider_file": str(path),
            "tts_file": str(tts_path),
            "provider": disk,
            "tts": tts_disk,
            "localstorage_seed": {
                "flat": {
                    k: ("<redacted>" if "hint" in k else v)
                    for k, v in (seed.get("flat") or {}).items()
                },
                "credentials_patch_keys": list((seed.get("credentials_patch") or {}).keys()),
                "added_patch": seed.get("added_patch"),
            },
            "provider": _provider_runtime_status(values),
            "tts_status": _tts_status(values),
        }
    )


def _localstorage_seed(provider: dict[str, Any], tts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the AIRI renderer seed plan consumed by CDP (merge-safe).

    AIRI consciousness reads ``settings/consciousness/*`` + credentials.
    Speech (openai-compatible-audio-speech) prefers credentials ``model``/``voice``.
    Credentials must be *merged* — a full replace races Pinia defaults and drops
    other provider stubs (and historically left consciousness empty after reload).
    """
    cfg = provider.get("config") or {}
    base = str(cfg.get("baseUrl") or "").rstrip("/") + "/"
    model = str(cfg.get("model") or "hermes-agent")
    api_key = str(cfg.get("apiKey") or "hermes-local")
    instance_id = "hermes-agent-openai-compatible"
    catalog = {
        instance_id: {
            "id": instance_id,
            "definitionId": "openai-compatible",
            "name": "Hermes Agent",
            "config": {"apiKey": api_key, "baseUrl": base},
            "validated": True,
            "validationBypassed": True,
        }
    }

    cred_patch: dict[str, dict[str, Any]] = {
        "openai-compatible": {"apiKey": api_key, "baseUrl": base},
    }
    added_patch: dict[str, bool] = {"openai-compatible": True}
    flat: dict[str, str] = {
        "settings/consciousness/active-provider": "openai-compatible",
        "settings/consciousness/active-model": model,
        "onboarding/completed": "true",
        "hermes/airi/inference-providers-hint": json.dumps(catalog, ensure_ascii=False),
    }

    tts = tts or {}
    tts_cfg = tts.get("config") if isinstance(tts.get("config"), dict) else {}
    airi_speech = str(tts.get("airi_provider") or "speech-noop")
    if tts.get("ok") and airi_speech not in {"", "speech-noop"} and tts_cfg.get("baseUrl"):
        speech_base = str(tts_cfg.get("baseUrl")).rstrip("/") + "/"
        speech_entry = {
            "apiKey": str(tts_cfg.get("apiKey") or "local"),
            "baseUrl": speech_base,
        }
        if tts_cfg.get("model"):
            speech_entry["model"] = str(tts_cfg["model"])
        if tts_cfg.get("voice"):
            speech_entry["voice"] = str(tts_cfg["voice"])
        cred_patch[airi_speech] = speech_entry
        added_patch[airi_speech] = True
        flat["settings/speech/active-provider"] = airi_speech
        flat["settings/speech/active-model"] = str(tts_cfg.get("model") or "")
        if tts_cfg.get("voice"):
            flat["settings/speech/voice"] = str(tts_cfg["voice"])
        # Unmute assistant speech when we deliberately wire a real TTS backend.
        flat["settings/speech/output-muted"] = "false"

    return {
        "flat": flat,
        "credentials_patch": cred_patch,
        "added_patch": added_patch,
        "catalog": catalog,
        # Legacy flat view used by older tests / status redaction.
        "legacy_keys": {
            **flat,
            "settings/credentials/providers": json.dumps(cred_patch, ensure_ascii=False),
            "settings/providers/added": json.dumps(added_patch, ensure_ascii=False),
        },
    }


def _cdp_targets(port: int, timeout: float = 2.0) -> list[dict[str, Any]]:
    url = f"http://127.0.0.1:{port}/json/list"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _cdp_pick_page(targets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer the AIRI renderer page; skip blank/devtools origins that deny localStorage."""
    pages = [
        t
        for t in targets
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
    ]
    if not pages:
        return None

    def score(page: dict[str, Any]) -> tuple[int, str]:
        url = str(page.get("url") or "")
        low = url.lower()
        if not url or low in {"about:blank", "about:srcdoc"}:
            return (0, url)
        if low.startswith("devtools://") or low.startswith("chrome-extension://"):
            return (0, url)
        if "localhost:5173" in low or "127.0.0.1:5173" in low:
            # Prefer the chat surface over beat-sync helper windows.
            if "#/chat" in low:
                return (100, url)
            if low.rstrip("/").endswith(":5173") or low.endswith(":5173/#/") or "#/" in low:
                return (90, url)
            return (80, url)
        if low.startswith("http://") or low.startswith("https://") or low.startswith("app://"):
            return (50, url)
        return (10, url)

    pages.sort(key=score, reverse=True)
    best = pages[0]
    if score(best)[0] <= 0:
        return None
    return best


def _cdp_call(ws: Any, msg_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"id": msg_id, "method": method, "params": params or {}}
    ws.send(json.dumps(payload))
    deadline = time.time() + 8.0
    while time.time() < deadline:
        raw = ws.recv()
        reply = json.loads(raw)
        if reply.get("id") == msg_id:
            return reply
    return {"error": {"message": f"timeout waiting for CDP id={msg_id}"}}


def _cdp_readback_expr() -> str:
    return """
(() => {
  const get = (k) => localStorage.getItem(k);
  let creds = {};
  try { creds = JSON.parse(get('settings/credentials/providers') || '{}') || {}; } catch (e) {}
  const oc = creds['openai-compatible'] || {};
  const sp = creds['openai-compatible-audio-speech'] || creds['openai-audio-speech'] || {};
  return {
    consciousnessProvider: get('settings/consciousness/active-provider') || '',
    consciousnessModel: get('settings/consciousness/active-model') || '',
    speechProvider: get('settings/speech/active-provider') || '',
    speechModel: get('settings/speech/active-model') || '',
    speechVoice: get('settings/speech/voice') || '',
    openaiCompatibleBase: oc.baseUrl || '',
    openaiCompatibleHasKey: !!(oc.apiKey && String(oc.apiKey).length > 0),
    speechBase: sp.baseUrl || '',
    speechHasModel: !!(sp.model && String(sp.model).length > 0),
    speechCredVoice: sp.voice || '',
  };
})()
"""


def _cdp_seed_expr(seed: dict[str, Any]) -> str:
    """Merge-safe localStorage seed (does not clobber unrelated provider stubs)."""
    flat = seed.get("flat") or {}
    cred_patch = seed.get("credentials_patch") or {}
    added_patch = seed.get("added_patch") or {}
    catalog = seed.get("catalog") or {}
    return f"""
(() => {{
  const credPatch = {json.dumps(cred_patch, ensure_ascii=False)};
  const addedPatch = {json.dumps(added_patch, ensure_ascii=False)};
  const flat = {json.dumps(flat, ensure_ascii=False)};
  const catalog = {json.dumps(catalog, ensure_ascii=False)};

  const mergeJson = (key, patch) => {{
    let cur = {{}};
    try {{ cur = JSON.parse(localStorage.getItem(key) || '{{}}') || {{}}; }} catch (e) {{ cur = {{}}; }}
    if (key === 'settings/credentials/providers') {{
      for (const [id, cfg] of Object.entries(patch)) {{
        cur[id] = Object.assign({{}}, cur[id] || {{}}, cfg);
      }}
    }} else {{
      Object.assign(cur, patch);
    }}
    localStorage.setItem(key, JSON.stringify(cur));
    return cur;
  }};

  mergeJson('settings/credentials/providers', credPatch);
  mergeJson('settings/providers/added', addedPatch);
  for (const [k, v] of Object.entries(flat)) {{
    localStorage.setItem(k, String(v));
  }}

  // Best-effort IndexedDB upsert for unstorage mount local:providers (airi-local).
  const idbPromise = new Promise((resolve) => {{
    try {{
      const openReq = indexedDB.open('keyval-store');
      openReq.onerror = () => resolve({{ idb: 'open-failed' }});
      openReq.onsuccess = () => {{
        try {{
          const db = openReq.result;
          const stores = Array.from(db.objectStoreNames || []);
          if (!stores.includes('keyval')) {{
            db.close();
            resolve({{ idb: 'no-keyval', stores }});
            return;
          }}
          const tx = db.transaction('keyval', 'readwrite');
          const store = tx.objectStore('keyval');
          // unstorage indexedb driver keys are typically base-prefixed.
          const candidates = ['airi-local:providers', 'providers', 'local:providers'];
          let wrote = false;
          const tryWrite = (idx) => {{
            if (idx >= candidates.length) {{
              db.close();
              resolve({{ idb: wrote ? 'wrote' : 'missed', stores }});
              return;
            }}
            const key = candidates[idx];
            const getReq = store.get(key);
            getReq.onsuccess = () => {{
              const existing = getReq.result;
              let next = catalog;
              if (existing && typeof existing === 'object' && !Array.isArray(existing)) {{
                next = Object.assign({{}}, existing, catalog);
              }}
              const putReq = store.put(next, key);
              putReq.onsuccess = () => {{ wrote = true; tryWrite(idx + 1); }};
              putReq.onerror = () => tryWrite(idx + 1);
            }};
            getReq.onerror = () => tryWrite(idx + 1);
          }};
          tryWrite(0);
        }} catch (e) {{
          resolve({{ idb: String(e) }});
        }}
      }};
    }} catch (e) {{
      resolve({{ idb: String(e) }});
    }}
  }});

  return idbPromise.then((idb) => ({{
    idb,
    consciousnessProvider: localStorage.getItem('settings/consciousness/active-provider'),
    consciousnessModel: localStorage.getItem('settings/consciousness/active-model'),
    speechProvider: localStorage.getItem('settings/speech/active-provider'),
    speechModel: localStorage.getItem('settings/speech/active-model'),
  }}));
}})()
"""


def _readback_matches(seed: dict[str, Any], readback: dict[str, Any]) -> bool:
    flat = seed.get("flat") or {}
    want_provider = str(flat.get("settings/consciousness/active-provider") or "openai-compatible")
    want_model = str(flat.get("settings/consciousness/active-model") or "")
    if str(readback.get("consciousnessProvider") or "") != want_provider:
        return False
    if want_model and str(readback.get("consciousnessModel") or "") != want_model:
        return False
    if not readback.get("openaiCompatibleHasKey"):
        return False
    want_speech = flat.get("settings/speech/active-provider")
    if want_speech and str(readback.get("speechProvider") or "") != str(want_speech):
        return False
    return True


def _cdp_eval_value(reply: dict[str, Any]) -> Any:
    """Unwrap CDP Runtime.evaluate returnByValue payload."""
    result = (reply.get("result") or {}).get("result")
    if isinstance(result, dict) and "value" in result:
        return result.get("value")
    return result


def _cdp_seed_localstorage(port: int, seed: dict[str, Any], wait_s: float = 45.0) -> dict[str, Any]:
    """Seed AIRI renderer localStorage through Electron remote debugging, then reload.

    Merge credentials (do not replace). Use CDP ``Page.reload`` (not in-page
    ``location.reload`` alone) and verify keys after reload; re-seed once if
    Pinia wiped consciousness.
    """
    # Accept legacy flat dicts from older callers/tests.
    if "flat" not in seed and any(str(k).startswith("settings/") for k in seed):
        seed = {
            "flat": {
                k: v
                for k, v in seed.items()
                if k not in {"credentials_patch", "added_patch", "catalog"}
            },
            "credentials_patch": {},
            "added_patch": {},
            "catalog": {},
        }

    deadline = time.time() + wait_s
    page = None
    while time.time() < deadline:
        page = _cdp_pick_page(_cdp_targets(port))
        if page:
            break
        time.sleep(0.5)
    if not page:
        return {"ok": False, "error": "CDP page target not found", "port": port}

    try:
        import websocket  # websocket-client
    except ImportError:
        return {
            "ok": False,
            "error": "websocket-client not installed",
            "hint": "uv pip install websocket-client",
        }

    def _run_once(ws_url: str) -> dict[str, Any]:
        ws = websocket.create_connection(ws_url, timeout=8)
        try:
            msg_id = 1
            nav = _cdp_call(
                ws,
                msg_id,
                "Runtime.evaluate",
                {
                    "expression": (
                        "(() => { try { if (!String(location.hash||'').includes('/chat')) "
                        "{ location.hash = '#/chat'; } return location.href; } catch (e) "
                        "{ return String(e); } })()"
                    ),
                    "returnByValue": True,
                },
            )
            msg_id += 1
            time.sleep(0.35)
            seeded = _cdp_call(
                ws,
                msg_id,
                "Runtime.evaluate",
                {
                    "expression": _cdp_seed_expr(seed),
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            msg_id += 1
            _cdp_call(ws, msg_id, "Page.enable", {})
            msg_id += 1
            reloaded = _cdp_call(ws, msg_id, "Page.reload", {"ignoreCache": False})
            return {
                "nav": _cdp_eval_value(nav),
                "seed": _cdp_eval_value(seeded),
                "reloaded": not bool(reloaded.get("error")),
            }
        finally:
            ws.close()

    def _readback_from(ws_url: str) -> dict[str, Any]:
        ws = websocket.create_connection(ws_url, timeout=8)
        try:
            reply = _cdp_call(
                ws,
                1,
                "Runtime.evaluate",
                {"expression": _cdp_readback_expr(), "returnByValue": True},
            )
        finally:
            ws.close()
        value = _cdp_eval_value(reply)
        return value if isinstance(value, dict) else {}

    try:
        first = _run_once(str(page["webSocketDebuggerUrl"]))
        time.sleep(2.5)
        page2 = _cdp_pick_page(_cdp_targets(port)) or page
        readback = _readback_from(str(page2["webSocketDebuggerUrl"]))
        ok = _readback_matches(seed, readback)
        reseed_pass = False
        if not ok:
            page3 = _cdp_pick_page(_cdp_targets(port)) or page2
            _run_once(str(page3["webSocketDebuggerUrl"]))
            time.sleep(2.5)
            page4 = _cdp_pick_page(_cdp_targets(port)) or page3
            readback = _readback_from(str(page4["webSocketDebuggerUrl"]))
            ok = _readback_matches(seed, readback)
            reseed_pass = True

        return {
            "ok": ok,
            "port": port,
            "target": (page2 or page).get("url"),
            "keys": list((seed.get("flat") or {}).keys()),
            "reloaded": bool(first.get("reloaded")),
            "nav": first.get("nav"),
            "seed": first.get("seed"),
            "readback": {
                "consciousnessProvider": readback.get("consciousnessProvider"),
                "consciousnessModel": readback.get("consciousnessModel"),
                "speechProvider": readback.get("speechProvider"),
                "speechModel": readback.get("speechModel"),
                "openaiCompatibleHasKey": readback.get("openaiCompatibleHasKey"),
                "speechBase": readback.get("speechBase"),
            },
            "reseeding": reseed_pass,
            "hint": (
                None
                if ok
                else "CDP seed wrote keys but post-reload readback mismatched — re-run hermes airi sync"
            ),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "port": port, "target": page.get("url")}


def _png_color_type(path: Path) -> int | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return raw[25] if len(raw) > 25 else None


def _icon_status(repo: Path) -> dict[str, Any]:
    """Read-only icon health (no mutation)."""
    resources_icon = repo / "apps" / "stage-tamagotchi" / "resources" / "icon.png"
    if not resources_icon.is_file():
        return {"path": str(resources_icon), "ok": False, "error": "missing"}
    color_type = _png_color_type(resources_icon)
    # PNG colour type 3 = indexed/palette (Electron nativeImage may invert on Windows).
    palette = color_type == 3
    return {
        "path": str(resources_icon),
        "ok": not palette,
        "png_color_type": color_type,
        "palette": palette,
        "hint": (
            "Indexed PNG — run hermes airi start to repair RGBA icon"
            if palette
            else None
        ),
    }


def _ensure_rgba_icon(repo: Path) -> dict[str, Any]:
    """Repair Electron tray/window icon if resources/icon.png is indexed (palette).

    Indexed PNGs can render as colour-inverted on Windows Electron nativeImage.
    Prefer plugin RGBA asset, then build/icon.png.
    """
    resources_icon = repo / "apps" / "stage-tamagotchi" / "resources" / "icon.png"
    build_icon = repo / "apps" / "stage-tamagotchi" / "build" / "icon.png"
    result: dict[str, Any] = {"path": str(resources_icon), "repaired": False}
    if not resources_icon.is_file():
        result["error"] = "resources/icon.png missing"
        return result

    color_type = _png_color_type(resources_icon)
    result["png_color_type_before"] = color_type
    if color_type != 3:
        result["mode_after"] = "ok"
        return result

    src = PLUGIN_ICON if PLUGIN_ICON.is_file() else build_icon
    if not src.is_file():
        result["error"] = "no RGBA source icon available"
        return result

    try:
        from PIL import Image

        fixed = Image.open(src).convert("RGBA")
        resources_icon.parent.mkdir(parents=True, exist_ok=True)
        fixed.save(resources_icon, format="PNG", optimize=True)
    except Exception:
        shutil.copy2(src, resources_icon)

    result["repaired"] = True
    result["source"] = str(src)
    result["reason"] = "palette-png-electron-nativeimage"
    result["png_color_type_after"] = _png_color_type(resources_icon)
    return result


def _build_seed(values: dict[str, Any] | None = None) -> dict[str, Any]:
    values = values or {}
    return _localstorage_seed(provider_payload(values), tts_payload(values))


def sync(values: dict[str, Any] | None = None, **_: Any) -> str:
    """One-shot: resolve live Hermes OpenAI core + write provider/TTS templates + CDP seed."""
    values = dict(values or {})
    resolved = _resolve_live_core(values)
    values["hermes_base_url"] = resolved["base_url"]
    cfg_result = json.loads(configure_hermes(values))
    if not cfg_result.get("ok"):
        return _json(cfg_result)
    probe = resolved.get("probe") or (resolved.get("probes") or [None])[-1] or {}
    provider_stat = _provider_runtime_status(values, probe=probe)
    tts_stat = _tts_status(values)
    # If worker already running, push credentials + TTS again.
    reseed: dict[str, Any] | None = None
    state = _read_state()
    if _pid_alive(int(state.get("pid") or 0)):
        cdp_port = int(state.get("cdp_port") or DEFAULT_CDP_PORT)
        reseed = _cdp_seed_localstorage(cdp_port, _build_seed(values), wait_s=10.0)
        updated = {
            **state,
            "cdp_seed": reseed,
            "hermes_base_url": values["hermes_base_url"],
            "tts": tts_stat,
            "provider_sync": provider_stat,
        }
        _write_state(updated)
    return _json(
        {
            "ok": True,
            "synced": True,
            "worker_role": WORKER_ROLE,
            "provider_file": cfg_result.get("provider_file"),
            "tts_file": cfg_result.get("tts_file"),
            "hermes_base_url": resolved["base_url"],
            "hermes_model": _model(values),
            "hermes_probe": probe,
            "hermes_probes": resolved.get("probes"),
            "provider": provider_stat,
            "tts": tts_stat,
            "cdp_reseed": reseed,
            "architecture": (
                "AIRI=Hermes process worker (VRM/TTS/UI); "
                "Hermes api_server=AI core; plugin=supervisor+provider/TTS sync+OSC"
            ),
            "next": (
                ["hermes airi start"]
                if resolved.get("ok") and provider_stat.get("ready")
                else _sync_next_hints(resolved, provider_stat)
            ),
        }
    )


def _sync_next_hints(resolved: dict[str, Any], provider: dict[str, Any] | None = None) -> list[str]:
    probes = list(resolved.get("probes") or [])
    refused = any("10061" in str((p or {}).get("error") or "") for p in probes)
    provider = provider or {}
    if refused:
        return [
            "Gateway api_server not listening — run: hermes gateway restart",
            "Confirm API_SERVER_KEY in ~/.hermes/.env (OpenAI core on :8642)",
            "hermes airi start",
        ]
    if not provider.get("api_key_configured"):
        return [
            "Set API_SERVER_KEY in ~/.hermes/.env and restart gateway (OpenAI core on :8642)",
            "hermes airi start",
        ]
    if provider.get("core_live") and not provider.get("core_ok"):
        return [
            "API_SERVER_KEY present but /v1/models rejected Bearer — rotate key or restart gateway",
            "hermes airi sync",
        ]
    return [
        "Probe Hermes /v1/models failed — check gateway api_server on :8642",
        "hermes airi start",
    ]


def _worker_health(state: dict[str, Any], values: dict[str, Any] | None = None) -> dict[str, Any]:
    values = values or {}
    pid = int(state.get("pid") or 0)
    running = _pid_alive(pid)
    cdp_port = int(state.get("cdp_port") or DEFAULT_CDP_PORT)
    cdp_pages = _cdp_targets(cdp_port) if running else []
    page = _cdp_pick_page(cdp_pages) if cdp_pages else None
    probe = _probe_hermes(_base_url(values), api_key=_api_key(values))
    provider_stat = _provider_runtime_status(values, probe=probe)
    tts_stat = _tts_status(values)
    seed = state.get("cdp_seed") if isinstance(state.get("cdp_seed"), dict) else {}
    readback = seed.get("readback") if isinstance(seed.get("readback"), dict) else {}
    provider_seeded = None
    if readback:
        provider_seeded = bool(
            readback.get("consciousnessProvider") == "openai-compatible"
            and readback.get("openaiCompatibleHasKey")
        )
    tts_seeded = None
    if readback and tts_stat.get("synced_provider") not in {None, "speech-noop"}:
        tts_seeded = readback.get("speechProvider") == tts_stat.get("synced_provider")
    healthy = bool(
        running
        and page
        and provider_stat.get("ready")
        and seed.get("ok", True)
    )
    return {
        "role": WORKER_ROLE,
        "healthy": healthy,
        "running": running,
        "pid": pid or None,
        "cdp_port": cdp_port,
        "cdp_page": (page or {}).get("url"),
        "cdp_targets": len(cdp_pages),
        "provider": {**provider_stat, "cdp_seeded": provider_seeded},
        "tts": {**tts_stat, "cdp_seeded": tts_seeded},
        "hermes_probe": {
            "live": probe.get("live"),
            "ok": probe.get("ok"),
            "status": probe.get("status"),
            "url": probe.get("url"),
            "error": probe.get("error"),
        },
        "last_cdp_seed_ok": seed.get("ok"),
        "last_cdp_readback": readback or None,
        "state_file": str(_state_file()),
        "concurrent_with_hermes_desktop": CONCURRENT_WITH_DESKTOP,
        "isolation": {
            "userdata": str(HERMES_AIRI_HOME / "userdata"),
            "app_user_model_id": "ai.moeru.airi",
            "hermes_desktop_app_user_model_id": "com.nousresearch.hermes",
            "cdp_port": cdp_port,
            "note": (
                "AIRI and Hermes Desktop are both Electron; isolated userData + "
                "distinct app ids + CDP :9455 allow side-by-side launch."
            ),
        },
    }


def status(values: dict[str, Any] | None = None, **_: Any) -> str:
    values = values or {}
    repo = _repo(values)
    state = _read_state()
    health = _worker_health(state, values)
    return _json(
        {
            "ok": True,
            "plugin": PLUGIN,
            "worker": health,
            "repo_root": str(repo),
            "submodule": (repo / ".git").exists() or (repo / "package.json").exists(),
            "airi_package": str(repo / "package.json"),
            "hermes_openai_base_url": _base_url(values),
            "hermes_model": _model(values),
            "hermes_probe": health.get("hermes_probe"),
            "provider": health.get("provider"),
            "tts": health.get("tts"),
            "provider_file": str(HERMES_AIRI_HOME / "hermes-provider.json"),
            "provider_file_exists": (HERMES_AIRI_HOME / "hermes-provider.json").is_file(),
            "tts_file": str(HERMES_AIRI_HOME / "hermes-tts.json"),
            "tts_file_exists": (HERMES_AIRI_HOME / "hermes-tts.json").is_file(),
            "airi_pid": health.get("pid"),
            "airi_running": health.get("running"),
            "cdp_port": health.get("cdp_port"),
            "icon": _icon_status(repo) if repo.is_dir() else {"skipped": True},
            "vrchat_osc": {
                "host": str(
                    values.get("vrchat_osc_host")
                    or _cfg().get("vrchat_osc_host")
                    or "127.0.0.1"
                ),
                "port": int(
                    values.get("vrchat_osc_port")
                    or _cfg().get("vrchat_osc_port")
                    or 9000
                ),
            },
            "architecture": (
                "AIRI=Hermes process worker; Hermes api_server=AI core; "
                "plugin=supervisor+provider/TTS sync+local OSC"
            ),
        }
    )


def _seed_running_worker(values: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    cdp_port = int(state.get("cdp_port") or DEFAULT_CDP_PORT)
    seed = _cdp_seed_localstorage(cdp_port, _build_seed(values), wait_s=8.0)
    provider_stat = _provider_runtime_status(values)
    tts_stat = _tts_status(values)
    updated = {
        **state,
        "cdp_seed": seed,
        "hermes_base_url": _base_url(values),
        "tts": tts_stat,
        "provider_sync": provider_stat,
        "worker_role": WORKER_ROLE,
        "last_sync_at": time.time(),
    }
    _write_state(updated)
    return updated


def start(values: dict[str, Any] | None = None, **_: Any) -> str:
    values = values or {}
    repo = _repo(values)
    package = repo / "package.json"
    if not package.exists():
        return _json({"ok": False, "error": "AIRI checkout/package.json not found", "repo_root": str(repo)})

    icon_fix = _ensure_rgba_icon(repo)
    sync_payload = json.loads(sync(values))
    if not sync_payload.get("ok"):
        return _json(sync_payload)

    state = _read_state()
    if _pid_alive(int(state.get("pid") or 0)):
        updated = _seed_running_worker(values, state)
        return _json(
            {
                "ok": True,
                "already_running": True,
                "worker_role": WORKER_ROLE,
                "concurrent_with_hermes_desktop": CONCURRENT_WITH_DESKTOP,
                "sync": sync_payload,
                "icon": icon_fix,
                "worker": _worker_health(updated, values),
                **updated,
            }
        )

    cdp_port = int(values.get("cdp_port") or _cfg().get("cdp_port") or DEFAULT_CDP_PORT)
    userdata = HERMES_AIRI_HOME / "userdata"
    userdata.mkdir(parents=True, exist_ok=True)

    pnpm = shutil.which("pnpm.cmd") or shutil.which("pnpm") or "pnpm.cmd"
    # electron-vite forwards args after `--` to Electron.
    # Chromium 111+ rejects CDP websockets without an explicit allow-origins.
    # Do NOT pass Desktop flags or kill Hermes.exe — concurrent Electrons are supported.
    command = [
        pnpm,
        "dev:tamagotchi",
        "--",
        f"--remote-debugging-port={cdp_port}",
        "--remote-allow-origins=*",
    ]
    env = os.environ.copy()
    # Isolate AIRI Electron userData from Hermes Desktop (and from stock AIRI installs).
    # Electron single-instance lock is scoped to userData → Desktop + AIRI can coexist.
    env["APP_USER_DATA_PATH"] = str(userdata)
    env["HERMES_AIRI_BASE_URL"] = _base_url(values)
    env["HERMES_AIRI_MODEL"] = _model(values)
    # Forward gateway key for any AIRI-side readers; CDP remains the Pinia path.
    # Do not invent HERMES_AIRI_API_KEY — Hermes auth is API_SERVER_KEY only.
    key_info = _api_key_info(values)
    if key_info["configured"]:
        env.setdefault("API_SERVER_KEY", key_info["api_key"])

    try:
        proc = subprocess.Popen(
            command,
            cwd=str(repo),
            env=env,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return _json(
            {
                "ok": False,
                "error": str(exc),
                "hint": "Install Node.js 24 and pnpm 10, then run pnpm install in vendor/airi",
            }
        )

    seed = _cdp_seed_localstorage(cdp_port, _build_seed(values), wait_s=60.0)
    provider_stat = _provider_runtime_status(values)
    tts_stat = _tts_status(values)
    state = {
        "pid": proc.pid,
        "command": command,
        "repo_root": str(repo),
        "started_at": time.time(),
        "cdp_port": cdp_port,
        "userdata": str(userdata),
        "hermes_base_url": _base_url(values),
        "cdp_seed": seed,
        "tts": tts_stat,
        "provider_sync": provider_stat,
        "worker_role": WORKER_ROLE,
        "icon": icon_fix,
        "last_sync_at": time.time(),
    }
    _write_state(state)
    return _json(
        {
            "ok": True,
            "worker_role": WORKER_ROLE,
            "concurrent_with_hermes_desktop": CONCURRENT_WITH_DESKTOP,
            "sync": sync_payload,
            "worker": _worker_health(state, values),
            **state,
        }
    )


def stop(values: dict[str, Any] | None = None, **_: Any) -> str:
    state = _read_state()
    pid = int((values or {}).get("pid") or state.get("pid") or 0)
    if not _pid_alive(pid):
        _clear_state()
        return _json({"ok": True, "stopped": False, "reason": "not running", "pid": pid})
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )
        else:
            os.kill(pid, 15)
        _clear_state()
        return _json({"ok": True, "stopped": True, "pid": pid, "worker_role": WORKER_ROLE})
    except OSError as exc:
        return _json({"ok": False, "error": str(exc), "pid": pid})


def restart(values: dict[str, Any] | None = None, **_: Any) -> str:
    """Stop then start the AIRI process worker (re-syncs provider/TTS seed)."""
    values = values or {}
    stopped = json.loads(stop(values))
    started = json.loads(start(values))
    return _json(
        {
            "ok": bool(started.get("ok")),
            "worker_role": WORKER_ROLE,
            "stopped": stopped,
            "started": started,
        }
    )


def _osc_string(value: str) -> bytes:
    data = value.encode("utf-8") + b"\0"
    return data + b"\0" * ((4 - len(data) % 4) % 4)


def _osc_message(address: str, value: str | float | bool) -> bytes:
    if isinstance(value, bool):
        tags, payload = (",T" if value else ",F"), b""
    elif isinstance(value, float):
        import struct

        tags, payload = ",f", struct.pack(">f", value)
    else:
        tags, payload = ",s", _osc_string(str(value))
    return _osc_string(address) + _osc_string(tags) + payload


def _send(address: str, value: str | float | bool, values: dict[str, Any]) -> dict[str, Any]:
    host = str(values.get("vrchat_osc_host") or _cfg().get("vrchat_osc_host") or "127.0.0.1")
    port = int(values.get("vrchat_osc_port") or _cfg().get("vrchat_osc_port") or 9000)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(_osc_message(address, value), (host, port))
        return {"ok": True, "address": address, "host": host, "port": port}
    except OSError as exc:
        return {"ok": False, "error": str(exc), "address": address, "host": host, "port": port}


def vrchat_chatbox(values: dict[str, Any] | None = None, **_: Any) -> str:
    values = values or {}
    text = str(values.get("text") or "").strip()
    if not text:
        return _json({"ok": False, "error": "text is required"})
    if len(text) > 144:
        return _json({"ok": False, "error": "VRChat chatbox text must be <= 144 characters"})
    result = _send("/chatbox/input", text, values)
    if result.get("ok"):
        result["value"] = bool(values.get("send", True))
    return _json(result)


def vrchat_parameter(values: dict[str, Any] | None = None, **_: Any) -> str:
    values = values or {}
    name = str(values.get("name") or "").strip()
    if not name:
        return _json({"ok": False, "error": "name is required"})
    value = values.get("value")
    if not isinstance(value, (str, float, bool)):
        return _json({"ok": False, "error": "value must be string, number, or boolean"})
    return _json(_send(f"/avatar/parameters/{name}", value, values))


def vrchat_autonomy(values: dict[str, Any] | None = None, **_: Any) -> str:
    values = values or {}
    enabled = bool(values.get("enabled"))
    return _json(
        {
            "ok": True,
            "enabled": enabled,
            "mode": "explicit-local-osc",
            "note": (
                "AIRI is a Hermes-managed process worker for VRM/TTS. "
                "Hermes api_server remains the AI core (synced via airi_start). "
                "Avatar actions stay on this local OSC plane."
            ),
        }
    )


AIRI_SCHEMAS = {
    "airi_status": {
        "name": "airi_status",
        "description": "Show AIRI worker health, Hermes provider/TTS sync readiness, and OSC status.",
        "parameters": {"type": "object", "properties": {}},
    },
    "airi_sync": {
        "name": "airi_sync",
        "description": "Sync Hermes api_server into AIRI (provider file + optional live CDP reseed).",
        "parameters": {
            "type": "object",
            "properties": {
                "repo_root": {"type": "string"},
                "hermes_base_url": {"type": "string"},
                "hermes_model": {"type": "string"},
            },
        },
    },
    "airi_configure_hermes": {
        "name": "airi_configure_hermes",
        "description": "Write AIRI OpenAI Compatible provider settings pointing to Hermes Agent (alias of sync write step).",
        "parameters": {
            "type": "object",
            "properties": {
                "repo_root": {"type": "string"},
                "hermes_base_url": {"type": "string"},
                "hermes_model": {"type": "string"},
            },
        },
    },
    "airi_start": {
        "name": "airi_start",
        "description": "Start AIRI as a Hermes process worker: sync provider/TTS, launch tamagotchi, CDP seed+reload.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo_root": {"type": "string"},
                "hermes_base_url": {"type": "string"},
                "hermes_model": {"type": "string"},
                "cdp_port": {"type": "integer"},
            },
        },
    },
    "airi_stop": {
        "name": "airi_stop",
        "description": "Stop the Hermes-managed AIRI process worker.",
        "parameters": {"type": "object", "properties": {"pid": {"type": "integer"}}},
    },
    "airi_restart": {
        "name": "airi_restart",
        "description": "Restart the AIRI process worker and re-seed Hermes API_SERVER_KEY into AIRI credentials.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo_root": {"type": "string"},
                "hermes_base_url": {"type": "string"},
                "hermes_model": {"type": "string"},
                "cdp_port": {"type": "integer"},
            },
        },
    },
    "airi_vrchat_chatbox": {
        "name": "airi_vrchat_chatbox",
        "description": "Send an explicit local OSC message to the VRChat chatbox.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "send": {"type": "boolean"},
                "vrchat_osc_host": {"type": "string"},
                "vrchat_osc_port": {"type": "integer"},
            },
            "required": ["text"],
        },
    },
    "airi_vrchat_parameter": {
        "name": "airi_vrchat_parameter",
        "description": "Set an explicit local VRChat avatar OSC parameter.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "value": {},
                "vrchat_osc_host": {"type": "string"},
                "vrchat_osc_port": {"type": "integer"},
            },
            "required": ["name", "value"],
        },
    },
    "airi_vrchat_autonomy": {
        "name": "airi_vrchat_autonomy",
        "description": "Enable or disable the explicit AIRI/VRChat autonomy control-plane state.",
        "parameters": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
    },
}
