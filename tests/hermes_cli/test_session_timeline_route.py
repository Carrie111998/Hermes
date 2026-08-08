"""Tests for GET /api/sessions/{session_id}/timeline (hermes_cli/web_routers/sessions.py).

Follows the TestClient pattern used throughout
tests/hermes_cli/test_web_server.py (e.g. TestWebServerEndpoints's
_setup_test_client fixture / test_get_action_status_endpoint_...): a real
Starlette TestClient against the actual FastAPI `app`, with the dashboard
session-auth header set, hitting the real route handler -- not a mock.
"""

import pytest

from tools import session_timeline as st


@pytest.fixture
def client(monkeypatch, _isolate_hermes_home):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    import hermes_state
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")

    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def test_timeline_route_empty_for_unknown_session(client):
    resp = client.get("/api/sessions/route-test-unknown/timeline")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"session_id": "route-test-unknown", "steps": [], "running": False}


def test_timeline_route_reflects_recorded_steps(client):
    sid = "route-test-basic"
    st.record_start(sid, "call-1", "read_file", {"path": "a.py"})
    st.record_end(sid, "call-1", status="succeeded", duration=0.5)

    resp = client.get(f"/api/sessions/{sid}/timeline")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == sid
    assert data["running"] is False
    assert len(data["steps"]) == 1
    step = data["steps"][0]
    assert step["tool"] == "read_file"
    assert step["status"] == "succeeded"
    assert step["duration"] == 0.5


def test_timeline_route_reports_running_true_for_in_flight_step(client):
    sid = "route-test-running"
    st.record_start(sid, "call-1", "terminal", {"command": "sleep 10"})

    resp = client.get(f"/api/sessions/{sid}/timeline")
    data = resp.json()
    assert data["running"] is True
    assert data["steps"][0]["status"] == "running"
    assert data["steps"][0]["duration"] is None


def test_timeline_route_respects_limit_param(client):
    sid = "route-test-limit"
    for i in range(10):
        st.record_start(sid, f"call-{i}", "terminal", {"command": f"echo {i}"})

    resp = client.get(f"/api/sessions/{sid}/timeline", params={"limit": 3})
    data = resp.json()
    assert len(data["steps"]) == 3
    # `limit` keeps the most recent steps.
    assert [s["step_n"] for s in data["steps"]] == [7, 8, 9]


def test_timeline_route_redacts_secret(client):
    """End-to-end: a secret in a tool arg must not survive the HTTP round trip."""
    sid = "route-test-redact"
    bearer = "sk-ant-api03-" + "Q" * 24
    st.record_start(sid, "call-1", "terminal", {"command": f'curl -H "Authorization: Bearer {bearer}"'})

    resp = client.get(f"/api/sessions/{sid}/timeline")
    body_text = resp.text
    assert bearer not in body_text
    assert "curl" in body_text
