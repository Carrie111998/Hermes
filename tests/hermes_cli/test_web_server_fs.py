import base64
import json
from pathlib import Path
from types import SimpleNamespace
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


def test_fs_write_text_rejects_a_stale_renderer_baseline_after_an_agent_mutation(client, tmp_path, monkeypatch):
    from tools.excalidraw_document import mutate_document

    target = tmp_path / "scene.excalidraw"
    target.write_text(json.dumps({
        "type": "excalidraw",
        "version": 2,
        "elements": [{"id": "r1", "type": "rectangle", "x": 0, "y": 0, "width": 1, "height": 1}],
    }), encoding="utf-8")
    baseline_response = client.get("/api/fs/read-text", params={"path": str(target)})
    baseline = baseline_response.json()["fingerprint"]

    def read_file_bytes(_path):
        return SimpleNamespace(base64_content=base64.b64encode(target.read_bytes()).decode("ascii"), error=None)

    def write_file(_path, content):
        target.write_text(content, encoding="utf-8")
        return SimpleNamespace(error=None)

    monkeypatch.setattr(
        "tools.excalidraw_document._get_file_ops",
        lambda _task_id: SimpleNamespace(read_file_bytes=read_file_bytes, write_file=write_file),
    )
    mutate_document(
        str(target),
        operation="update",
        payload=[{"id": "r1", "x": 42}],
        expected_fingerprint=baseline,
        task_id="agent",
        profile="default",
        runtime="local",
    )
    agent_document = target.read_text(encoding="utf-8")

    response = client.post(
        "/api/fs/write-text",
        json={
            "path": str(target),
            "content": json.dumps({"type": "excalidraw", "version": 2, "elements": []}),
            "expected_fingerprint": baseline,
        },
    )

    assert response.status_code == 409
    assert target.read_text(encoding="utf-8") == agent_document


def test_fs_read_data_url_rejects_over_cap(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "_FS_DATA_URL_MAX_BYTES", 3)
    target = tmp_path / "image.png"
    target.write_bytes(b"1234")

    response = client.get("/api/fs/read-data-url", params={"path": str(target)})

    assert response.status_code == 413


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
