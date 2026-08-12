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
        # Already snake_case in both the doc and the route; listed so the
        # rewrite still strips the leading colon.
        "role": "role",
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


def upload_document(client, headers, name, content, content_type="text/plain",
                    document_type="product_catalog"):
    response = client.post(
        "/api/v1/documents/upload", headers=headers,
        data={"document_type": document_type},
        files={"file": (name, content, content_type)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def wait_for_document(client, headers, document_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        document = client.get(f"/api/v1/documents/{document_id}", headers=headers).json()
        if document["status"] in {"ready", "needs_attention", "failed"}:
            return document
        time.sleep(0.01)
    raise AssertionError(f"document {document_id} never settled")


def test_document_upload_stores_both_forms_and_semantic_run_reads_markdown():
    _, client, headers, _ = make_client()
    uploaded = upload_document(client, headers, "catalog.txt", b"Widget catalogue")
    ready = wait_for_document(client, headers, uploaded["id"])
    assert ready["status"] == "ready"
    assert set(ready) >= {"id", "name", "status"}
    assert "active_processed_artifact_id" not in ready

    started = client.post(f"/api/v1/documents/{uploaded['id']}/process", headers=headers)
    run = wait_for_run(client, headers, started.json()["id"])
    assert run["status"] == "succeeded"
    assert run["payload"]["path"].endswith("content.md")


def test_customer_document_json_hides_every_internal_field():
    _, client, headers, _ = make_client()
    uploaded = upload_document(client, headers, "catalog.csv", b"name\nWidget\n", "text/csv")
    ready = wait_for_document(client, headers, uploaded["id"])
    forbidden = {
        "active_processed_artifact_id", "current_processing_attempt_id",
        "original_checksum", "storage_path", "local_path", "reason_code", "diagnostic",
    }
    assert not (forbidden & set(ready))
    body = str(ready).lower()
    for term in ("anydoc", "converter", "conversion", "ocr", "markdown"):
        assert term not in body


def test_semantic_run_leaves_the_document_ready():
    """Technical readiness and semantic extraction are separate states."""
    _, client, headers, _ = make_client()
    uploaded = upload_document(client, headers, "catalog.txt", b"Widget catalogue")
    wait_for_document(client, headers, uploaded["id"])
    started = client.post(f"/api/v1/documents/{uploaded['id']}/process", headers=headers)
    wait_for_run(client, headers, started.json()["id"])

    document = client.get(f"/api/v1/documents/{uploaded['id']}", headers=headers).json()
    assert document["status"] == "ready"
    assert document["processing_run_id"]
    assert document["data"]["records"] or document["data"]["rejects"] == []


def test_semantic_reprocessing_replaces_records_instead_of_duplicating():
    _, client, headers, _ = make_client()
    uploaded = upload_document(client, headers, "catalog.txt", b"Widget catalogue")
    wait_for_document(client, headers, uploaded["id"])

    for _ in range(2):
        started = client.post(f"/api/v1/documents/{uploaded['id']}/process", headers=headers)
        assert started.status_code == 202, started.text
        wait_for_run(client, headers, started.json()["id"])

    products = client.get("/api/v1/products", headers=headers).json()
    from_document = [
        product for product in products
        if product.get("source_document_id") == uploaded["id"]
    ]
    assert len(from_document) == len({p["product_name"] for p in from_document})


def test_processing_an_unready_document_is_a_safe_conflict():
    _, client, headers, company_id = make_client()
    app = client.app
    document_id = new_id("doc")
    stamp = now()
    app.state.db.execute(
        "INSERT INTO documents(id,company_id,document_type,name,status,data,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (document_id, company_id, "product_catalog", "locked.pdf", "needs_attention",
         json_dump({}), stamp, stamp),
    )
    response = client.post(f"/api/v1/documents/{document_id}/process", headers=headers)
    assert response.status_code == 409
    detail = response.json()["detail"].lower()
    for term in ("anydoc", "converter", "conversion", "ocr", "markdown"):
        assert term not in detail


def test_oversized_upload_is_rejected_before_any_row_is_written():
    _, client, headers, company_id = make_client()
    app = client.app
    app.state.settings = type(app.state.settings)(
        **{**app.state.settings.__dict__, "max_upload_bytes": 8}
    )
    response = client.post(
        "/api/v1/documents/upload", headers=headers,
        data={"document_type": "product_catalog"},
        files={"file": ("big.txt", b"x" * 64, "text/plain")},
    )
    assert response.status_code == 413
    assert app.state.db.all("SELECT id FROM documents WHERE company_id=?", (company_id,)) == []


def test_deleting_a_document_removes_both_forms():
    _, client, headers, company_id = make_client()
    app = client.app
    uploaded = upload_document(client, headers, "catalog.csv", b"name\nWidget\n", "text/csv")
    wait_for_document(client, headers, uploaded["id"])

    assert client.delete(f"/api/v1/documents/{uploaded['id']}", headers=headers).status_code == 204
    assert app.state.db.all(
        "SELECT id FROM document_artifacts WHERE document_id=?", (uploaded["id"],)
    ) == []
    assert app.state.db.one("SELECT id FROM documents WHERE id=?", (uploaded["id"],)) is None


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


def test_production_password_reset_withholds_the_token():
    """The reset endpoint is unauthenticated, so a returned token is takeover."""
    root = Path(tempfile.mkdtemp(prefix="interfaze-api-prod-"))
    settings = Settings(
        database_path=root / "test.db",
        upload_dir=root / "uploads",
        bootstrap_admin_email="admin@example.test",
        bootstrap_admin_password="correct-horse-battery",
        credential_key=TEST_CREDENTIAL_KEY,
        public_base_url="https://agent.example.com",
    )
    client = TestClient(create_app(settings, run_executor=StubRunExecutor()))
    requested = client.post("/api/v1/auth/password-reset/request",
                            json={"email": settings.bootstrap_admin_email})
    assert requested.status_code == 202
    assert "reset_token" not in requested.json(), requested.text


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
