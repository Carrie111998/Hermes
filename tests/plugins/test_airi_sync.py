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


def test_localstorage_seed_points_hermes_core():
    provider = core.provider_payload(
        {"hermes_base_url": "http://127.0.0.1:8642/v1", "hermes_model": "hermes-agent", "api_key": "test-key"}
    )
    seed = core._localstorage_seed(provider)
    creds = json.loads(seed["settings/credentials/providers"])
    assert creds["openai-compatible"]["baseUrl"].endswith("/")
    assert creds["openai-compatible"]["apiKey"] == "test-key"
    assert seed["settings/consciousness/active-provider"] == "openai-compatible"
    assert seed["settings/consciousness/active-model"] == "hermes-agent"
    assert json.loads(seed["settings/providers/added"])["openai-compatible"] is True


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
    assert payload["auth"]["api_key_configured"] is True
    assert "secret-should-not-persist" not in path.read_text(encoding="utf-8")


def test_sync_includes_hermes_probe_and_auth(monkeypatch, tmp_path: Path):
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
                "auth_ok": True,
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
    assert payload["hermes_probe"]["auth_ok"] is True
    assert payload["auth"]["ready"] is True
    assert payload["hermes_base_url"].endswith("/")


def test_start_already_running_reseeds(monkeypatch):
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
        lambda port, seed, wait_s=8.0: {"ok": True, "port": port, "keys": list(seed), "reloaded": True},
    )
    monkeypatch.setattr(
        core,
        "_auth_status",
        lambda values=None, probe=None: {
            "ready": True,
            "api_key_configured": True,
            "api_key_source": "API_SERVER_KEY",
            "probe_live": True,
            "probe_auth_ok": True,
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
        lambda port, seed, wait_s=60.0: {"ok": True, "port": port, "reloaded": True, "keys": list(seed)},
    )
    monkeypatch.setattr(core, "_write_state", lambda data: captured.update({"state": data}))
    monkeypatch.setattr(core, "_auth_status", lambda values=None, probe=None: {"ready": True, "api_key_configured": True})
    monkeypatch.setattr(core, "_worker_health", lambda state, values=None: {"healthy": True})
    monkeypatch.setattr(core.shutil, "which", lambda name: "pnpm.cmd" if "pnpm" in name else None)

    payload = json.loads(core.start({}))
    assert payload["ok"] is True
    assert payload["concurrent_with_hermes_desktop"] is True
    assert "--remote-debugging-port=9455" in captured["command"]
    assert captured["env"]["APP_USER_DATA_PATH"] == str(home / "userdata")
    assert captured["env"]["HERMES_AIRI_API_KEY"] == "k"
    # Must never target Desktop process kill paths.
    assert "Hermes.exe" not in " ".join(captured["command"])


def test_probe_distinguishes_live_vs_auth(monkeypatch):
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
    assert ok["auth_ok"] is True

    class FakeHTTPError(core.urllib.error.HTTPError):
        def __init__(self):
            pass

    def raise_401(*a, **k):
        err = core.urllib.error.HTTPError(
            "http://127.0.0.1:8642/v1/models", 401, "Unauthorized", hdrs=None, fp=None
        )
        raise err

    monkeypatch.setattr(core.urllib.request, "urlopen", raise_401)
    gated = core._probe_hermes("http://127.0.0.1:8642/v1/", api_key="bad")
    assert gated["live"] is True
    assert gated["auth_ok"] is False
    assert gated["auth_required"] is True
