from __future__ import annotations

import pytest

from hermes_cli.interactive_workspace import (
    InteractiveWorkspaceConnectedResult,
    InteractiveWorkspaceError,
    InteractiveWorkspaceResult,
)


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
    result = TestClient(app)
    result.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return result


def _payload() -> dict[str, str]:
    return {
        "project_id": "p_fixture",
        "task_id": "t_fixture",
        "workstream_id": "W1.1",
        "idempotency_key": "p_fixture:t_fixture:W1.1:v1",
        "write_scope": "dashboard frontend",
    }


def test_interactive_workspace_endpoint_returns_persisted_receipt(client, monkeypatch):
    import hermes_cli.interactive_workspace as workspace

    captured = []

    def fake_start(request):
        captured.append(request)
        return InteractiveWorkspaceResult(
            project_id=request.project_id,
            task_id=request.task_id,
            workstream_id=request.workstream_id,
            session_id="20260805_120000_ab12cd",
            repo_root="/repo",
            workspace_path="/repo/.worktrees/t_fixture",
            branch="feat/browser-session",
            base_ref="origin/main",
            preflight_status="passed",
            preflight_summary="PREFLIGHT_OK",
            reused=False,
        )

    monkeypatch.setattr(workspace, "start_interactive_task_workspace", fake_start)

    response = client.post("/api/workspaces/interactive/start", json=_payload())

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "project_id": "p_fixture",
        "task_id": "t_fixture",
        "workstream_id": "W1.1",
        "session_id": "20260805_120000_ab12cd",
        "repo_root": "/repo",
        "workspace_path": "/repo/.worktrees/t_fixture",
        "branch": "feat/browser-session",
        "base_ref": "origin/main",
        "preflight_status": "passed",
        "preflight_summary": "PREFLIGHT_OK",
        "reused": False,
    }
    assert len(captured) == 1
    assert captured[0].profile_name == ""


def test_interactive_workspace_endpoint_maps_native_conflict(client, monkeypatch):
    import hermes_cli.interactive_workspace as workspace

    def fail(_request):
        raise InteractiveWorkspaceError(
            "project_task_mismatch",
            "task does not belong to project",
        )

    monkeypatch.setattr(workspace, "start_interactive_task_workspace", fail)

    response = client.post("/api/workspaces/interactive/start", json=_payload())

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "project_task_mismatch"


def test_interactive_workspace_connected_endpoint_records_consumer_evidence(
    client, monkeypatch
):
    import hermes_cli.interactive_workspace as workspace

    captured = []

    def fake_connected(request, session_id):
        captured.append((request, session_id))
        return InteractiveWorkspaceConnectedResult(
            project_id=request.project_id,
            task_id=request.task_id,
            workstream_id=request.workstream_id,
            session_id=session_id,
        )

    monkeypatch.setattr(
        workspace,
        "mark_interactive_task_session_connected",
        fake_connected,
    )
    response = client.post(
        "/api/workspaces/interactive/connected",
        json={**_payload(), "session_id": "20260805_120000_ab12cd"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "project_id": "p_fixture",
        "task_id": "t_fixture",
        "workstream_id": "W1.1",
        "session_id": "20260805_120000_ab12cd",
        "reused": False,
    }
    assert captured[0][1] == "20260805_120000_ab12cd"


def test_interactive_workspace_endpoint_requires_dashboard_auth(monkeypatch, _isolate_hermes_home):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    from hermes_cli.web_server import app

    response = TestClient(app).post("/api/workspaces/interactive/start", json=_payload())

    assert response.status_code == 401
