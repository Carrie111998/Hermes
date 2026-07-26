"""Product API qualification checks; also runnable without pytest."""
from __future__ import annotations

import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from server.agent_service import StubRunExecutor
from server.app import create_app
from server.config import Settings
from server.db import json_dump, new_id, now

# Fixed test-only Fernet key. Real deployments read INTERFAZE_CREDENTIAL_KEY;
# tests need one because outbound email must carry a signed opt-out link.
TEST_CREDENTIAL_KEY = "KJ9KmdJiLL6itiwlEGTvGQ4ptS4dnd1ZZPyRPTwmjs4="


def make_client():
    root = Path(tempfile.mkdtemp(prefix="interfaze-api-test-"))
    settings = Settings(
        database_path=root / "test.db",
        upload_dir=root / "uploads",
        bootstrap_admin_email="admin@example.test",
        bootstrap_admin_password="correct-horse-battery",
        credential_key=TEST_CREDENTIAL_KEY,
    )
    app = create_app(settings, run_executor=StubRunExecutor())
    client = TestClient(app)
    login = client.post("/api/v1/auth/login", json={
        "email": settings.bootstrap_admin_email,
        "password": settings.bootstrap_admin_password,
    })
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    company = client.post("/api/v1/admin/companies", headers=headers, json={"name": "Acme"})
    assert company.status_code == 201, company.text
    headers["X-Company-ID"] = company.json()["id"]
    return app, client, headers, company.json()["id"]


def wait_for_run(client, headers, run_id, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/v1/agent-runs/{run_id}", headers=headers).json()
        if run["status"] in {"succeeded", "failed", "cancelled"}:
            return run
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not finish")


def seed_lead_and_contact(client, headers):
    lead = client.post("/api/v1/leads", headers=headers,
                       json={"company_name": "Buyer GmbH", "country": "DE"})
    assert lead.status_code == 201, lead.text
    contact = client.post("/api/v1/contacts", headers=headers,
                          json={"lead_id": lead.json()["id"], "email": "buyer@example.com"})
    assert contact.status_code == 201, contact.text
    return lead.json(), contact.json()


def test_all_product_routes_are_exposed():
    _, client, _, _ = make_client()
    names = {
        "companyId": "company_id", "userId": "user_id", "documentId": "document_id",
        "productId": "product_id", "countryCode": "country_code", "scanId": "scan_id",
        "leadId": "lead_id", "researchId": "research_id", "contactId": "contact_id",
        "campaignId": "campaign_id", "messageId": "message_id", "integrationId": "integration_id",
        "ruleId": "rule_id", "actionId": "action_id", "runId": "run_id",
        "providerMessageId": "provider_message_id", "exportId": "export_id",
        "sourceId": "source_id", "activityId": "activity_id", "snapshotId": "snapshot_id",
    }
    expected = set()
    for method, path in re.findall(
        r"^(GET|POST|PUT|PATCH|DELETE)\s+(/api/v1/[^\s]+)",
        (ROOT / "PRODUCT.md").read_text(encoding="utf-8"), re.MULTILINE,
    ):
        for old, new in names.items():
            path = path.replace(f":{old}", "{" + new + "}")
        expected.add((method, path))
    actual = {(method.upper(), path) for path, value in client.app.openapi()["paths"].items()
              for method in value if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}}
    assert expected <= actual, sorted(expected - actual)


def test_tenant_scope_blocks_cross_company_access():
    _, client, headers, _ = make_client()
    second = client.post("/api/v1/admin/companies", headers=headers, json={"name": "Other"}).json()
    user = client.post("/api/v1/admin/users", headers=headers, json={
        "email": "sales@acme.test", "password": "another-secure-password",
        "role": "customer", "company_id": headers["X-Company-ID"],
    }).json()
    login = client.post("/api/v1/auth/login", json={"email": user["email"],
                                                     "password": "another-secure-password"}).json()
    customer_headers = {"Authorization": f"Bearer {login['access_token']}",
                        "X-Company-ID": second["id"]}
    assert client.get("/api/v1/company/profile", headers=customer_headers).status_code == 403


def test_scan_limit_enforced_at_service_boundary():
    _, client, headers, _ = make_client()
    response = client.post("/api/v1/agent-runs", headers=headers, json={
        "run_type": "lead_scan", "payload": {"countries": ["DE", "FR", "NL", "GB", "ES", "IT"]},
    })
    assert response.status_code == 422


