"""Lifecycle contracts for the background document processing coordinator."""

import time

import pytest

from agent.document_processing import DocumentProcessingResult, ProcessingDisposition
from server.db import Database, now
from server.document_artifacts import DocumentArtifactRepository
from server.document_processing_service import DocumentProcessingService


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "interfaze.db")
    stamp = now()
    database.execute(
        "INSERT INTO companies(id,name,legal_name,status,data,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?)",
        ("cmp_1", "Acme", "Acme", "active", "{}", stamp, stamp),
    )
    database.execute(
        "INSERT INTO documents(id,company_id,document_type,name,status,data,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("doc_1", "cmp_1", "catalog", "catalog.csv", "uploaded", "{}", stamp, stamp),
    )
    return database


@pytest.fixture()
def repo(db, tmp_path):
    repository = DocumentArtifactRepository(db, tmp_path / "uploads")
    repository.store_original("cmp_1", "doc_1", "catalog.csv", "text/csv", b"name\nWidget\n")
    return repository


@pytest.fixture()
def service(repo):
    instance = DocumentProcessingService(repo, workers=1, timeout_seconds=10)
    yield instance
    instance.shutdown()


def test_submit_transitions_uploaded_processing_ready(service, repo, db):
    service.submit("cmp_1", "doc_1")
    settled = service.wait_until_settled("cmp_1", "doc_1", timeout=5)
    assert settled.public_status == "ready"
    row = db.one("SELECT status,active_processed_artifact_id FROM documents WHERE id='doc_1'")
    assert row["status"] == "ready"
    assert row["active_processed_artifact_id"]
    processed = repo.get_active_processed("cmp_1", "doc_1")
    assert "Widget" in repo.materialize("cmp_1", processed.id).read_text()


def test_missing_fallback_dependency_is_needs_attention(service, db):
    service.processor = lambda **_: DocumentProcessingResult(
        ProcessingDisposition.NEEDS_ATTENTION,
        reason_code="advanced_processing_unavailable",
    )
    service.submit("cmp_1", "doc_1")
    service.wait_until_settled("cmp_1", "doc_1", timeout=5)
    row = db.one("SELECT status,status_detail FROM documents WHERE id='doc_1'")
    assert row["status"] == "needs_attention"
    assert "OCR" not in row["status_detail"]
    assert "advanced_processing_unavailable" not in row["status_detail"]


def test_encrypted_document_gets_actionable_public_copy(service, db):
    service.processor = lambda **_: DocumentProcessingResult(
        ProcessingDisposition.FAILED, reason_code="encrypted", diagnostic="EncryptedError: x"
    )
    service.submit("cmp_1", "doc_1")
    service.wait_until_settled("cmp_1", "doc_1", timeout=5)
    row = db.one("SELECT status,status_detail FROM documents WHERE id='doc_1'")
    assert row["status"] == "failed"
    assert "unlocked" in row["status_detail"]
    for forbidden in ("Anydoc", "converter", "OCR", "Markdown"):
        assert forbidden.lower() not in row["status_detail"].lower()


def test_technical_detail_stays_on_the_attempt_row(service, db):
    service.processor = lambda **_: DocumentProcessingResult(
        ProcessingDisposition.FAILED, reason_code="encrypted", diagnostic="EncryptedError: x"
    )
    service.submit("cmp_1", "doc_1")
    service.wait_until_settled("cmp_1", "doc_1", timeout=5)
    attempt = db.one(
        "SELECT reason_code,diagnostic FROM document_processing_attempts"
        " WHERE document_id='doc_1' ORDER BY started_at DESC LIMIT 1"
    )
    assert attempt["reason_code"] == "encrypted"
    assert attempt["diagnostic"] == "EncryptedError: x"


def test_blank_output_is_rejected(service, db):
    service.processor = lambda **_: DocumentProcessingResult(
        ProcessingDisposition.CONVERTED, markdown="   "
    )
    service.submit("cmp_1", "doc_1")
    service.wait_until_settled("cmp_1", "doc_1", timeout=5)
    assert db.one("SELECT status FROM documents WHERE id='doc_1'")["status"] == "needs_attention"


def test_oversized_output_is_rejected(repo, db):
    service = DocumentProcessingService(repo, workers=1, timeout_seconds=10, max_output_bytes=16)
    try:
        service.processor = lambda **_: DocumentProcessingResult(
            ProcessingDisposition.CONVERTED, markdown="x" * 100
        )
        service.submit("cmp_1", "doc_1")
        service.wait_until_settled("cmp_1", "doc_1", timeout=5)
        assert db.one("SELECT status FROM documents WHERE id='doc_1'")["status"] == "needs_attention"
    finally:
        service.shutdown()


def test_processor_timeout_fails_without_hanging(repo, db):
    service = DocumentProcessingService(repo, workers=1, timeout_seconds=0.2)
    try:
        def _slow(**_):
            time.sleep(1)
            return DocumentProcessingResult(ProcessingDisposition.CONVERTED, markdown="late")

        service.processor = _slow
        service.submit("cmp_1", "doc_1")
        settled = service.wait_until_settled("cmp_1", "doc_1", timeout=5)
        assert settled.public_status == "failed"
        assert settled.reason_code == "processing_timeout"
    finally:
        service.shutdown()


def test_retry_failure_preserves_the_previous_ready_artifact(service, repo, db):
    service.submit("cmp_1", "doc_1")
    service.wait_until_settled("cmp_1", "doc_1", timeout=5)
    good = repo.get_active_processed("cmp_1", "doc_1")
    assert good is not None

    service.processor = lambda **_: DocumentProcessingResult(
        ProcessingDisposition.FAILED, reason_code="malformed"
    )
    service.retry("cmp_1", "doc_1")
    service.wait_until_settled("cmp_1", "doc_1", timeout=5)

    assert repo.get_active_processed("cmp_1", "doc_1").id == good.id
    assert db.one("SELECT status FROM documents WHERE id='doc_1'")["status"] == "failed"


def test_resubmitting_identical_bytes_reuses_the_artifact(service, repo):
    service.submit("cmp_1", "doc_1")
    service.wait_until_settled("cmp_1", "doc_1", timeout=5)
    first = repo.get_active_processed("cmp_1", "doc_1")

    service.submit("cmp_1", "doc_1")
    service.wait_until_settled("cmp_1", "doc_1", timeout=5)
    assert repo.get_active_processed("cmp_1", "doc_1").id == first.id


def test_force_reprocesses_even_when_unchanged(service, repo):
    service.submit("cmp_1", "doc_1")
    service.wait_until_settled("cmp_1", "doc_1", timeout=5)
    first = repo.get_active_processed("cmp_1", "doc_1")

    service.submit("cmp_1", "doc_1", force=True)
    service.wait_until_settled("cmp_1", "doc_1", timeout=5)
    assert repo.get_active_processed("cmp_1", "doc_1").id != first.id


def test_shutdown_is_idempotent(repo):
    service = DocumentProcessingService(repo, workers=1)
    service.shutdown()
    service.shutdown()


def test_submitting_after_shutdown_is_refused(repo):
    service = DocumentProcessingService(repo, workers=1)
    service.shutdown()
    with pytest.raises(RuntimeError):
        service.submit("cmp_1", "doc_1")
