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


def test_image_generation_request_routes_to_real_tool_layer(monkeypatch) -> None:
    checked: list[str] = []
    appended: list[tuple[str, str, str]] = []

    async def fake_request_hermes_json(
        user: demo.DemoUser,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        timeout_s: float = 120.0,
    ) -> tuple[int, dict[str, Any]]:
        checked.append(f"{method} {path} {user.workspace_id}")
        return 200, {"session": {"id": "s1"}}

    def fake_generate_image(prompt: str) -> dict[str, Any]:
        assert prompt == "帮我做一个猫的图片"
        return {
            "success": True,
            "image": "https://atlas-media.example/cat.jpg",
            "provider": "atlas",
        }

    def fake_append(session_id: str, role: str, content: str) -> None:
        appended.append((session_id, role, content))

    monkeypatch.setattr(demo, "_request_hermes_json", fake_request_hermes_json)
    monkeypatch.setattr(demo, "_generate_image_sync", fake_generate_image)
    monkeypatch.setattr(demo, "_append_session_message", fake_append)
    client = _client(monkeypatch)
    assert client.post("/login", data={"username": "alice", "password": "alice123"}).status_code == 303

    response = client.post("/app/api/sessions/s1/chat", json={"message": "帮我做一个猫的图片"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool"]["name"] == "image_generate"
    assert "https://atlas-media.example/cat.jpg" in payload["message"]["content"]
    assert checked == ["GET /api/sessions/s1 workspace-brand"]
    assert appended[0] == ("s1", "user", "帮我做一个猫的图片")
    assert appended[1][0:2] == ("s1", "assistant")
