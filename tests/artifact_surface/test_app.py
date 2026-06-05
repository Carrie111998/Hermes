import json
import pytest
from fastapi.testclient import TestClient
from artifact_surface import store
from artifact_surface.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "artifacts_dir", lambda: tmp_path)
    (tmp_path / "ops.html").write_text("<h1>Ops</h1>", encoding="utf-8")
    (tmp_path / "ops.json").write_text(json.dumps(
        {"id": "ops", "title": "Ops Overview", "source": "scout"}), encoding="utf-8")
    return TestClient(app)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["service"] == "hermes-canvas"


def test_index_lists_artifacts(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Ops Overview" in r.text


def test_raw_artifact_served_with_csp(client):
    r = client.get("/raw/ops")
    assert r.status_code == 200
    assert "<h1>Ops</h1>" in r.text
    assert "connect-src 'self'" in r.headers.get("content-security-policy", "")


def test_raw_missing_artifact_404(client):
    assert client.get("/raw/nope").status_code == 404


def test_raw_traversal_404(client):
    assert client.get("/raw/..%2f..%2fsecrets").status_code in (404, 400)


def test_view_wrapper_has_sandboxed_iframe(client):
    r = client.get("/a/ops")
    assert r.status_code == 200
    assert "sandbox=" in r.text
    assert "/raw/ops" in r.text


def test_api_cron_endpoint_present(client):
    r = client.get("/api/cron")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


from pathlib import Path
import artifact_surface


def test_reference_artifact_exists_and_wires_endpoints():
    p = Path(artifact_surface.__file__).parent / "examples" / "ops_overview.html"
    assert p.exists(), "reference artifact missing"
    text = p.read_text(encoding="utf-8")
    assert "/static/hermes-live.js" in text
    assert "/api/cron" in text
    assert "/api/events" in text
