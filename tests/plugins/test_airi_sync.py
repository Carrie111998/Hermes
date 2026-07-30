"""Tests for AIRI ↔ Hermes process-worker sync and concurrent Electron wiring."""
from __future__ import annotations

import json
from pathlib import Path

from plugins.airi import core


def test_base_url_keeps_trailing_slash():
    assert core._base_url({"hermes_base_url": "http://127.0.0.1:9119/v1"}) == "http://127.0.0.1:9119/v1/"
    assert core._base_url({"hermes_base_url": "http://127.0.0.1:9119/v1/"}) == "http://127.0.0.1:9119/v1/"


def test_default_cdp_port_avoids_desktop_perf():
    # Hermes Desktop perf docs use :9222 / :9333 — AIRI must not collide.
    assert core.DEFAULT_CDP_PORT == 9455
    assert core.CONCURRENT_WITH_DESKTOP is True


def test_api_key_info_uses_api_server_key_only(monkeypatch):
    """Plugin must not invent HERMES_AIRI_API_KEY as a parallel secret path."""
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("HERMES_AIRI_API_KEY", raising=False)
    monkeypatch.setenv("HERMES_AIRI_API_KEY", "should-be-ignored")
    monkeypatch.setattr(core, "_load_hermes_dotenv", lambda: None)

    # Without API_SERVER_KEY, even HERMES_AIRI_API_KEY must not win.
    info = core._api_key_info({})
    assert info["configured"] is False
    assert info["source"] == "placeholder"

    monkeypatch.setenv("API_SERVER_KEY", "gateway-key")
    info2 = core._api_key_info({})
    assert info2["configured"] is True
    assert info2["api_key"] == "gateway-key"
    assert info2["source"] == "API_SERVER_KEY"


def test_localstorage_seed_points_hermes_core():
    provider = core.provider_payload(
        {"hermes_base_url": "http://127.0.0.1:8642/v1", "hermes_model": "hermes-agent", "api_key": "test-key"}
    )
    seed = core._localstorage_seed(provider)
    creds = seed["credentials_patch"]["openai-compatible"]
    assert creds["baseUrl"].endswith("/")
    assert creds["apiKey"] == "test-key"
    assert seed["flat"]["settings/consciousness/active-provider"] == "openai-compatible"
    assert seed["flat"]["settings/consciousness/active-model"] == "hermes-agent"
    assert seed["added_patch"]["openai-compatible"] is True


def test_localstorage_seed_includes_irodori_tts(monkeypatch):
    monkeypatch.setattr(
        core,
        "tts_payload",
        lambda values=None: {
            "ok": True,
            "source": "hermes_tts.irodori",
            "hermes_provider": "irodori-tts",
            "airi_provider": "openai-compatible-audio-speech",
            "config": {
                "apiKey": "local",
                "baseUrl": "http://127.0.0.1:8088/v1/",
                "model": "irodori-tts",
                "voice": "hakua",
            },
        },
    )
    # Bypass tts_payload monkeypatch path used inside seed builder by passing tts directly.
    provider = core.provider_payload(
        {"hermes_base_url": "http://127.0.0.1:8642/v1", "hermes_model": "hermes-agent", "api_key": "k"}
    )
    tts = {
        "ok": True,
        "airi_provider": "openai-compatible-audio-speech",
        "config": {
            "apiKey": "local",
            "baseUrl": "http://127.0.0.1:8088/v1/",
            "model": "irodori-tts",
            "voice": "hakua",
        },
    }
    seed = core._localstorage_seed(provider, tts)
    assert seed["flat"]["settings/speech/active-provider"] == "openai-compatible-audio-speech"
    assert seed["flat"]["settings/speech/active-model"] == "irodori-tts"
    assert seed["flat"]["settings/speech/voice"] == "hakua"
    speech = seed["credentials_patch"]["openai-compatible-audio-speech"]
    assert speech["baseUrl"] == "http://127.0.0.1:8088/v1/"
    assert speech["model"] == "irodori-tts"
    assert speech["voice"] == "hakua"


def test_tts_payload_maps_irodori(monkeypatch):
    monkeypatch.setattr(core, "_hermes_tts_section", lambda: {"provider": "irodori-tts", "irodori": {}})

    class FakeSettings:
        base_url = "http://127.0.0.1:8088"
        model = "irodori-tts"
        voice = "hakua"

    import sys
    import types

    fake = types.ModuleType("plugins.irodori_tts.core")
    fake.settings = lambda tts=None: FakeSettings()
    monkeypatch.setitem(sys.modules, "plugins.irodori_tts.core", fake)
    # Also support `from plugins.irodori_tts.core import settings`
    pkg = types.ModuleType("plugins.irodori_tts")
    pkg.core = fake
    monkeypatch.setitem(sys.modules, "plugins.irodori_tts", pkg)

    payload = core.tts_payload({})
    assert payload["ok"] is True
    assert payload["airi_provider"] == "openai-compatible-audio-speech"
    assert payload["config"]["baseUrl"].endswith("/v1/")
    assert payload["config"]["model"] == "irodori-tts"
    assert payload["config"]["voice"] == "hakua"


