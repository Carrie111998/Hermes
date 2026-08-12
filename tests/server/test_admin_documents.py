"""Authorization and content contracts for the admin document API."""

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.server.test_api_mvp import make_client, upload_document, wait_for_document, wait_for_run


@pytest.fixture()
def admin(tmp_path):
    app, client, headers, company_id = make_client()
    uploaded = upload_document(client, headers, "catalog.csv", b"sku,name\nA-1,Widget\n",
                               "text/csv")
    wait_for_document(client, headers, uploaded["id"])
    started = client.post(f"/api/v1/documents/{uploaded['id']}/process", headers=headers)
    wait_for_run(client, headers, started.json()["id"])
    return app, client, headers, company_id, uploaded["id"]


def customer_client(client, headers, company_id):
    created = client.post("/api/v1/admin/users", headers=headers, json={
        "email": f"customer{time.time_ns()}@example.test",
        "password": "another-secure-password", "role": "customer", "company_id": company_id,
    })
    assert created.status_code == 201, created.text
    login = client.post("/api/v1/auth/login", json={
        "email": created.json()["email"], "password": "another-secure-password",
    }).json()
    return {"Authorization": f"Bearer {login['access_token']}", "X-Company-ID": company_id}


def test_admin_document_detail_contains_artifacts_attempts_results_and_evidence(admin):
    _, client, headers, _, document_id = admin
    detail = client.get(f"/api/v1/admin/documents/{document_id}", headers=headers).json()

    assert {item["role"] for item in detail["artifacts"]} == {"original", "processed"}
    assert detail["attempts"]
    assert detail["attempts"][-1]["public_status"] == "ready"
    # The persisted semantic result is exactly what the run reported.
    output = detail["agent_run"]["output"]
    assert output["records"] == detail["records"]
    assert output["rejects"] == detail["rejects"]
    assert "evidence" in detail["agent_run"]
    assert detail["agent_run"]["related"]["document_id"] == document_id


def test_detail_never_embeds_artifact_bytes(admin):
    _, client, headers, _, document_id = admin
    detail = client.get(f"/api/v1/admin/documents/{document_id}", headers=headers).json()
    for artifact in detail["artifacts"]:
        assert "content" not in artifact
        assert artifact["checksum"] and artifact["size_bytes"] > 0


def test_customer_cannot_download_processed_artifact(admin):
    _, client, headers, company_id, document_id = admin
    response = client.get(
        f"/api/v1/admin/documents/{document_id}/artifacts/processed",
        headers=customer_client(client, headers, company_id),
    )
    assert response.status_code in {401, 403}


@pytest.mark.parametrize(
    "method,path_suffix",
    [("get", ""), ("get", "/artifacts/original"), ("post", "/retry"), ("delete", "")],
)
def test_every_document_route_requires_admin(admin, method, path_suffix):
    _, client, headers, company_id, document_id = admin
    response = getattr(client, method)(
        f"/api/v1/admin/documents/{document_id}{path_suffix}",
        headers=customer_client(client, headers, company_id),
    )
    assert response.status_code in {401, 403}


def test_admin_list_filters_by_company_and_status(admin):
    _, client, headers, company_id, document_id = admin
    other = client.post("/api/v1/admin/companies", headers=headers,
                        json={"name": "Other"}).json()

    listed = client.get(f"/api/v1/admin/documents?company_id={company_id}",
                        headers=headers).json()
    assert [item["id"] for item in listed] == [document_id]
    assert listed[0]["has_processed_artifact"] is True
    assert listed[0]["company_name"] == "Acme"

    assert client.get(f"/api/v1/admin/documents?company_id={other['id']}",
                      headers=headers).json() == []
    assert client.get("/api/v1/admin/documents?status=failed", headers=headers).json() == []


