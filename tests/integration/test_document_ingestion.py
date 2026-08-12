"""The whole document path, end to end, with real conversion.

Nothing is stubbed except the agent process itself: the fixture is a real
document, `anydoc` really parses it, and the bytes really round-trip through
SQLite and the local mirror. That is the point — every other test in this
feature monkeypatches one seam, and the seams are exactly where a whole-path
regression would hide.
"""

from __future__ import annotations

import sys
import tempfile
import time
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.agent_service import StubRunExecutor
from server.app import create_app
from server.config import Settings

FIXTURES = ROOT / "tests" / "fixtures" / "documents"
TEST_CREDENTIAL_KEY = "KJ9KmdJiLL6itiwlEGTvGQ4ptS4dnd1ZZPyRPTwmjs4="


@pytest.fixture()
def stack(tmp_path, monkeypatch):
    """A full API with its own database, upload root, and HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    root = Path(tempfile.mkdtemp(prefix="interfaze-ingestion-", dir=tmp_path))
    settings = Settings(
        database_path=root / "test.db",
        upload_dir=root / "uploads",
        bootstrap_admin_email="admin@example.test",
        bootstrap_admin_password="correct-horse-battery",
        credential_key=TEST_CREDENTIAL_KEY,
    )
    app = create_app(settings, run_executor=StubRunExecutor())
    with TestClient(app) as client:
        login = client.post("/api/v1/auth/login", json={
            "email": settings.bootstrap_admin_email,
            "password": settings.bootstrap_admin_password,
        })
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        company = client.post("/api/v1/admin/companies", headers=headers,
                              json={"name": "Acme"})
        headers["X-Company-ID"] = company.json()["id"]
        yield app, client, headers, company.json()["id"]


def _wait_for(predicate, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition never became true")


@pytest.mark.parametrize(
    "fixture_name,content_type,needle",
    [
        ("sample.csv", "text/csv", "Widget"),
        ("sample.docx",
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "Quarterly catalogue"),
        ("sample.pdf", "application/pdf", "Terms and conditions"),
    ],
)
def test_upload_to_admin_audit_to_delete(stack, fixture_name, content_type, needle):
    app, client, headers, company_id = stack
    payload = (FIXTURES / fixture_name).read_bytes()

    # 1. Upload through the real API.
    uploaded = client.post(
        "/api/v1/documents/upload", headers=headers,
        data={"document_type": "product_catalog"},
        files={"file": (fixture_name, payload, content_type)},
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    assert uploaded.json()["status"] in {"uploaded", "processing"}

    # 2. Wait for Ready — real conversion, no stubbed processor.
    document = _wait_for(lambda: (
        lambda d: d if d["status"] in {"ready", "needs_attention", "failed"} else None
    )(client.get(f"/api/v1/documents/{document_id}", headers=headers).json()))
    assert document["status"] == "ready", document

    # 3. Both forms exist in the database AND on disk, checksum-verified.
    artifacts = app.state.document_artifacts
    original = artifacts.get_original(company_id, document_id)
    processed = artifacts.get_active_processed(company_id, document_id)
    assert original and processed

    rows = {
        row["role"]: row["content"]
        for row in app.state.db.all(
            "SELECT role, content FROM document_artifacts WHERE document_id=?",
            (document_id,),
        )
    }
    assert bytes(rows["original"]) == payload, "the original must be byte-identical"
    assert needle in bytes(rows["processed"]).decode("utf-8")
    assert sha256(payload).hexdigest() == original.checksum
    assert Path(original.local_path).read_bytes() == payload
    assert needle in Path(processed.local_path).read_text(encoding="utf-8")

    # 4. Deleting the local mirror does not lose anything: admin preview
    #    rebuilds it from the database.
    Path(processed.local_path).unlink()
    preview = client.get(
        f"/api/v1/admin/documents/{document_id}/artifacts/processed", headers=headers
    )
    assert preview.status_code == 200
    assert needle in preview.text
    assert Path(processed.local_path).is_file(), "the mirror must be restored"

    # 5. Semantic extraction reads the processed Markdown, not the original.
    started = client.post(f"/api/v1/documents/{document_id}/process", headers=headers)
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]
    assert started.json()["payload"]["path"].endswith("content.md")

    run = _wait_for(lambda: (
        lambda r: r if r["status"] in {"succeeded", "failed", "cancelled"} else None
    )(client.get(f"/api/v1/agent-runs/{run_id}", headers=headers).json()))
    assert run["status"] == "succeeded", run

    # The document itself stays Ready: the file is still perfectly usable.
    assert client.get(f"/api/v1/documents/{document_id}",
                      headers=headers).json()["status"] == "ready"

    # 6. Admin detail carries both forms, the attempts, the result, the run.
    detail = client.get(f"/api/v1/admin/documents/{document_id}", headers=headers).json()
    assert {a["role"] for a in detail["artifacts"]} == {"original", "processed"}
    assert detail["attempts"][-1]["public_status"] == "ready"
    assert detail["agent_run"]["id"] == run_id
    assert "evidence" in detail["agent_run"]

    # 7. Deleting removes both forms and the mirror directory.
    mirror_root = Path(original.local_path).parent.parent
    assert client.delete(f"/api/v1/admin/documents/{document_id}",
                         headers=headers).status_code == 204
    assert app.state.db.all(
        "SELECT id FROM document_artifacts WHERE document_id=?", (document_id,)
    ) == []
    assert app.state.db.one("SELECT id FROM documents WHERE id=?", (document_id,)) is None
    assert not mirror_root.exists()


def test_customer_never_sees_implementation_vocabulary(stack):
    _, client, headers, _ = stack
    uploaded = client.post(
        "/api/v1/documents/upload", headers=headers,
        data={"document_type": "product_catalog"},
        files={"file": ("sample.pdf", (FIXTURES / "sample.pdf").read_bytes(),
                        "application/pdf")},
    ).json()

    document = _wait_for(lambda: (
        lambda d: d if d["status"] in {"ready", "needs_attention", "failed"} else None
    )(client.get(f"/api/v1/documents/{uploaded['id']}", headers=headers).json()))

    listed = client.get("/api/v1/documents", headers=headers).json()
    body = (str(document) + str(listed)).lower()
    for forbidden in ("anydoc", "conversion", "converter", "markdown generation", "ocr"):
        assert forbidden not in body
    # And no internal identifiers leak either.
    for field in ("active_processed_artifact_id", "original_checksum", "storage_path"):
        assert field not in document


def test_a_locked_document_settles_with_actionable_copy(stack):
    """An encrypted PDF must fail with advice, not a stack trace."""
    _, client, headers, _ = stack
    # A PDF header with a body anydoc cannot parse.
    uploaded = client.post(
        "/api/v1/documents/upload", headers=headers,
        data={"document_type": "product_catalog"},
        files={"file": ("broken.pdf", b"%PDF-1.4\nnot a real pdf body\n",
                        "application/pdf")},
    ).json()

    document = _wait_for(lambda: (
        lambda d: d if d["status"] in {"ready", "needs_attention", "failed"} else None
    )(client.get(f"/api/v1/documents/{uploaded['id']}", headers=headers).json()))

    assert document["status"] in {"needs_attention", "failed"}
    assert document["status_detail"], "a non-ready document must say what to do"
    for forbidden in ("anydoc", "converter", "traceback", "ocr", "markdown"):
        assert forbidden not in document["status_detail"].lower()

    # Semantic extraction is refused, safely.
    refused = client.post(f"/api/v1/documents/{uploaded['id']}/process", headers=headers)
    assert refused.status_code == 409


def test_startup_backfills_and_processes_a_legacy_document(tmp_path, monkeypatch):
    """A pre-artifact document row gains both forms on the next boot."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    root = Path(tempfile.mkdtemp(prefix="interfaze-backfill-", dir=tmp_path))
    settings = Settings(
        database_path=root / "test.db",
        upload_dir=root / "uploads",
        bootstrap_admin_email="admin@example.test",
        bootstrap_admin_password="correct-horse-battery",
        credential_key=TEST_CREDENTIAL_KEY,
    )

    legacy = root / "legacy.csv"
    legacy.write_bytes((FIXTURES / "sample.csv").read_bytes())

    # First boot: seed a legacy-shaped row, as an older release would have left.
    app = create_app(settings, run_executor=StubRunExecutor())
    company_id = "cmp_legacy"
    from server.db import now

    stamp = now()
    app.state.db.execute(
        "INSERT INTO companies(id,name,legal_name,status,data,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (company_id, "Legacy", "Legacy", "active", "{}", stamp, stamp),
    )
    app.state.db.execute(
        "INSERT INTO documents(id,company_id,document_type,name,storage_path,content_type,"
        "size_bytes,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("doc_legacy", company_id, "product_catalog", "legacy.csv", str(legacy),
         "text/csv", 0, "uploaded", "{}", stamp, stamp),
    )

    # Second boot: lifespan runs the backfill and queues processing.
    app = create_app(settings, run_executor=StubRunExecutor())
    with TestClient(app):
        settled = app.state.document_processing.wait_until_settled(
            company_id, "doc_legacy", timeout=15
        )
        assert settled is not None and settled.public_status == "ready"

    processed = app.state.document_artifacts.get_active_processed(company_id, "doc_legacy")
    assert processed is not None
    assert "Widget" in Path(processed.local_path).read_text(encoding="utf-8")
    assert legacy.exists(), "the legacy file itself is never consumed"
