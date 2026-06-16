from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import services.multiuser_web_demo as demo


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(demo, "session_store", demo.LoginSessionStore())
    return TestClient(demo.app, follow_redirects=False)


def test_login_sets_http_only_cookie(monkeypatch) -> None:
    client = _client(monkeypatch)

    response = client.post("/login", data={"username": "alice", "password": "alice123"})

    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert demo.COOKIE_NAME in cookie
    assert "HttpOnly" in cookie


def test_api_requires_login(monkeypatch) -> None:
    client = _client(monkeypatch)

    response = client.get("/app/api/me")

    assert response.status_code == 401
    assert response.json() == {"error": "not_authenticated"}


def test_logged_in_user_is_projected_to_principal_headers(monkeypatch) -> None:
    captured: list[dict[str, str]] = []

    async def fake_call_hermes(
        user: demo.DemoUser,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        timeout_s: float = 120.0,
    ) -> JSONResponse:
        captured.append(demo._principal_headers(user))
        return JSONResponse({"data": []})

    monkeypatch.setattr(demo, "_call_hermes", fake_call_hermes)
    client = _client(monkeypatch)
    login = client.post("/login", data={"username": "bob", "password": "bob123"})
    assert login.status_code == 303

    response = client.get("/app/api/sessions")

    assert response.status_code == 200
    assert captured == [
        {
            "Authorization": "Bearer dev-multiuser-test-key",
            "X-Hermes-Tenant-Id": "tenant-demo",
            "X-Hermes-Workspace-Id": "workspace-video",
            "X-Hermes-Project-Id": "project-ultra",
            "X-Hermes-User-Id": "user-bob",
            "X-Hermes-Roles": "creator",
        }
    ]


def test_logout_clears_session(monkeypatch) -> None:
    client = _client(monkeypatch)
    assert client.post("/login", data={"username": "alice", "password": "alice123"}).status_code == 303

    logout = client.post("/logout")
    me = client.get("/app/api/me")

    assert logout.status_code == 303
    assert me.status_code == 401
