"""Legacy documents must gain an original artifact without losing their bytes."""

from pathlib import Path

import pytest

from server.db import Database, now
from server.document_artifacts import DocumentArtifactRepository


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "interfaze.db")
    stamp = now()
    database.execute(
        "INSERT INTO companies(id,name,legal_name,status,data,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?)",
        ("cmp_1", "Acme", "Acme", "active", "{}", stamp, stamp),
    )
    return database


@pytest.fixture()
def repo(db, tmp_path):
    return DocumentArtifactRepository(db, tmp_path / "uploads")


@pytest.fixture()
def legacy_file(tmp_path):
    path = tmp_path / "legacy-catalog.csv"
    path.write_bytes(b"sku,name\nA-1,Widget\n")
    return path


def seed_legacy_document(db, *, storage_path, status="uploaded", document_id="doc_old"):
    stamp = now()
    db.execute(
        "INSERT INTO documents(id,company_id,document_type,name,storage_path,content_type,"
        "size_bytes,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (document_id, "cmp_1", "product_catalog", "legacy-catalog.csv", storage_path,
         "text/csv", 0, status, "{}", stamp, stamp),
    )
    return document_id


def test_backfill_creates_original_artifact_and_queues_processing(repo, db, legacy_file):
    seed_legacy_document(db, storage_path=str(legacy_file), status="uploaded")
    summary = repo.backfill_existing_documents()
    assert summary == {"backfilled": 1, "missing": 0, "already_current": 0}
    assert db.one(
        "SELECT role FROM document_artifacts WHERE document_id='doc_old'"
    )["role"] == "original"
    assert db.one("SELECT status FROM documents WHERE id='doc_old'")["status"] in {
        "uploaded", "processing"
    }
    assert ("cmp_1", "doc_old") in repo.documents_awaiting_processing()


def test_backfilled_bytes_match_the_legacy_file_exactly(repo, db, legacy_file):
    seed_legacy_document(db, storage_path=str(legacy_file))
    repo.backfill_existing_documents()
    original = repo.get_original("cmp_1", "doc_old")
    assert repo.materialize("cmp_1", original.id).read_bytes() == legacy_file.read_bytes()
    # The legacy file itself is never moved or deleted.
    assert legacy_file.exists()


def test_backfill_is_idempotent_across_restarts(repo, db, legacy_file):
    seed_legacy_document(db, storage_path=str(legacy_file))
    assert repo.backfill_existing_documents()["backfilled"] == 1
    second = repo.backfill_existing_documents()
    assert second == {"backfilled": 0, "missing": 0, "already_current": 1}
    assert len(db.all("SELECT id FROM document_artifacts WHERE document_id='doc_old'")) == 1


def test_unreadable_legacy_row_gets_product_safe_attention(repo, db, tmp_path):
    seed_legacy_document(db, storage_path=str(tmp_path / "vanished.csv"))
    assert repo.backfill_existing_documents() == {
        "backfilled": 0, "missing": 1, "already_current": 0
    }
    row = db.one("SELECT status,status_detail FROM documents WHERE id='doc_old'")
    assert row["status"] == "needs_attention"
    for forbidden in ("anydoc", "converter", "conversion", "ocr", "markdown"):
        assert forbidden not in row["status_detail"].lower()


def test_legacy_row_without_a_storage_path_is_counted_missing(repo, db):
    seed_legacy_document(db, storage_path=None)
    assert repo.backfill_existing_documents()["missing"] == 1


def test_supabase_location_is_read_once_through_the_resolver(repo, db, monkeypatch,
                                                             legacy_file):
    seed_legacy_document(db, storage_path="supabase://interfaze-documents/cmp_1/doc_old/x.csv")

    calls = []

    class _Response:
        content = legacy_file.read_bytes()

        def raise_for_status(self):
            return None

    def _get(url, timeout=60):
        calls.append(url)
        return _Response()

    import httpx

    monkeypatch.setattr(httpx, "get", _get)
    summary = repo.backfill_existing_documents(resolver=lambda loc: f"https://signed/{loc}")

    assert summary["backfilled"] == 1
    assert len(calls) == 1
    original = repo.get_original("cmp_1", "doc_old")
    assert repo.materialize("cmp_1", original.id).read_bytes() == legacy_file.read_bytes()

    # And the signed URL is never needed again.
    repo.backfill_existing_documents(resolver=lambda loc: f"https://signed/{loc}")
    assert len(calls) == 1


def test_supabase_location_without_a_resolver_is_missing_not_fatal(repo, db):
    seed_legacy_document(db, storage_path="supabase://interfaze-documents/cmp_1/doc_old/x.csv")
    assert repo.backfill_existing_documents()["missing"] == 1


def test_awaiting_processing_skips_documents_that_already_have_a_sidecar(repo, db,
                                                                        legacy_file):
    seed_legacy_document(db, storage_path=str(legacy_file))
    repo.backfill_existing_documents()
    attempt = repo.start_attempt("cmp_1", "doc_old")
    processed = repo.store_processed("cmp_1", "doc_old", attempt.id, "# Widget")
    repo.finish_attempt("cmp_1", attempt.id, "ready", processed_artifact_id=processed.id)
    assert repo.documents_awaiting_processing() == []


def test_awaiting_processing_recovers_a_run_interrupted_by_a_restart(repo, db,
                                                                    legacy_file):
    seed_legacy_document(db, storage_path=str(legacy_file))
    repo.backfill_existing_documents()
    repo.start_attempt("cmp_1", "doc_old")  # left mid-flight, as a crash would
    assert ("cmp_1", "doc_old") in repo.documents_awaiting_processing()
