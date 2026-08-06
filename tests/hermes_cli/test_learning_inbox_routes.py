"""HTTP contract tests for the Learning Inbox routes."""

import pytest


def _client():
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    import hermes_state
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
    from hermes_constants import get_hermes_home

    hermes_state.DEFAULT_DB_PATH = get_hermes_home() / "state.db"
    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return client


@pytest.fixture(autouse=True)
def _isolated_home(_isolate_hermes_home):
    yield


def test_learning_inbox_route_lists_details_and_dismisses_pending_memory():
    import tools.write_approval as write_approval

    record = write_approval.stage_write(
        write_approval.MEMORY,
        {"action": "add", "target": "user", "content": "prefers concise reviews"},
        summary="prefers concise reviews",
        origin="background_review",
    )
    client = _client()

    response = client.get("/api/learning/inbox")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["id"] == f"memory:{record['id']}"

    detail = client.get(f"/api/learning/inbox/memory/{record['id']}")
    assert detail.status_code == 200
    assert "prefers concise reviews" in detail.json()["detail"]

    dismissed = client.post(f"/api/learning/inbox/memory/{record['id']}/dismiss")
    assert dismissed.status_code == 200
    assert dismissed.json()["ok"] is True
    assert client.get("/api/learning/inbox").json()["count"] == 0


def test_learning_inbox_route_rejects_invalid_action_and_reference():
    client = _client()

    invalid_action = client.post("/api/learning/inbox/memory/deadbeef/archive")
    assert invalid_action.status_code == 400

    invalid_reference = client.get("/api/learning/inbox/memory/../secrets")
    assert invalid_reference.status_code in {400, 404}


def test_learning_inbox_route_preserves_invalid_profile_status():
    client = _client()

    response = client.get("/api/learning/inbox?profile=missing-profile")

    assert response.status_code in {400, 404}