def test_readback_matches_requires_consciousness_and_key():
    seed = {
        "flat": {
            "settings/consciousness/active-provider": "openai-compatible",
            "settings/consciousness/active-model": "hermes-agent",
            "settings/speech/active-provider": "openai-compatible-audio-speech",
        }
    }
    assert core._readback_matches(
        seed,
        {
            "consciousnessProvider": "openai-compatible",
            "consciousnessModel": "hermes-agent",
            "openaiCompatibleHasKey": True,
            "speechProvider": "openai-compatible-audio-speech",
        },
    )
    assert not core._readback_matches(
        seed,
        {
            "consciousnessProvider": "",
            "consciousnessModel": "",
            "openaiCompatibleHasKey": True,
            "speechProvider": "speech-noop",
        },
    )

def test_configure_hermes_writes_provider_file_without_secret(tmp_path: Path, monkeypatch):
    repo = tmp_path / "airi"
    repo.mkdir()
    (repo / "package.json").write_text("{}", encoding="utf-8")
    home = tmp_path / "hermes-airi"
    monkeypatch.setattr(core, "HERMES_AIRI_HOME", home)
    monkeypatch.setattr(core, "_repo", lambda values=None: repo)
    monkeypatch.setattr(core, "_api_key_info", lambda values=None: {
        "api_key": "secret-should-not-persist",
        "source": "API_SERVER_KEY",
        "configured": True,
        "is_placeholder": False,
    })

    payload = json.loads(
        core.configure_hermes({"hermes_base_url": "http://127.0.0.1:8642/v1", "api_key": "secret-should-not-persist"})
    )
    assert payload["ok"] is True
    path = Path(payload["provider_file"])
    assert path.is_file()
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["definitionId"] == "openai-compatible"
    assert written["config"]["baseUrl"].endswith("/")
    assert "apiKey" not in written["config"]
    assert written["config"]["apiKeyEnv"] == "API_SERVER_KEY"
    assert payload["provider"]["api_key_configured"] is True
    assert "auth" not in payload
    assert "secret-should-not-persist" not in path.read_text(encoding="utf-8")


def test_sync_includes_hermes_probe_and_provider(monkeypatch, tmp_path: Path):
    repo = tmp_path / "airi"
    repo.mkdir()
    (repo / "package.json").write_text("{}", encoding="utf-8")
    home = tmp_path / "hermes-airi"
    monkeypatch.setattr(core, "HERMES_AIRI_HOME", home)
    monkeypatch.setattr(core, "_repo", lambda values=None: repo)
    monkeypatch.setattr(
        core,
        "_resolve_live_core",
        lambda values=None: {
            "ok": True,
            "base_url": "http://127.0.0.1:8642/v1/",
            "probe": {
                "ok": True,
                "live": True,
                "status": 200,
                "url": "http://127.0.0.1:8642/v1/models",
            },
            "probes": [],
        },
    )
    monkeypatch.setattr(
        core,
        "_api_key_info",
        lambda values=None: {
            "api_key": "k",
            "source": "API_SERVER_KEY",
            "configured": True,
            "is_placeholder": False,
        },
    )
    monkeypatch.setattr(core, "_read_state", lambda: {})
    monkeypatch.setattr(core, "_pid_alive", lambda pid: False)

    payload = json.loads(core.sync({"hermes_base_url": "http://127.0.0.1:8642/v1"}))
    assert payload["ok"] is True
    assert payload["synced"] is True
    assert payload["worker_role"] == "airi-desktop-shell"
    assert payload["hermes_probe"]["ok"] is True
    assert payload["provider"]["ready"] is True
    assert payload["provider"]["api_key_source"] == "API_SERVER_KEY"
    assert "auth" not in payload
    assert payload["hermes_base_url"].endswith("/")


