"""Tenant-backed Silverline demo profile qualification."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.agent_service import StubRunExecutor
from server.app import create_app
from server.config import Settings
from server.db import Database
from server.demo_seed import COMPANY_ID, seed_silverline


def test_demo_seed_is_idempotent_and_tenant_scoped(tmp_path):
    settings = Settings(
        database_path=tmp_path / "demo.db",
        upload_dir=tmp_path / "uploads",
        bootstrap_admin_email="admin@example.test",
        bootstrap_admin_password="correct-horse-battery",
        chat_enabled=False,
        credential_key="KJ9KmdJiLL6itiwlEGTvGQ4ptS4dnd1ZZPyRPTwmjs4=",
    )
    db = Database(settings.database_path)
    first = seed_silverline(db, email="client@silverline.test", password="silverline-test-123")
    second = seed_silverline(db, email="client@silverline.test", password="silverline-test-123")
    assert first["counts"] == second["counts"]
    assert db.one("SELECT COUNT(*) AS n FROM companies")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM leads WHERE company_id=?", (COMPANY_ID,))["n"] == 25

    client = TestClient(create_app(settings, db=db, run_executor=StubRunExecutor()))
    login = client.post("/api/v1/auth/login", json={
        "email": "client@silverline.test", "password": "silverline-test-123",
    })
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    me = client.get("/api/v1/auth/me", headers=headers).json()
    assert me["role"] == "customer"
    assert me["company_id"] == COMPANY_ID
    assert client.get("/api/v1/company/profile", headers=headers).json()["data"]["name"] == "Silverine"
    assert len(client.get("/api/v1/leads", headers=headers).json()) == 25
    assert len(client.get("/api/v1/contacts", headers=headers).json()) == 14
    messages = client.get("/api/v1/outreach/messages", headers=headers).json()
    assert len(messages) == 10
    assert all(item["content"]["to"].endswith("@example.test") for item in messages)
    pending = next(item for item in messages if item["status"] == "pending_approval")
    approved = client.post(f"/api/v1/outreach/messages/{pending['id']}/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    drafted = client.post(f"/api/v1/outreach/messages/{pending['id']}/create-draft", headers=headers)
    assert drafted.status_code == 200, drafted.text
    assert drafted.json()["status"] == "draft"


def test_demo_seed_does_not_touch_other_tenants(tmp_path):
    db = Database(tmp_path / "demo.db")
    stamp = 1000.0
    db.execute("INSERT INTO companies VALUES(?,?,?,?,?,?,?)",
               ("company_other", "Other Client", None, "active", "{}", stamp, stamp))
    db.execute("INSERT INTO onboarding(company_id,updated_at) VALUES(?,?)", ("company_other", stamp))
    seed_silverline(db, email="client@silverline.test", password="silverline-test-123")
    assert db.one("SELECT name FROM companies WHERE id='company_other'")["name"] == "Other Client"
    assert db.one("SELECT COUNT(*) AS n FROM leads WHERE company_id='company_other'")["n"] == 0
