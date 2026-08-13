"""Tests for the bundled agent-screen dashboard plugin.

Contract, not a change-detector:

* the FastAPI router imports and exposes /status /start /stop
* /status always reports platform + supported
* /start and /stop are 501 off macOS and never spawn/kill
* a healthy running instance makes /start a no-op (no second Popen)
* process control uses ``pgrep -x`` / ``pkill -x``, never ``-f``
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PLUGIN_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "agent-screen"
    / "dashboard"
    / "plugin_api.py"
)


@pytest.fixture
def plugin_api(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        f"agent_screen_plugin_api_{id(monkeypatch)}", PLUGIN_MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def client(plugin_api):
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/agent-screen")
    return TestClient(app)


def test_router_exports_expected_routes(plugin_api):
    paths = {route.path for route in plugin_api.router.routes}
    assert "/status" in paths
    assert "/start" in paths
    assert "/stop" in paths


def test_status_reports_platform_and_supported(client, plugin_api, monkeypatch):
    monkeypatch.setattr(plugin_api, "_supported", lambda: False)
    monkeypatch.setattr(plugin_api, "_app_running", lambda: False)
    monkeypatch.setattr(plugin_api, "_stream_ok", lambda: False)
    monkeypatch.setattr(plugin_api.sys, "platform", "linux")
    r = client.get("/api/plugins/agent-screen/status")
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is False
    assert body["running"] is False
    assert body["stream"] is False
    assert body["platform"] == "linux"
    assert "macOS" in body["error"]


def test_start_stop_are_501_off_macos(client, plugin_api, monkeypatch):
    spawned = []
    killed = []

    monkeypatch.setattr(plugin_api, "_supported", lambda: False)
    monkeypatch.setattr(plugin_api, "_spawn", lambda: spawned.append(True))
    monkeypatch.setattr(
        plugin_api.subprocess,
        "run",
        lambda *a, **k: killed.append(a) or type("R", (), {"returncode": 1, "stdout": ""})(),
    )

    start = client.post("/api/plugins/agent-screen/start")
    stop = client.post("/api/plugins/agent-screen/stop")
    assert start.status_code == 501
    assert stop.status_code == 501
    assert spawned == []
    assert killed == []


def test_start_is_noop_when_healthy(client, plugin_api, monkeypatch):
    spawned = []
    monkeypatch.setattr(plugin_api, "_supported", lambda: True)
    monkeypatch.setattr(plugin_api, "_app_running", lambda: True)
    monkeypatch.setattr(plugin_api, "_stream_ok", lambda: True)
    monkeypatch.setattr(plugin_api, "_spawn", lambda: spawned.append(True))
    monkeypatch.setattr(plugin_api, "_launcher_ok", lambda: True)

    r = client.post("/api/plugins/agent-screen/start")
    assert r.status_code == 200
    assert r.json()["running"] is True
    assert spawned == []


def test_start_errors_when_launcher_missing(client, plugin_api, monkeypatch):
    monkeypatch.setattr(plugin_api, "_supported", lambda: True)
    monkeypatch.setattr(plugin_api, "_app_running", lambda: False)
    monkeypatch.setattr(plugin_api, "_stream_ok", lambda: False)
    monkeypatch.setattr(plugin_api, "_launcher_ok", lambda: False)

    r = client.post("/api/plugins/agent-screen/start")
    assert r.status_code == 200
    assert "launcher missing" in r.json()["error"]


def test_stop_is_noop_when_not_running(client, plugin_api, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin_api, "_supported", lambda: True)
    monkeypatch.setattr(plugin_api, "_app_running", lambda: False)
    monkeypatch.setattr(plugin_api, "_stream_ok", lambda: False)
    monkeypatch.setattr(
        plugin_api.subprocess,
        "run",
        lambda *a, **k: calls.append(a[0]) or type("R", (), {"returncode": 1, "stdout": ""})(),
    )

    r = client.post("/api/plugins/agent-screen/stop")
    assert r.status_code == 200
    assert calls == []


def test_process_control_uses_exact_name_not_full_cmdline(plugin_api):
    src = PLUGIN_MODULE_PATH.read_text()
    assert '["pgrep", "-x", PROC_NAME]' in src
    assert '["pkill", "-x", PROC_NAME]' in src
    assert plugin_api.PROC_NAME == "agent-screen-app"
    # The old footgun must not come back.
    assert 'pgrep", "-f"' not in src
    assert 'pkill", "-f"' not in src
