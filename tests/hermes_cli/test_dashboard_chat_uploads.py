from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.xdist_group("dashboard_auth_app_state")


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[TestClient, object]:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import web_server

    web_server.app.state.auth_required = False
    web_server.app.state.bound_host = "127.0.0.1"
    client = TestClient(web_server.app, base_url="http://127.0.0.1:9119")
    return client, web_server


def test_chat_upload_requires_dashboard_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    client, _web_server = _client(monkeypatch, tmp_path)

    response = client.post(
        "/api/chat/uploads",
        content=b"image",
        headers={"X-Hermes-Filename": "sample.png", "Content-Type": "image/png"},
    )

    assert response.status_code == 401


def test_chat_upload_stores_media_under_hermes_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    client, web_server = _client(monkeypatch, tmp_path)

    response = client.post(
        "/api/chat/uploads",
        content=b"image-bytes",
        headers={
            "X-Hermes-Session-Token": web_server._SESSION_TOKEN,
            "X-Hermes-Filename": "../bad name.png",
            "Content-Type": "image/png",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    stored = Path(payload["path"])
    assert stored.exists()
    assert stored.read_bytes() == b"image-bytes"
    assert stored.is_relative_to(tmp_path)
    assert payload["name"] == "bad name.png"
    assert payload["size"] == len(b"image-bytes")


def test_chat_upload_rejects_non_media(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    client, web_server = _client(monkeypatch, tmp_path)

    response = client.post(
        "/api/chat/uploads",
        content=b"text",
        headers={
            "X-Hermes-Session-Token": web_server._SESSION_TOKEN,
            "X-Hermes-Filename": "notes.txt",
            "Content-Type": "text/plain",
        },
    )

    assert response.status_code == 415
