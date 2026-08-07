"""Regression test for #81050 — web dashboard DELETE /api/mcp/servers/{name}
leaves .meta.json (and any .client.json) in mcp-tokens/, so a server removed
via the dashboard can be revived at the next gateway restart by leftover
files. The CLI path (`hermes mcp remove`) already routes through
MCPOAuthManager.remove() to delete all three per-server files; this test
pins the equivalent behavior on the dashboard DELETE path.
"""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _clear_state():
    """Reset dashboard in-memory state between tests."""
    from hermes_cli import web_server

    web_server.app.state.auth_required = False
    yield


def _client():
    from starlette.testclient import TestClient

    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return client


def test_dashboard_delete_removes_all_three_token_files(tmp_path, monkeypatch):
    """A server removed via DELETE /api/mcp/servers/{name} must have its
    .json, .client.json, and .meta.json files deleted so a gateway restart
    cannot revive the server (#81050).
    """
    import json
    import yaml

    # Isolate config I/O to tmp_path.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.config.get_hermes_home", lambda: tmp_path
    )
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    monkeypatch.setattr(
        "hermes_cli.config.get_config_path", lambda: config_path
    )
    monkeypatch.setattr(
        "hermes_cli.config.get_env_path", lambda: env_path
    )

    # Seed a config with the server under test.
    config_path.write_text(yaml.safe_dump({
        "mcp_servers": {
            "ghost-srv": {
                "url": "https://mcp.example.com/mcp",
                "auth": "oauth",
            },
        },
        "_config_version": 9,
    }))

    # Direct HermesTokenStorage at the tmp_path.
    monkeypatch.setattr("hermes_cli.mcp_config.get_hermes_home", lambda: tmp_path)

    # Lay down the three per-server files an OAuth server would leave behind.
    token_dir = tmp_path / "mcp-tokens"
    token_dir.mkdir()
    (token_dir / "ghost-srv.json").write_text(json.dumps({
        "access_token": "secret",
        "refresh_token": "secret",
    }))
    (token_dir / "ghost-srv.client.json").write_text(json.dumps({
        "client_id": "ghost",
        "client_secret": "secret",
    }))
    (token_dir / "ghost-srv.meta.json").write_text(json.dumps({
        "issuer": "https://idp.example.com",
    }))

    # Also seed a *different* server's token file: removing ghost-srv must not
    # touch it (regression guard for accidental over-broad cleanup).
    (token_dir / "keep-srv.json").write_text(json.dumps({
        "access_token": "secret",
    }))

    # Make the dashboard's get_hermes_home() resolve to tmp_path too.
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", lambda: tmp_path
    )

    client = _client()
    response = client.delete("/api/mcp/servers/ghost-srv")

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}

    # The three ghost-srv files must all be gone.
    assert not (token_dir / "ghost-srv.json").exists(), (
        "ghost-srv.json (OAuth tokens) survived DELETE — would leak credentials"
    )
    assert not (token_dir / "ghost-srv.client.json").exists(), (
        "ghost-srv.client.json (client registration) survived DELETE"
    )
    assert not (token_dir / "ghost-srv.meta.json").exists(), (
        "ghost-srv.meta.json (OAuth metadata) survived DELETE — "
        "this is the file that lets the gateway revive the server on restart (#81050)"
    )

    # The unrelated server's tokens must remain untouched.
    assert (token_dir / "keep-srv.json").exists(), (
        "delete leaked across to another server's tokens"
    )

    # Config must no longer reference the removed server.
    from hermes_cli.config import load_config
    config = load_config()
    assert "ghost-srv" not in config.get("mcp_servers", {})


def test_dashboard_delete_missing_server_returns_404(tmp_path, monkeypatch):
    """Deleting a server that's not in config.yaml returns 404 and does not
    raise or accidentally delete tokens for an unrelated server."""
    import yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.config.get_hermes_home", lambda: tmp_path
    )
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    monkeypatch.setattr(
        "hermes_cli.config.get_config_path", lambda: config_path
    )
    monkeypatch.setattr(
        "hermes_cli.config.get_env_path", lambda: env_path
    )
    config_path.write_text(yaml.safe_dump({
        "mcp_servers": {},
        "_config_version": 9,
    }))
    monkeypatch.setattr("hermes_cli.mcp_config.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)

    client = _client()
    response = client.delete("/api/mcp/servers/never-existed")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_dashboard_delete_with_no_tokens_still_succeeds(tmp_path, monkeypatch):
    """Removing a non-OAuth server (no token files at all) must succeed —
    the token cleanup is best-effort and the absence of files is not an error.
    """
    import yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.config.get_hermes_home", lambda: tmp_path
    )
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    monkeypatch.setattr(
        "hermes_cli.config.get_config_path", lambda: config_path
    )
    monkeypatch.setattr(
        "hermes_cli.config.get_env_path", lambda: env_path
    )
    config_path.write_text(yaml.safe_dump({
        "mcp_servers": {
            "plain-srv": {"url": "https://mcp.example.com/mcp"},
        },
        "_config_version": 9,
    }))
    monkeypatch.setattr("hermes_cli.mcp_config.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)

    client = _client()
    response = client.delete("/api/mcp/servers/plain-srv")

    assert response.status_code == 200
    assert response.json() == {"ok": True}