def test_start_already_running_reseeds(monkeypatch, tmp_path: Path):
    repo = tmp_path / "airi"
    repo.mkdir()
    (repo / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(core, "_repo", lambda values=None: repo)
    monkeypatch.setattr(core, "sync", lambda values=None, **_: json.dumps({"ok": True, "synced": True}))
    monkeypatch.setattr(core, "_ensure_rgba_icon", lambda repo: {"repaired": False})
    # Stale seed in state must not clobber a fresh CDP seed (dict merge order bug).
    monkeypatch.setattr(
        core,
        "_read_state",
        lambda: {
            "pid": 4242,
            "cdp_port": 9455,
            "cdp_seed": {"ok": False, "error": "stale"},
        },
    )
    monkeypatch.setattr(core, "_pid_alive", lambda pid: True)
    written: dict = {}
    monkeypatch.setattr(core, "_write_state", lambda data: written.update(data))
    monkeypatch.setattr(
        core,
        "_cdp_seed_localstorage",
        lambda port, seed, wait_s=8.0: {
            "ok": True,
            "port": port,
            "keys": list((seed.get("flat") or seed).keys()),
            "reloaded": True,
            "readback": {
                "consciousnessProvider": "openai-compatible",
                "consciousnessModel": "hermes-agent",
                "openaiCompatibleHasKey": True,
                "speechProvider": "openai-compatible-audio-speech",
            },
        },
    )
    monkeypatch.setattr(
        core,
        "_provider_runtime_status",
        lambda values=None, probe=None: {
            "ready": True,
            "api_key_configured": True,
            "api_key_source": "API_SERVER_KEY",
            "core_live": True,
            "core_ok": True,
        },
    )
    monkeypatch.setattr(
        core,
        "_worker_health",
        lambda state, values=None: {"healthy": True, "running": True, "concurrent_with_hermes_desktop": True},
    )
    payload = json.loads(core.start({}))
    assert payload["ok"] is True
    assert payload["already_running"] is True
    assert payload["cdp_seed"]["ok"] is True
    assert payload["cdp_seed"].get("reloaded") is True
    assert written["cdp_seed"]["ok"] is True
    assert "auth" not in written
    assert payload["worker_role"] == "airi-desktop-shell"


def test_start_sets_isolated_userdata_and_cdp(monkeypatch, tmp_path: Path):
    repo = tmp_path / "airi"
    repo.mkdir()
    (repo / "package.json").write_text("{}", encoding="utf-8")
    home = tmp_path / "hermes-airi"
    monkeypatch.setattr(core, "HERMES_AIRI_HOME", home)
    monkeypatch.setattr(core, "_repo", lambda values=None: repo)
    monkeypatch.setattr(core, "sync", lambda values=None, **_: json.dumps({"ok": True, "synced": True}))
    monkeypatch.setattr(core, "_ensure_rgba_icon", lambda r: {"repaired": False, "mode_after": "RGBA"})
    monkeypatch.setattr(core, "_read_state", lambda: {})
    monkeypatch.setattr(core, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(core, "_api_key_info", lambda values=None: {
        "api_key": "k",
        "source": "API_SERVER_KEY",
        "configured": True,
        "is_placeholder": False,
    })
    captured: dict = {}

    class FakeProc:
        pid = 7777

    def fake_popen(command, cwd=None, env=None, **kwargs):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["env"] = dict(env or {})
        return FakeProc()

    monkeypatch.setattr(core.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        core,
        "_cdp_seed_localstorage",
        lambda port, seed, wait_s=60.0: {
            "ok": True,
            "port": port,
            "reloaded": True,
            "keys": list((seed.get("flat") or seed).keys()),
            "readback": {
                "consciousnessProvider": "openai-compatible",
                "openaiCompatibleHasKey": True,
            },
        },
    )
    monkeypatch.setattr(core, "_write_state", lambda data: captured.update({"state": data}))
    monkeypatch.setattr(
        core,
        "_provider_runtime_status",
        lambda values=None, probe=None: {"ready": True, "api_key_configured": True},
    )
    monkeypatch.setattr(core, "_worker_health", lambda state, values=None: {"healthy": True})
    monkeypatch.setattr(core.shutil, "which", lambda name: "pnpm.cmd" if "pnpm" in name else None)

    payload = json.loads(core.start({}))
    assert payload["ok"] is True
    assert payload["concurrent_with_hermes_desktop"] is True
    assert "--remote-debugging-port=9455" in captured["command"]
    assert captured["env"]["APP_USER_DATA_PATH"] == str(home / "userdata")
    assert captured["env"].get("API_SERVER_KEY") == "k"
    assert "HERMES_AIRI_API_KEY" not in captured["env"]
    # Must never target Desktop process kill paths.
    assert "Hermes.exe" not in " ".join(captured["command"])


def test_probe_distinguishes_live_vs_accepted(monkeypatch):
    class FakeResp:
        status = 200

        def read(self, _n=4096):
            return b'{"data":[]}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(core.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    ok = core._probe_hermes("http://127.0.0.1:8642/v1/", api_key="real")
    assert ok["live"] is True
    assert ok["ok"] is True
    assert "auth_ok" not in ok

    def raise_401(*a, **k):
        err = core.urllib.error.HTTPError(
            "http://127.0.0.1:8642/v1/models", 401, "Unauthorized", hdrs=None, fp=None
        )
        raise err

    monkeypatch.setattr(core.urllib.request, "urlopen", raise_401)
    gated = core._probe_hermes("http://127.0.0.1:8642/v1/", api_key="bad")
    assert gated["live"] is True
    assert gated["ok"] is False
    assert "auth_ok" not in gated
    assert "auth_required" not in gated
