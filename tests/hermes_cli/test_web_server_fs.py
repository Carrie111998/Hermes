import base64
from pathlib import Path

import pytest

from hermes_cli import web_server

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    previous_auth_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        if previous_auth_required is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous_auth_required


def test_fs_list_sorts_and_hides_noise(client, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "b.txt").write_text("b")
    (root / "a_dir").mkdir()
    (root / "a.txt").write_text("a")
    (root / "node_modules").mkdir()
    (root / ".git").mkdir()

    response = client.get("/api/fs/list", params={"path": str(root)})

    assert response.status_code == 200
    entries = response.json()["entries"]
    assert [entry["name"] for entry in entries] == ["a_dir", "a.txt", "b.txt"]
    assert entries[0] == {"name": "a_dir", "path": str(root / "a_dir"), "isDirectory": True}
    assert all(entry["name"] not in {".git", "node_modules"} for entry in entries)


def test_fs_read_data_url_rejects_over_cap(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "_FS_DATA_URL_MAX_BYTES", 3)
    target = tmp_path / "image.png"
    target.write_bytes(b"1234")

    response = client.get("/api/fs/read-data-url", params={"path": str(target)})

    assert response.status_code == 413


def test_fs_download_streams_file_without_data_url_cap(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "_FS_DATA_URL_MAX_BYTES", 3)
    target = tmp_path / "report with spaces.pdf"
    target.write_bytes(b"123456")

    response = client.get("/api/fs/download", params={"path": str(target)})

    assert response.status_code == 200
    assert response.content == b"123456"
    assert response.headers["content-type"].startswith("application/pdf")
    assert "report%20with%20spaces.pdf" in response.headers["content-disposition"]


def test_fs_download_rejects_sensitive_files(client, tmp_path):
    target = tmp_path / ".env"
    target.write_text("SECRET=1")

    response = client.get("/api/fs/download", params={"path": str(target)})

    assert response.status_code == 403


@pytest.mark.parametrize("route", ["read-text", "read-data-url"])
@pytest.mark.parametrize(
    "sensitive_name", [".env", "auth.json", "config.yaml"]
)
def test_fs_read_previews_reject_sensitive_files(
    client, tmp_path, route, sensitive_name
):
    """The two preview endpoints must enforce the same #57505 guard as
    download — without it an authenticated session reads every credential
    store as text or a whole-file base64 data URL (#95303)."""
    target = tmp_path / sensitive_name
    target.write_text("SECRET=1")

    response = client.get(f"/api/fs/{route}", params={"path": str(target)})

    assert response.status_code == 403


def test_fs_read_previews_reject_credential_directory(client, tmp_path):
    """Paths inside a credential directory (mcp-tokens) are sensitive by
    component, not basename — both preview routes must 403."""
    cred_dir = tmp_path / "mcp-tokens"
    cred_dir.mkdir()
    target = cred_dir / "server.json"
    target.write_text("{}")

    for route in ("read-text", "read-data-url"):
        response = client.get(f"/api/fs/{route}", params={"path": str(target)})
        assert response.status_code == 403, route


def test_fs_read_previews_serve_ordinary_files(client, tmp_path):
    """The guard must not block the normal spot-editor flow."""
    target = tmp_path / "notes.md"
    target.write_text("hello")

    text_response = client.get("/api/fs/read-text", params={"path": str(target)})
    url_response = client.get("/api/fs/read-data-url", params={"path": str(target)})

    assert text_response.status_code == 200
    assert text_response.json()["text"] == "hello"
    assert url_response.status_code == 200
    assert "aGVsbG8=" in url_response.json()["dataUrl"]


def test_fs_endpoints_require_auth(tmp_path):
    client = TestClient(web_server.app)
    target = tmp_path / "secret.txt"
    target.write_text("secret")

    list_response = client.get("/api/fs/list", params={"path": str(tmp_path)})
    read_response = client.get("/api/fs/read-text", params={"path": str(target)})
    default_response = client.get("/api/fs/default-cwd")

    assert list_response.status_code == 401
    assert read_response.status_code == 401
    assert default_response.status_code == 401
