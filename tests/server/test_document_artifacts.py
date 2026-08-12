"""Durability, tenancy, and recovery contracts for document artifacts."""

from hashlib import sha256
from pathlib import Path

import pytest

from server.db import Database, new_id, now
from server.document_artifacts import DocumentArtifactRepository


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "interfaze.db")
    stamp = now()
    for company in ("cmp_1", "cmp_2"):
        database.execute(
            "INSERT INTO companies(id,name,legal_name,status,data,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (company, company, company, "active", "{}", stamp, stamp),
        )
    for document, company in (("doc_1", "cmp_1"), ("doc_2", "cmp_2")):
        database.execute(
            "INSERT INTO documents(id,company_id,document_type,name,status,data,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (document, company, "catalog", "report.pdf", "uploaded", "{}", stamp, stamp),
        )
    return database


@pytest.fixture()
def repo(db, tmp_path):
    return DocumentArtifactRepository(db, tmp_path / "uploads")


def test_original_and_processed_are_database_authoritative(repo, db, tmp_path):
    original = repo.store_original(
        "cmp_1", "doc_1", "report.pdf", "application/pdf", b"%PDF-test"
    )
    attempt = repo.start_attempt("cmp_1", "doc_1")
    processed = repo.store_processed("cmp_1", "doc_1", attempt.id, "# Report\nBody")

    assert db.one(
        "SELECT content FROM document_artifacts WHERE id=?", (original.id,)
    )["content"] == b"%PDF-test"
    assert db.one(
        "SELECT content FROM document_artifacts WHERE id=?", (processed.id,)
    )["content"] == b"# Report\nBody"

    Path(processed.local_path).unlink()
    rebuilt = repo.materialize("cmp_1", processed.id)
    assert rebuilt.read_text() == "# Report\nBody"
    assert sha256(rebuilt.read_bytes()).hexdigest() == processed.checksum


def test_corrupt_mirror_is_rebuilt_from_the_database(repo):
    original = repo.store_original("cmp_1", "doc_1", "report.pdf", "application/pdf", b"%PDF-x")
    Path(original.local_path).write_bytes(b"tampered")
    rebuilt = repo.materialize("cmp_1", original.id)
    assert rebuilt.read_bytes() == b"%PDF-x"


def test_another_company_cannot_materialize_or_delete(repo):
    original = repo.store_original("cmp_1", "doc_1", "report.pdf", "application/pdf", b"%PDF-x")
    with pytest.raises(LookupError):
        repo.materialize("cmp_2", original.id)
    with pytest.raises(LookupError):
        repo.delete_document("cmp_2", "doc_1")
    assert repo.get_original("cmp_1", "doc_1") is not None


def test_store_original_is_idempotent_per_checksum(repo, db):
    first = repo.store_original("cmp_1", "doc_1", "report.pdf", "application/pdf", b"%PDF-x")
    second = repo.store_original("cmp_1", "doc_1", "report.pdf", "application/pdf", b"%PDF-x")
    assert first.id == second.id
    rows = db.all(
        "SELECT id FROM document_artifacts WHERE document_id=? AND role='original'",
        ("doc_1",),
    )
    assert len(rows) == 1


def test_processed_artifact_is_reused_for_the_same_input(repo):
    repo.store_original("cmp_1", "doc_1", "report.pdf", "application/pdf", b"%PDF-x")
    attempt = repo.start_attempt("cmp_1", "doc_1")
    first = repo.store_processed("cmp_1", "doc_1", attempt.id, "# Body")
    repo.finish_attempt("cmp_1", attempt.id, "ready", processed_artifact_id=first.id)

    reused = repo.get_reusable_processed("cmp_1", "doc_1", first.input_checksum)
    assert reused is not None and reused.id == first.id
    assert repo.get_reusable_processed(
        "cmp_1", "doc_1", first.input_checksum, force=True
    ) is None


def test_failed_retry_preserves_the_previous_active_artifact(repo, db):
    repo.store_original("cmp_1", "doc_1", "report.pdf", "application/pdf", b"%PDF-x")
    good = repo.start_attempt("cmp_1", "doc_1")
    processed = repo.store_processed("cmp_1", "doc_1", good.id, "# Good")
    repo.finish_attempt("cmp_1", good.id, "ready", processed_artifact_id=processed.id)
    assert repo.get_active_processed("cmp_1", "doc_1").id == processed.id

    bad = repo.start_attempt("cmp_1", "doc_1")
    repo.finish_attempt("cmp_1", bad.id, "failed", reason_code="encrypted")

    assert repo.get_active_processed("cmp_1", "doc_1").id == processed.id
    assert db.one("SELECT status FROM documents WHERE id='doc_1'")["status"] == "failed"


def test_promotion_only_happens_on_success(repo, db):
    repo.store_original("cmp_1", "doc_1", "report.pdf", "application/pdf", b"%PDF-x")
    attempt = repo.start_attempt("cmp_1", "doc_1")
    assert db.one(
        "SELECT status,active_processed_artifact_id FROM documents WHERE id='doc_1'"
    )["status"] == "processing"
    processed = repo.store_processed("cmp_1", "doc_1", attempt.id, "# Body")
    assert db.one(
        "SELECT active_processed_artifact_id FROM documents WHERE id='doc_1'"
    )["active_processed_artifact_id"] is None
    repo.finish_attempt("cmp_1", attempt.id, "ready", processed_artifact_id=processed.id)
    row = db.one(
        "SELECT status,active_processed_artifact_id,ready_at FROM documents WHERE id='doc_1'"
    )
    assert row["status"] == "ready"
    assert row["active_processed_artifact_id"] == processed.id
    assert row["ready_at"]