def test_company_brain_version_and_approval():
    _, client, headers, _ = make_client()
    client.post("/api/v1/products", headers=headers,
                json={"product_name": "Widget", "data": {"target_industries": ["retail"]}})
    started = client.post("/api/v1/company-brain/build", headers=headers, json={})
    run = wait_for_run(client, headers, started.json()["id"])
    assert run["status"] == "succeeded" and run["output_ref"]
    snapshot = client.get("/api/v1/company-brain/snapshots", headers=headers).json()[0]
    approved = client.post("/api/v1/company-brain/approve", headers=headers,
                           json={"snapshot_id": snapshot["id"]})
    assert approved.status_code == 200 and approved.json()["status"] == "approved"


def test_document_upload_and_processing_pipeline():
    _, client, headers, _ = make_client()
    uploaded = client.post(
        "/api/v1/documents/upload", headers=headers,
        data={"document_type": "product_catalog"},
        files={"file": ("catalog.txt", b"Widget catalogue", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    started = client.post(f"/api/v1/documents/{uploaded.json()['id']}/process", headers=headers)
    run = wait_for_run(client, headers, started.json()["id"])
    assert run["status"] == "succeeded"
    status = client.get(
        f"/api/v1/documents/{uploaded.json()['id']}/processing-status", headers=headers,
    ).json()
    assert status["status"] == "processed"


def test_local_password_reset_flow():
    _, client, headers, company_id = make_client()
    created = client.post("/api/v1/admin/users", headers=headers, json={
        "email": "reset@example.test", "password": "old-secure-password",
        "role": "customer", "company_id": company_id,
    })
    assert created.status_code == 201
    requested = client.post("/api/v1/auth/password-reset/request",
                            json={"email": "reset@example.test"})
    token = requested.json()["reset_token"]
    confirmed = client.post("/api/v1/auth/password-reset/confirm",
                            json={"token": token, "password": "new-secure-password"})
    assert confirmed.status_code == 200
    assert client.post("/api/v1/auth/login", json={"email": "reset@example.test",
                                                    "password": "new-secure-password"}).status_code == 200


def test_message_revision_invalidates_approval():
    _, client, headers, _ = make_client()
    lead, _ = seed_lead_and_contact(client, headers)
    started = client.post(f"/api/v1/leads/{lead['id']}/generate-outreach", headers=headers)
    run = wait_for_run(client, headers, started.json()["id"])
    message_id = run["output_ref"]
    assert client.post(f"/api/v1/outreach/messages/{message_id}/approve", headers=headers).status_code == 200
    updated = client.patch(f"/api/v1/outreach/messages/{message_id}", headers=headers,
                           json={"content": {"body": "A revised, specific partnership message."}})
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2 and not updated.json()["approved"]


def test_provider_draft_is_idempotent():
    app, client, headers, company_id = make_client()
    stamp = now()
    app.state.db.execute("INSERT INTO integrations VALUES(?,?,?,?,?,?,?,?,?)", (
        new_id("int"), company_id, "email", "stub", "connected", None, json_dump({}), stamp, stamp,
    ))
    lead, _ = seed_lead_and_contact(client, headers)
    run = wait_for_run(
        client, headers,
        client.post(f"/api/v1/leads/{lead['id']}/generate-outreach", headers=headers).json()["id"],
    )
    message_id = run["output_ref"]
    client.post(f"/api/v1/outreach/messages/{message_id}/approve", headers=headers)
    first = client.post(f"/api/v1/outreach/messages/{message_id}/create-draft", headers=headers)
    second = client.post(f"/api/v1/outreach/messages/{message_id}/create-draft", headers=headers)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["idempotent"] is True
    assert first.json()["provider_message_id"] == second.json()["provider_message_id"]
    delivery_runs = [run for run in client.get("/api/v1/agent-runs", headers=headers).json()
                     if run["run_type"] == "email_send"]
    assert len(delivery_runs) == 1 and delivery_runs[0]["status"] == "succeeded"


def test_csv_export_is_tenant_scoped():
    _, client, headers, _ = make_client()
    seed_lead_and_contact(client, headers)
    export = client.post("/api/v1/exports/leads", headers=headers, json={})
    assert export.status_code == 201 and export.json()["rows"] == 1
    download = client.get(f"/api/v1/exports/{export.json()['id']}/download", headers=headers)
    assert download.status_code == 200 and "Buyer GmbH" in download.content.decode("utf-8-sig")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} API qualification checks passed")
