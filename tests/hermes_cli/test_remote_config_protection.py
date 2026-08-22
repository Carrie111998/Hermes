"""
Tests for Issue #88913: Remote Config Protection, Revision Tracking, and Security Preservation.
"""

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.config import save_config, read_raw_config


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    """Test client bound to a temporary HERMES_HOME with session token headers configured."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_auth = getattr(web_server.app.state, "auth_required", None)

    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 9119
    web_server.app.state.auth_required = False

    # Initialize a valid remote config with custom provider and basic auth
    initial_config = {
        "model": {
            "default": "azure/gpt-5.6-sol",
            "provider": "bifrost",
        },
        "fallback_providers": [],
        "auxiliary": {
            "vision": {
                "provider": "bifrost-vllm",
                "model": "qwen36-27b",
                "base_url": "http://da-aihost01:4000/v1",
            }
        },
        "dashboard": {
            "basic_auth": {
                "username": "admin",
                "password_hash": "$scrypt$N=16384,r=8,p=1$abc...",
                "secret": "stable-signing-secret",
            }
        },
    }
    save_config(initial_config)

    headers = {"X-Hermes-Session-Token": web_server._SESSION_TOKEN}
    client = TestClient(web_server.app, base_url="http://127.0.0.1:9119", headers=headers)
    yield client

    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_auth


def test_get_config_includes_revision(web_client):
    """GET /api/config must include a deterministic _revision token."""
    response = web_client.get("/api/config")
    assert response.status_code == 200
    data = response.json()
    assert "_revision" in data
    assert len(data["_revision"]) == 16


def test_put_config_rejects_stale_revision(web_client):
    """PUT /api/config with a non-matching expected_revision must return HTTP 409 Conflict."""
    response = web_client.put(
        "/api/config",
        json={
            "config": {"agent": {"personality": "helpful"}},
            "expected_revision": "invalid_stale_revision_token",
        },
    )
    assert response.status_code == 409
    assert "Config has been modified" in response.json()["detail"]


def test_put_config_accepts_valid_revision(web_client):
    """PUT /api/config with matching expected_revision must succeed and update _revision."""
    get_res = web_client.get("/api/config")
    rev = get_res.json()["_revision"]

    response = web_client.put(
        "/api/config",
        json={
            "config": {"agent": {"personality": "tactical"}},
            "expected_revision": rev,
        },
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "_revision" in response.json()
    assert response.json()["_revision"] != rev


def test_put_config_preserves_dashboard_basic_auth_on_empty_overwrite(web_client):
    """Generic serialization sending empty basic_auth strings must NOT erase remote auth."""
    raw_before = read_raw_config()
    auth_before = raw_before["dashboard"]["basic_auth"]
    assert auth_before["username"] == "admin"
    assert auth_before["secret"] == "stable-signing-secret"

    # Simulate Desktop sending empty auth strings in a generic snapshot PUT
    response = web_client.put(
        "/api/config",
        json={
            "config": {
                "dashboard": {
                    "basic_auth": {
                        "username": "",
                        "password_hash": "",
                        "password": "",
                        "secret": "",
                    }
                }
            }
        },
    )
    assert response.status_code == 200

    raw_after = read_raw_config()
    auth_after = raw_after["dashboard"]["basic_auth"]
    assert auth_after["username"] == "admin"
    assert auth_after["secret"] == "stable-signing-secret"


def test_put_config_clears_dashboard_basic_auth_with_verified_revision(web_client):
    """A PUT carrying a matching expected_revision has proven the caller saw
    the current state, so an empty basic_auth in that request is a
    deliberate clear and must go through, not be silently restored."""
    get_res = web_client.get("/api/config")
    rev = get_res.json()["_revision"]

    response = web_client.put(
        "/api/config",
        json={
            "config": {
                "dashboard": {
                    "basic_auth": {
                        "username": "",
                        "password_hash": "",
                        "password": "",
                        "secret": "",
                    }
                }
            },
            "expected_revision": rev,
        },
    )
    assert response.status_code == 200

    raw_after = read_raw_config()
    auth_after = raw_after["dashboard"]["basic_auth"]
    assert auth_after["username"] == ""
    assert auth_after["secret"] == ""