def test_delete_document_removes_rows_and_mirror(repo, db, tmp_path):
    original = repo.store_original("cmp_1", "doc_1", "report.pdf", "application/pdf", b"%PDF-x")
    attempt = repo.start_attempt("cmp_1", "doc_1")
    repo.store_processed("cmp_1", "doc_1", attempt.id, "# Body")
    directory = Path(original.local_path).parent.parent

    repo.delete_document("cmp_1", "doc_1")

    assert db.all("SELECT id FROM document_artifacts WHERE document_id='doc_1'") == []
    assert db.all("SELECT id FROM document_processing_attempts WHERE document_id='doc_1'") == []
    assert db.one("SELECT id FROM documents WHERE id='doc_1'") is None
    assert not directory.exists()
    # The tenant root survives — only this document's directory is removed.
    assert (tmp_path / "uploads" / "cmp_1").exists()


def test_reason_codes_stay_out_of_the_public_status_detail(repo, db):
    repo.store_original("cmp_1", "doc_1", "report.pdf", "application/pdf", b"%PDF-x")
    attempt = repo.start_attempt("cmp_1", "doc_1")
    repo.finish_attempt(
        "cmp_1",
        attempt.id,
        "needs_attention",
        reason_code="advanced_processing_unavailable",
        diagnostic="ModuleNotFoundError: marker",
        public_message="This file needs attention before it can be used.",
    )
    row = db.one("SELECT status,status_detail FROM documents WHERE id='doc_1'")
    assert row["status"] == "needs_attention"
    assert "marker" not in row["status_detail"]
    assert "advanced_processing_unavailable" not in row["status_detail"]
    stored = db.one(
        "SELECT reason_code,diagnostic FROM document_processing_attempts WHERE id=?",
        (attempt.id,),
    )
    assert stored["reason_code"] == "advanced_processing_unavailable"


def test_unsafe_filenames_stay_inside_the_document_directory(repo, tmp_path):
    original = repo.store_original(
        "cmp_1", "doc_1", "../../etc/passwd", "text/plain", b"root"
    )
    resolved = Path(original.local_path).resolve()
    assert resolved.is_relative_to((tmp_path / "uploads" / "cmp_1" / "doc_1").resolve())


def test_backfill_is_idempotent(repo, db, tmp_path):
    legacy = tmp_path / "legacy.txt"
    legacy.write_bytes(b"Widget catalogue")
    db.execute("UPDATE documents SET storage_path=? WHERE id='doc_1'", (str(legacy),))
    db.execute("DELETE FROM documents WHERE id='doc_2'")

    first = repo.backfill_existing_documents()
    assert first == {"backfilled": 1, "missing": 0, "already_current": 0}
    assert db.one(
        "SELECT role FROM document_artifacts WHERE document_id='doc_1'"
    )["role"] == "original"

    second = repo.backfill_existing_documents()
    assert second == {"backfilled": 0, "missing": 0, "already_current": 1}


def test_metadata_reads_never_load_the_content_blob(repo, monkeypatch):
    """Reading metadata must not drag the document's bytes out of SQLite.

    `content` is the one heavy column and no metadata caller uses it — an
    ArtifactRecord has no content field. Selecting it anyway cost ~5ms per 5MB
    original on paths that run for every upload, every processing attempt, and
    every admin page render, scaling linearly with document size.
    """
    repo.store_original("cmp_1", "doc_1", "report.pdf", "application/pdf", b"%PDF-x" * 1000)

    selects: list[str] = []
    for name in ("one", "all"):
        original = getattr(repo.db, name)

        def record(sql, params=(), _original=original):
            if "FROM document_artifacts" in sql and sql.lstrip().upper().startswith("SELECT"):
                selects.append(sql)
            return _original(sql, params)

        monkeypatch.setattr(repo.db, name, record)

    repo.get_original("cmp_1", "doc_1")
    repo.get_active_processed("cmp_1", "doc_1")
    repo.list_artifacts("cmp_1", "doc_1")

    assert selects, "expected the metadata reads to hit document_artifacts"
    for sql in selects:
        projection = sql.split("FROM")[0].removeprefix("SELECT")
        columns = {part.strip().lower() for part in projection.split(",")}
        # `content_type` is fine — it is a short string. Only the bytes matter.
        assert "content" not in columns, sql


def test_materialize_still_reads_the_content_blob(repo):
    """The optimization must not reach the one caller that needs the bytes."""
    original = repo.store_original("cmp_1", "doc_1", "report.pdf", "application/pdf", b"%PDF-x")
    Path(original.local_path).unlink()
    assert repo.materialize("cmp_1", original.id).read_bytes() == b"%PDF-x"


def test_backfill_marks_unreadable_legacy_rows_needs_attention(repo, db, tmp_path):
    db.execute(
        "UPDATE documents SET storage_path=? WHERE id='doc_1'",
        (str(tmp_path / "gone.txt"),),
    )
    db.execute("DELETE FROM documents WHERE id='doc_2'")
    summary = repo.backfill_existing_documents()
    assert summary == {"backfilled": 0, "missing": 1, "already_current": 0}
    row = db.one("SELECT status,status_detail FROM documents WHERE id='doc_1'")
    assert row["status"] == "needs_attention"
    assert row["status_detail"]
