from __future__ import annotations

import time

from fastapi.testclient import TestClient

from server.agent_service import AgentRunService, BaseRunExecutor, StubRunExecutor
from server.app import create_app
from server.config import Settings
from server.db import Database, json_dump, new_id, now


TEST_KEY = "KJ9KmdJiLL6itiwlEGTvGQ4ptS4dnd1ZZPyRPTwmjs4="


def _client(tmp_path, executor=None):
    settings = Settings(
        database_path=tmp_path / "profile-api.db",
        upload_dir=tmp_path / "uploads",
        bootstrap_admin_email="admin@example.test",
        bootstrap_admin_password="correct-horse-battery",
        credential_key=TEST_KEY,
    )
    app = create_app(settings, run_executor=executor or StubRunExecutor())
    client = TestClient(app)
    login = client.post(
        "/api/v1/auth/login",
        json={"email": settings.bootstrap_admin_email, "password": settings.bootstrap_admin_password},
    ).json()
    admin = {"Authorization": f"Bearer {login['access_token']}"}
    first = client.post("/api/v1/admin/companies", headers=admin, json={"name": "Acme"}).json()
    second = client.post("/api/v1/admin/companies", headers=admin, json={"name": "Other"}).json()
    created = client.post(
        "/api/v1/admin/users",
        headers=admin,
        json={
            "email": "researcher@acme.test",
            "password": "another-secure-password",
            "role": "customer",
            "company_id": first["id"],
        },
    ).json()
    assert created["company_id"] == first["id"]
    customer_login = client.post(
        "/api/v1/auth/login",
        json={"email": "researcher@acme.test", "password": "another-secure-password"},
    ).json()
    headers = {"Authorization": f"Bearer {customer_login['access_token']}"}
    return app, client, headers, first["id"], second["id"]


def _profile(seller_countries=None):
    return {
        "identity": {"name": "Acme", "website": "https://acme.test"},
        "seller_countries": seller_countries or ["TR"],
        "products": [
            {
                "id": "prd_1",
                "name": "Vana",
                "english_name": "Valve",
                "hs_codes": ["8481"],
                "sector_ids": ["industrial-machinery"],
                "emphasis": 1,
            }
        ],
        "market_preferences": {"target_countries": ["DE"], "languages": ["de", "en"]},
        "hidden_label_ids": [],
        "playbook_versions": {"industrial-machinery": "1"},
    }


def test_confirmed_profile_uses_principal_scope_and_versions_changes(tmp_path):
    app, client, headers, company_id, other_company_id = _client(tmp_path)

    first = client.put("/api/v1/company/research-profile", headers=headers, json=_profile())
    second = client.put(
        "/api/v1/company/research-profile",
        headers=headers,
        json=_profile(["TR", "DE"]),
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] != second.json()["id"]
    assert client.get("/api/v1/company/research-profile", headers=headers).json()["id"] == second.json()["id"]
    assert app.state.db.one(
        "SELECT COUNT(*) AS n FROM company_profile_versions WHERE company_id=?",
        (company_id,),
    )["n"] == 2
    spoofed = client.put(
        "/api/v1/company/research-profile",
        headers={**headers, "X-Company-ID": other_company_id},
        json=_profile(),
    )
    assert spoofed.status_code == 403


def test_onboarding_research_is_bounded_and_remains_an_unconfirmed_suggestion(tmp_path):
    app, client, headers, company_id, _ = _client(tmp_path)

    response = client.post(
        "/api/v1/onboarding/research-profile",
        headers=headers,
        json={"official_website": "https://acme.test"},
    )

    assert response.status_code == 202
    run = response.json()
    assert run["run_type"] == "company_profile_research"
    assert run["payload"] == {
        "official_website": "https://acme.test",
        "max_pages": 8,
        "max_seconds": 120,
    }
    deadline = time.time() + 3
    while time.time() < deadline:
        run = client.get(f"/api/v1/agent-runs/{run['id']}", headers=headers).json()
        if run["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.01)
    assert run["status"] == "succeeded"
    assert run["output"]["products"] == []
    assert app.state.db.one(
        "SELECT COUNT(*) AS n FROM company_profile_versions WHERE company_id=?",
        (company_id,),
    )["n"] == 0


class _UncitedProfileExecutor(BaseRunExecutor):
    def execute(self, service, run):
        return {
            "identity": {"name": "Acme", "website": "https://acme.test"},
            "seller_countries": ["TR"],
            "products": [{"name": "Valve", "source_span_ids": []}],
            "market_preferences": {},
            "source_spans": [],
        }


def test_profile_research_rejects_products_without_exact_source_spans(tmp_path):
    db = Database(tmp_path / "invalid-profile-output.db")
    company_id, stamp = new_id("cmp"), now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (company_id, "Acme", "active", json_dump({}), stamp, stamp),
    )
    service = AgentRunService(db, _UncitedProfileExecutor())
    run = service.create(
        company_id,
        "company_profile_research",
        {"official_website": "https://acme.test", "max_pages": 8, "max_seconds": 120},
    )
    service.start(company_id, run["id"])
    deadline = time.time() + 3
    while time.time() < deadline:
        run = service.get(company_id, run["id"])
        if run["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.01)
    assert run["status"] == "failed"
    assert "source span" in run["error"].lower()
