"""``/api/memory/providers/*`` must run under the requested management profile.

The GET/PUT ``/config`` siblings already take ``profile`` and wrap their body in
``_profile_scope``; ``POST .../setup`` did not, so it persisted provider values
and ran the provider's install steps against the dashboard's LAUNCH profile no
matter which profile the caller asked for.

Asserts the observable contract — what ``get_hermes_home()`` resolves to while
the handler body runs — rather than mocking ``_profile_scope`` itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "profiles" / "coder").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


@pytest.fixture
def client(profile_env, monkeypatch):
    import hermes_cli.web_server as ws

    monkeypatch.setattr(ws, "_require_valid_memory_provider_name", lambda name: None)
    monkeypatch.setattr(ws, "_memory_provider_manifest", lambda name: {"name": name})
    monkeypatch.setattr(ws, "_invalidate_plugins_hub_cache", lambda: None)
    c = TestClient(ws.app)
    c.headers[ws._SESSION_HEADER_NAME] = ws._SESSION_TOKEN
    return c


def _record_home(monkeypatch, seen: dict):
    """Capture the HERMES_HOME visible from inside the handler body."""
    import hermes_cli.web_server as ws
    from hermes_constants import get_hermes_home

    class _Provider:
        pass

    monkeypatch.setattr(ws, "_load_memory_provider", lambda name: _Provider())

    def _write(name, provider, values):
        seen["write"] = str(get_hermes_home())

    def _install(name):
        seen["install"] = str(get_hermes_home())
        return {"ok": True}

    monkeypatch.setattr(ws, "_write_memory_provider_config_values", _write)
    monkeypatch.setattr(ws, "_install_memory_provider_setup", _install)


class TestSetupMemoryProviderProfileScope:
    def test_setup_runs_under_the_requested_profile(
        self, client, profile_env, monkeypatch
    ):
        seen: dict = {}
        _record_home(monkeypatch, seen)

        resp = client.post(
            "/api/memory/providers/honcho/setup?profile=coder",
            json={"values": {"HONCHO_API_KEY": "k"}},
        )

        assert resp.status_code == 200
        expected = str(profile_env / "profiles" / "coder")
        assert seen["write"] == expected
        assert seen["install"] == expected

    def test_setup_without_profile_uses_the_launch_home(
        self, client, profile_env, monkeypatch
    ):
        seen: dict = {}
        _record_home(monkeypatch, seen)

        resp = client.post(
            "/api/memory/providers/honcho/setup",
            json={"values": {"HONCHO_API_KEY": "k"}},
        )

        assert resp.status_code == 200
        assert seen["write"] == str(profile_env)
        assert seen["install"] == str(profile_env)

    def test_config_read_already_scopes_and_still_does(
        self, client, profile_env, monkeypatch
    ):
        """Guards the sibling this fix is modelled on against regressing."""
        import hermes_cli.web_server as ws
        from hermes_constants import get_hermes_home

        seen: dict = {}

        def _payload(name, provider):
            seen["read"] = str(get_hermes_home())
            return {"name": name, "fields": []}

        monkeypatch.setattr(ws, "_load_memory_provider", lambda name: object())
        monkeypatch.setattr(ws, "_memory_provider_payload", _payload)

        resp = client.get("/api/memory/providers/honcho/config?profile=coder")

        assert resp.status_code == 200
        assert seen["read"] == str(profile_env / "profiles" / "coder")