def test_artifact_download_streams_verified_bytes(admin):
    _, client, headers, _, document_id = admin
    original = client.get(f"/api/v1/admin/documents/{document_id}/artifacts/original",
                          headers=headers)
    assert original.status_code == 200
    assert original.content == b"sku,name\nA-1,Widget\n"
    assert original.headers["x-content-type-options"] == "nosniff"

    processed = client.get(f"/api/v1/admin/documents/{document_id}/artifacts/processed",
                           headers=headers)
    assert processed.status_code == 200
    assert b"Widget" in processed.content
    assert "content-disposition" in processed.headers


def test_artifact_download_rebuilds_a_deleted_mirror(admin):
    app, client, headers, company_id, document_id = admin
    processed = app.state.document_artifacts.get_active_processed(company_id, document_id)
    Path(processed.local_path).unlink()

    response = client.get(f"/api/v1/admin/documents/{document_id}/artifacts/processed",
                          headers=headers)
    assert response.status_code == 200
    assert b"Widget" in response.content


def test_unknown_artifact_role_is_a_404(admin):
    _, client, headers, _, document_id = admin
    assert client.get(f"/api/v1/admin/documents/{document_id}/artifacts/secret",
                      headers=headers).status_code == 404


def test_retry_reprocesses_the_document(admin):
    app, client, headers, company_id, document_id = admin
    before = app.state.document_artifacts.get_active_processed(company_id, document_id)

    response = client.post(f"/api/v1/admin/documents/{document_id}/retry", headers=headers)
    assert response.status_code == 202
    app.state.document_processing.wait_until_settled(company_id, document_id, timeout=10)

    after = app.state.document_artifacts.get_active_processed(company_id, document_id)
    assert after is not None and after.id != before.id
    assert app.state.db.one(
        "SELECT action FROM activity_log WHERE entity_id=? AND action='document_processing_retried'",
        (document_id,),
    )


def test_delete_removes_both_forms_and_records_activity(admin):
    app, client, headers, company_id, document_id = admin
    assert client.delete(f"/api/v1/admin/documents/{document_id}",
                         headers=headers).status_code == 204

    assert app.state.db.all(
        "SELECT id FROM document_artifacts WHERE document_id=?", (document_id,)
    ) == []
    assert app.state.db.one("SELECT id FROM documents WHERE id=?", (document_id,)) is None
    assert app.state.db.one(
        "SELECT id FROM activity_log WHERE entity_id=? AND action='document_deleted'",
        (document_id,),
    )
    assert client.get(f"/api/v1/admin/documents/{document_id}",
                      headers=headers).status_code == 404


def test_admin_run_detail_resolves_the_company_from_the_run(admin):
    app, client, headers, company_id, document_id = admin
    run_id = app.state.db.one(
        "SELECT processing_run_id FROM documents WHERE id=?", (document_id,)
    )["processing_run_id"]

    detail = client.get(f"/api/v1/admin/agent-runs/{run_id}/detail", headers=headers).json()
    assert detail["id"] == run_id
    assert detail["company_id"] == company_id
    assert "events" in detail and "evidence" in detail


def test_admin_run_detail_404s_for_unknown_runs_without_leaking(admin):
    _, client, headers, _, _ = admin
    response = client.get("/api/v1/admin/agent-runs/run_does_not_exist/detail",
                          headers=headers)
    assert response.status_code == 404
    assert "company" not in response.json()["detail"].lower()


def test_admin_detail_may_show_technical_reason_codes(admin):
    """The admin side is exactly where reason codes belong."""
    app, client, headers, company_id, document_id = admin
    attempt = app.state.document_artifacts.start_attempt(company_id, document_id)
    app.state.document_artifacts.finish_attempt(
        company_id, attempt.id, "failed", reason_code="encrypted",
        diagnostic="EncryptedError: locked",
        public_message="We couldn't process this file.",
    )
    detail = client.get(f"/api/v1/admin/documents/{document_id}", headers=headers).json()
    last = detail["attempts"][-1]
    assert last["reason_code"] == "encrypted"
    assert last["diagnostic"] == "EncryptedError: locked"
    # ...while the customer-facing sentence stays free of them.
    assert "Encrypted" not in (last["public_message"] or "")
