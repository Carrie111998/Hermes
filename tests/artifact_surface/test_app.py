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


def test_index_shows_pin_badge_for_pinned(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "artifacts_dir", lambda: tmp_path)
    (tmp_path / "pinned.html").write_text("<h1>P</h1>", encoding="utf-8")
    (tmp_path / "pinned.json").write_text(json.dumps(
        {"id": "pinned", "title": "Pinned Card", "pinned": True}), encoding="utf-8")
    (tmp_path / "plain.html").write_text("<h1>X</h1>", encoding="utf-8")
    (tmp_path / "plain.json").write_text(json.dumps(
        {"id": "plain", "title": "Plain Card"}), encoding="utf-8")
    r = TestClient(app).get("/")
    assert r.status_code == 200
    # the pinned card carries a visible pin indicator; the plain one does not
    assert 'class="pin"' in r.text
    # pinned card renders before the plain card (pinned-first ordering)
    assert r.text.index("Pinned Card") < r.text.index("Plain Card")


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


def test_api_devflow_endpoint_present(client):
    r = client.get("/api/devflow")
    assert r.status_code == 200
    assert {
        "ledger_total", "active_leases", "read_errors", "ledger_available",
        "approval_queue", "approval_queue_page", "ddp_auth_readiness", "tick_health",
    } <= set(r.json())


def test_api_devflow_accepts_bounded_detail_query(client):
    r = client.get("/api/devflow", params={"state": "TRIAGED", "limit": 1})
    assert r.status_code == 200
    assert r.json()["request_page"]["limit"] == 1


from pathlib import Path
import artifact_surface


def test_reference_artifact_exists_and_wires_endpoints():
    p = Path(artifact_surface.__file__).parent / "examples" / "ops_overview.html"
    assert p.exists(), "reference artifact missing"
    text = p.read_text(encoding="utf-8")
    assert "/static/hermes-live.js" in text
    assert "/api/cron" in text
    assert "/api/events" in text
