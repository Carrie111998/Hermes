"""Readiness must not activate platform plugins or optional platform SDKs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import time

from fastapi.testclient import TestClient


def test_status_does_not_enter_gateway_or_plugin_resolution(monkeypatch):
    import gateway.config as gateway_config
    from gateway.platform_registry import platform_registry
    from hermes_cli import plugins, web_server

    calls: list[str] = []

    def forbidden(name):
        def _record(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"/api/status called forbidden resolver: {name}")

        return _record

    monkeypatch.setattr(
        gateway_config,
        "load_gateway_config",
        forbidden("load_gateway_config"),
    )
    monkeypatch.setattr(
        plugins,
        "discover_plugins",
        forbidden("discover_plugins"),
    )
    monkeypatch.setattr(
        platform_registry,
        "plugin_entries",
        forbidden("plugin_entries"),
    )
    monkeypatch.setattr(
        platform_registry,
        "all_entries",
        forbidden("all_entries"),
    )
    monkeypatch.setattr(
        platform_registry,
        "_resolve_all",
        forbidden("_resolve_all"),
    )
    monkeypatch.setattr(
        platform_registry,
        "get",
        forbidden("adapter_lookup"),
    )

    response = TestClient(web_server.app).get("/api/status")

    assert response.status_code == 200
    assert calls == []


def test_status_is_independent_of_concurrent_plugin_discovery(monkeypatch):
    from hermes_cli import plugins, web_server

    entered = threading.Event()
    release = threading.Event()

    def slow_discovery():
        entered.set()
        release.wait(timeout=5)

    monkeypatch.setattr(plugins, "discover_plugins", slow_discovery)
    worker = threading.Thread(target=plugins.discover_plugins)
    worker.start()
    assert entered.wait(timeout=1)

    started = time.perf_counter()
    response = TestClient(web_server.app).get("/api/status")
    elapsed = time.perf_counter() - started
    release.set()
    worker.join(timeout=1)

    assert response.status_code == 200
    assert elapsed < 1
    assert not worker.is_alive()


def test_status_request_installs_import_guard_before_application_import(tmp_path):
    """A fresh request must not import adapters, SDKs, or entry-point payloads."""

    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "gateway_state.json").write_text(
        json.dumps(
            {
                "gateway_state": "running",
                "platforms": {
                    "telegram": {"state": "connected"},
                    "removed_plugin": {"state": "disconnected"},
                },
            }
        ),
        encoding="utf-8",
    )
    script = textwrap.dedent(
        """
        import importlib.metadata
        import json
        import sys

        forbidden_prefixes = (
            "gateway.platforms.",
            "plugins.platforms.",
            "hermes_plugins.platforms__",
        )
        forbidden_roots = {
            "aioimaplib",
            "aiosmtplib",
            "botbuilder",
            "discord",
            "dingtalk_stream",
            "google",
            "googleapiclient",
            "lark_oapi",
            "mattermostdriver",
            "msal",
            "nio",
            "slack_bolt",
            "slack_sdk",
            "telegram",
            "twilio",
        }

        class RejectPlatformImports:
            def find_spec(self, fullname, path=None, target=None):
                if (
                    fullname.startswith(forbidden_prefixes)
                    or fullname.split(".", 1)[0] in forbidden_roots
                ):
                    raise AssertionError("forbidden readiness import: " + fullname)
                return None

        sys.meta_path.insert(0, RejectPlatformImports())
        before = set(sys.modules)

        original_entry_point_load = importlib.metadata.EntryPoint.load
        def reject_entry_point_load(self):
            raise AssertionError("readiness loaded entry point: " + self.name)
        importlib.metadata.EntryPoint.load = reject_entry_point_load

        from fastapi.testclient import TestClient
        from hermes_cli import web_server

        response = TestClient(web_server.app).get("/api/status")
        imported = sorted(set(sys.modules) - before)
        forbidden = [
            name
            for name in imported
            if name.startswith(forbidden_prefixes)
            or name.split(".", 1)[0] in forbidden_roots
        ]
        print(
            "STATUS_IMPORT_RESULT="
            + json.dumps(
                {
                    "status_code": response.status_code,
                    "forbidden": forbidden,
                },
                sort_keys=True,
            )
        )
        importlib.metadata.EntryPoint.load = original_entry_point_load
        """
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not (
            key.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY"))
            or key
            in {
                "API_SERVER_KEY",
                "EMAIL_ADDRESS",
                "GOOGLE_APPLICATION_CREDENTIALS",
                "HASS_TOKEN",
                "IRC_CHANNEL",
                "IRC_SERVER",
                "MATRIX_HOMESERVER",
                "PHOTON_PROJECT_ID",
                "RAFT_PROFILE",
                "SIGNAL_ACCOUNT",
                "SIGNAL_HTTP_URL",
                "SIMPLEX_WS_URL",
            }
        )
    }
    env["HERMES_HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    marker = "STATUS_IMPORT_RESULT="
    payload_line = next(
        (line for line in result.stdout.splitlines() if line.startswith(marker)),
        None,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert payload_line is not None, result.stdout + result.stderr
    payload = json.loads(payload_line[len(marker) :])
    assert payload == {"status_code": 200, "forbidden": []}
