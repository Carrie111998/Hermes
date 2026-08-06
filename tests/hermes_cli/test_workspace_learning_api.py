from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hermes_cli.web_routers import workspace_learning


def _metrics(successes=2):
    return {
        "cases": 2,
        "cost": 1,
        "latency_ms": 100,
        "safety_failures": 0,
        "successes": successes,
    }


def _app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def principal(request: Request, call_next):
        request.state.token_principal = SimpleNamespace(
            principal=request.headers.get("x-test-principal", "anonymous")
        )
        return await call_next(request)

    app.include_router(workspace_learning.router)
    return app


def test_dashboard_actor_identity_survives_session_token_rotation(monkeypatch, tmp_path):
    monkeypatch.setattr(workspace_learning, "get_hermes_home", lambda: tmp_path)

    def request(token: str) -> Request:
        return Request(
            {
                "type": "http",
                "headers": [(b"x-hermes-session-token", token.encode())],
                "query_string": b"",
            }
        )

    first = workspace_learning._principal(request("token-one"))
    second = workspace_learning._principal(request("token-two"))
    assert first == second
    assert first.startswith("dashboard-user:")


def test_learning_api_enforces_principals_and_closes_memory_loop(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    workspace_learning.reset_workspace_learning_state_for_tests()
    app = FastAPI()

    @app.middleware("http")
    async def principal(request: Request, call_next):
        request.state.token_principal = SimpleNamespace(
            principal=request.headers.get("x-test-principal", "anonymous")
        )
        return await call_next(request)

    app.include_router(workspace_learning.router)
    client = TestClient(app)

    signal_response = client.post(
        "/api/workspace/learning/signals",
        headers={"x-test-principal": "proposer-1"},
        json={
            "content": "User prefers grounded examples",
            "kind": "explicit_correction",
            "project_id": "project-1",
            "provenance": [{"source": "slack", "ref": "thread-1"}],
            "reusable": True,
        },
    )
    assert signal_response.status_code == 200
    signal_id = signal_response.json()["signal"]["signal_id"]

    candidate_response = client.post(
        "/api/workspace/learning/candidates",
        headers={"x-test-principal": "proposer-1"},
        json={
            "destination": "user_memory",
            "proposal": {
                "action": "add",
                "content": "User prefers grounded examples",
                "target": "user",
            },
            "risk": "low",
            "signal_ids": [signal_id],
        },
    )
    assert candidate_response.status_code == 200
    candidate_id = candidate_response.json()["candidate"]["candidate_id"]

    proposer_queue = client.get(
        "/api/workspace/learning/operator-queue?role=evaluator",
        headers={"x-test-principal": "proposer-1"},
    )
    assert proposer_queue.json()["tasks"] == []
    evaluator_queue = client.get(
        "/api/workspace/learning/operator-queue?role=evaluator",
        headers={"x-test-principal": "evaluator-1"},
    )
    assert [item["candidate_id"] for item in evaluator_queue.json()["tasks"]] == [candidate_id]

    denied_evaluator = client.post(
        f"/api/workspace/learning/candidates/{candidate_id}/evaluate",
        headers={"x-test-principal": "proposer-1"},
        json={
            "baseline": _metrics(1),
            "candidate": _metrics(2),
            "held_out_digest": "a" * 64,
            "policy_digest": "b" * 64,
        },
    )
    assert denied_evaluator.status_code == 409

    evaluated = client.post(
        f"/api/workspace/learning/candidates/{candidate_id}/evaluate",
        headers={"x-test-principal": "evaluator-1"},
        json={
            "baseline": _metrics(1),
            "candidate": _metrics(2),
            "held_out_digest": "a" * 64,
            "policy_digest": "b" * 64,
        },
    )
    assert evaluated.status_code == 200
    assert evaluated.json()["candidate"]["status"] == "approval_pending"

    approved = client.post(
        f"/api/workspace/learning/candidates/{candidate_id}/approve",
        headers={"x-test-principal": "user-1"},
    )
    assert approved.status_code == 200

    canary = client.post(
        f"/api/workspace/learning/candidates/{candidate_id}/canary",
        headers={"x-test-principal": "promoter-1"},
        json={"metrics": _metrics(2)},
    )
    assert canary.status_code == 200
    assert canary.json()["candidate"]["status"] == "canary_passed"

    applied = client.post(
        f"/api/workspace/learning/candidates/{candidate_id}/apply",
        headers={"x-test-principal": "promoter-1"},
    )
    assert applied.status_code == 200
    assert applied.json()["candidate"]["status"] == "applied"
    profile = tmp_path / "memories" / "USER.md"
    assert "User prefers grounded examples" in profile.read_text()

    rolled_back = client.post(
        f"/api/workspace/learning/candidates/{candidate_id}/rollback",
        headers={"x-test-principal": "user-1"},
        json={"reason": "Regression"},
    )
    assert rolled_back.status_code == 200
    assert rolled_back.json()["candidate"]["status"] == "quarantined"
    assert not profile.exists() or "User prefers grounded examples" not in profile.read_text()

    listed = client.get("/api/workspace/learning/candidates")
    assert listed.status_code == 200
    assert listed.json()["candidates"][0]["candidate_id"] == candidate_id
    workspace_learning.reset_workspace_learning_state_for_tests()


def test_learning_profile_selector_isolates_candidate_stores(monkeypatch, tmp_path):
    workspace_learning.reset_workspace_learning_state_for_tests()
    monkeypatch.setattr(
        workspace_learning,
        "_profile_home",
        lambda profile: tmp_path / str(profile or "default"),
    )
    client = TestClient(_app())
    proposer = {"X-Test-Principal": "proposer-service"}
    response = client.post(
        "/api/workspace/learning/signals?profile=alpha",
        headers=proposer,
        json={
            "content": "Use structured release notes",
            "kind": "explicit_correction",
            "project_id": None,
            "provenance": [{"source": "chat", "ref": "msg-profile"}],
            "reusable": True,
        },
    )
    assert response.status_code == 200
    signal_id = response.json()["signal"]["signal_id"]
    created = client.post(
        "/api/workspace/learning/candidates?profile=alpha",
        headers=proposer,
        json={
            "destination": "memory",
            "proposal": {
                "action": "add",
                "content": "Release notes use a structured format",
                "target": "memory",
            },
            "risk": "low",
            "signal_ids": [signal_id],
        },
    )
    assert created.status_code == 200
    alpha = client.get("/api/workspace/learning/candidates?profile=alpha", headers=proposer)
    beta = client.get("/api/workspace/learning/candidates?profile=beta", headers=proposer)
    assert len(alpha.json()["candidates"]) == 1
    assert beta.json()["candidates"] == []
    workspace_learning.reset_workspace_learning_state_for_tests()
