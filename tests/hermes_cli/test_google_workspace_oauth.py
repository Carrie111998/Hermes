"""Desktop Google Workspace OAuth route contract."""

import json
import time

import pytest


@pytest.fixture(autouse=True)
def _clean_google_flows():
    from hermes_cli import web_server
    from hermes_cli.web_routers import google_workspace

    previous_auth_required = getattr(web_server.app.state, "auth_required", False)
    google_workspace._flows.clear()
    web_server.app.state.auth_required = False
    yield
    google_workspace._flows.clear()
    web_server.app.state.auth_required = previous_auth_required


class _FakeCredentials:
    granted_scopes = ["https://www.googleapis.com/auth/drive"]

    def to_json(self) -> str:
        return json.dumps(
            {
                "token": "access-token",
                "refresh_token": "refresh-token",
                "client_id": "client-id",
                "client_secret": "client-secret",
            }
        )


class _FakeOAuthFlow:
    credentials = _FakeCredentials()

    def fetch_token(self, *, code: str) -> None:
        self.code = code


def _client():
    from starlette.testclient import TestClient

    from hermes_cli import web_server
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN

    client = TestClient(web_server.app, base_url="http://127.0.0.1:8080")
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return client


def test_google_workspace_oauth_saves_token_after_loopback_callback(tmp_path, monkeypatch):
    from hermes_cli import web_server
    from hermes_cli.web_routers import google_workspace

    home = tmp_path / "hermes"
    home.mkdir()
    client_secret = home / "google_client_secret.json"
    client_secret.write_text('{"installed": {}}', encoding="utf-8")
    token = home / "google_token.json"

    monkeypatch.setattr(
        google_workspace,
        "_profile_paths",
        lambda _profile: (home, client_secret, token),
    )
    monkeypatch.setattr(
        google_workspace,
        "_load_google_flow",
        lambda *_args: (_FakeOAuthFlow(), "https://accounts.google.com/authorize", "state-1"),
    )
    monkeypatch.setattr(web_server, "_require_token", lambda _request: None)
    web_server.app.state.auth_required = False
    google_workspace._flows.clear()

    client = _client()
    started = client.post("/api/google-workspace/oauth/start")
    assert started.status_code == 200
    flow_id = started.json()["flow_id"]
    assert started.json()["authorization_url"] == "https://accounts.google.com/authorize"

    callback = client.get("/api/google-workspace/oauth/callback?code=code-1&state=state-1")
    assert callback.status_code == 200

    for _ in range(50):
        status = client.get(f"/api/google-workspace/oauth/flows/{flow_id}").json()
        if status["status"] == "approved":
            break
        time.sleep(0.01)
    else:
        raise AssertionError(f"OAuth did not complete: {status}")

    saved = json.loads(token.read_text(encoding="utf-8"))
    assert saved["refresh_token"] == "refresh-token"
    assert saved["scopes"] == ["https://www.googleapis.com/auth/drive"]
    assert "code-1" not in json.dumps(status)


def test_google_workspace_status_does_not_expose_token_contents(tmp_path, monkeypatch):
    from hermes_cli import web_server
    from hermes_cli.web_routers import google_workspace

    home = tmp_path / "hermes"
    home.mkdir()
    client_secret = home / "google_client_secret.json"
    token = home / "google_token.json"
    client_secret.write_text('{"installed": {}}', encoding="utf-8")
    token.write_text(
        json.dumps({"token": "access-token", "refresh_token": "refresh-token", "scopes": ["drive"]}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        google_workspace,
        "_profile_paths",
        lambda _profile: (home, client_secret, token),
    )
    monkeypatch.setattr(web_server, "_require_token", lambda _request: None)
    web_server.app.state.auth_required = False

    response = _client().get("/api/google-workspace/status")
    assert response.status_code == 200
    assert response.json() == {"configured": True, "connected": True, "scopes": ["drive"]}
    assert "refresh-token" not in response.text
    assert "access-token" not in response.text
