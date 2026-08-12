"""Durable storage for both forms of every uploaded document.

Two invariants drive the whole module:

1. **The database is the authority.** ``document_artifacts.content`` holds the
   complete bytes. The tenant-scoped tree under ``upload_root`` is a mirror that
   :meth:`DocumentArtifactRepository.materialize` rebuilds, checksum-verified,
   whenever a file is missing or has been tampered with.
2. **The original is never touched.** Processing only ever adds a ``processed``
   row. A failed retry leaves the previously promoted artifact in place.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .db import json_dump, new_id, now

__all__ = ["ArtifactRecord", "AttemptRecord", "DocumentArtifactRepository"]

# Statuses a customer may see. Anything else is an internal detail.
PUBLIC_STATUSES = ("uploaded", "processing", "ready", "needs_attention", "failed")

_ARTIFACT_COLUMNS = (
    "id, document_id, company_id, role, filename, content_type, content, "
    "checksum, size_bytes, local_path, attempt_id, metadata, created_at"
)
_ATTEMPT_COLUMNS = (
    "id, document_id, company_id, public_status, public_message, internal_stage, "
    "reason_code, diagnostic, input_checksum, output_checksum, run_id, "
    "started_at, completed_at"
)


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    document_id: str
    company_id: str
    role: str
    filename: str
    content_type: str
    checksum: str
    size_bytes: int
    local_path: str
    attempt_id: str | None
    metadata: dict
    created_at: float

    @property
    def input_checksum(self) -> str | None:
        """Checksum of the original this artifact was derived from."""
        return self.metadata.get("input_checksum")


@dataclass(frozen=True)
class AttemptRecord:
    id: str
    document_id: str
    company_id: str
    public_status: str
    public_message: str | None
    internal_stage: str
    reason_code: str | None
    diagnostic: str | None
    input_checksum: str
    output_checksum: str | None
    run_id: str | None
    started_at: float
    completed_at: float | None


def _checksum(content: bytes) -> str:
    return sha256(content).hexdigest()


def _safe_name(name: str) -> str:
    """Reduce an uploaded filename to a single harmless path segment."""
    stem = os.path.basename(str(name or "")).replace("\\", "/").rsplit("/", 1)[-1]
    stem = "".join(c for c in stem if c.isalnum() or c in "._- ").strip(" .")
    return stem[:120] or "document"


def _write_verified(path: Path, content: bytes, checksum: str) -> Path:
    """Write ``content`` atomically, refusing to publish a corrupt mirror."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(content)
    if _checksum(temporary.read_bytes()) != checksum:
        temporary.unlink(missing_ok=True)
        raise IOError("artifact checksum mismatch")
    temporary.replace(path)
    return path


def _artifact(row) -> ArtifactRecord:
    return ArtifactRecord(
        id=row["id"],
        document_id=row["document_id"],
        company_id=row["company_id"],
        role=row["role"],
        filename=row["filename"],
        content_type=row["content_type"],
        checksum=row["checksum"],
        size_bytes=int(row["size_bytes"]),
        local_path=row["local_path"],
        attempt_id=row["attempt_id"],
        metadata=json.loads(row["metadata"] or "{}"),
        created_at=float(row["created_at"]),
    )


def _attempt(row) -> AttemptRecord:
    return AttemptRecord(
        id=row["id"],
        document_id=row["document_id"],
        company_id=row["company_id"],
        public_status=row["public_status"],
        public_message=row["public_message"],
        internal_stage=row["internal_stage"],
        reason_code=row["reason_code"],
        diagnostic=row["diagnostic"],
        input_checksum=row["input_checksum"],
        output_checksum=row["output_checksum"],
        run_id=row["run_id"],
        started_at=float(row["started_at"]),
        completed_at=None if row["completed_at"] is None else float(row["completed_at"]),
    )


class DocumentArtifactRepository:
    def __init__(self, db, upload_root: Path | str):
        self.db = db
        self.upload_root = Path(upload_root)

    # ── paths ──────────────────────────────────────────────────────────────

    def _document_dir(self, company_id: str, document_id: str) -> Path:
        return self.upload_root / _safe_name(company_id) / _safe_name(document_id)

    def _original_path(self, company_id: str, document_id: str, filename: str) -> Path:
        return self._document_dir(company_id, document_id) / "original" / _safe_name(filename)

    def _processed_path(self, company_id: str, document_id: str) -> Path:
        return self._document_dir(company_id, document_id) / "derived" / "content.md"

    # ── reads ──────────────────────────────────────────────────────────────

    def get_artifact(self, company_id: str, artifact_id: str) -> ArtifactRecord | None:
        row = self.db.one(
            f"SELECT {_ARTIFACT_COLUMNS} FROM document_artifacts WHERE id=? AND company_id=?",
            (artifact_id, company_id),
        )
        return _artifact(row) if row else None

    def get_original(self, company_id: str, document_id: str) -> ArtifactRecord | None:
        row = self.db.one(
            f"SELECT {_ARTIFACT_COLUMNS} FROM document_artifacts"
            " WHERE company_id=? AND document_id=? AND role='original'"
            " ORDER BY created_at DESC LIMIT 1",
            (company_id, document_id),
        )
        return _artifact(row) if row else None

    def get_active_processed(self, company_id: str, document_id: str) -> ArtifactRecord | None:
        row = self.db.one(
            "SELECT active_processed_artifact_id FROM documents WHERE id=? AND company_id=?",
            (document_id, company_id),
        )
        if not row or not row["active_processed_artifact_id"]:
            return None
        return self.get_artifact(company_id, row["active_processed_artifact_id"])

    def get_reusable_processed(
        self, company_id: str, document_id: str, input_checksum: str, *, force: bool = False
    ) -> ArtifactRecord | None:
        """A verified processed artifact already derived from these exact bytes."""
        if force:
            return None
        active = self.get_active_processed(company_id, document_id)
        if active is None or active.input_checksum != input_checksum:
            return None
        return active

    def list_artifacts(self, company_id: str, document_id: str) -> list[ArtifactRecord]:
        return [
            _artifact(row)
            for row in self.db.all(
                f"SELECT {_ARTIFACT_COLUMNS} FROM document_artifacts"
                " WHERE company_id=? AND document_id=? ORDER BY created_at",
                (company_id, document_id),
            )
        ]

    def list_attempts(self, company_id: str, document_id: str) -> list[AttemptRecord]:
        return [
            _attempt(row)
            for row in self.db.all(
                f"SELECT {_ATTEMPT_COLUMNS} FROM document_processing_attempts"
                " WHERE company_id=? AND document_id=? ORDER BY started_at",
                (company_id, document_id),
            )
        ]

    # ── writes ─────────────────────────────────────────────────────────────

    def store_original(
        self,
        company_id: str,
        document_id: str,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> ArtifactRecord:
        """Persist the uploaded bytes. Idempotent per (document, checksum)."""
        checksum = _checksum(content)
        existing = self.db.one(
            f"SELECT {_ARTIFACT_COLUMNS} FROM document_artifacts"
            " WHERE company_id=? AND document_id=? AND role='original' AND checksum=?",
            (company_id, document_id, checksum),
        )
        if existing:
            record = _artifact(existing)
            self.materialize(company_id, record.id)
            return record

        path = self._original_path(company_id, document_id, filename)
        _write_verified(path, content, checksum)

        artifact_id = new_id("art")
        stamp = now()
        with self.db.transaction() as conn:
            conn.execute(
                f"INSERT INTO document_artifacts({_ARTIFACT_COLUMNS})"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id, document_id, company_id, "original",
                    _safe_name(filename), content_type or "application/octet-stream",
                    content, checksum, len(content), str(path), None, "{}", stamp,
                ),
            )
            conn.execute(
                "UPDATE documents SET original_checksum=?, storage_path=?, size_bytes=?,"
                " content_type=COALESCE(?, content_type), updated_at=?"
                " WHERE id=? AND company_id=?",
                (checksum, str(path), len(content), content_type or None,
                 stamp, document_id, company_id),
            )
        return self.get_artifact(company_id, artifact_id)  # type: ignore[return-value]

    def start_attempt(
        self, company_id: str, document_id: str, *, run_id: str | None = None
    ) -> AttemptRecord:
        """Open an attempt and move the document into the Processing state."""
        original = self.get_original(company_id, document_id)
        if original is None:
            raise LookupError(f"no original artifact for document {document_id}")

        attempt_id = new_id("dpa")
        stamp = now()
        with self.db.transaction() as conn:
            conn.execute(
                f"INSERT INTO document_processing_attempts({_ATTEMPT_COLUMNS})"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id, document_id, company_id, "processing", None, "queued",
                    None, None, original.checksum, None, run_id, stamp, None,
                ),
            )
            conn.execute(
                "UPDATE documents SET status='processing', status_detail=NULL,"
                " current_processing_attempt_id=?, processing_started_at=?, updated_at=?"
                " WHERE id=? AND company_id=?",
                (attempt_id, stamp, stamp, document_id, company_id),
            )
        return self.get_attempt(company_id, attempt_id)  # type: ignore[return-value]

    def get_attempt(self, company_id: str, attempt_id: str) -> AttemptRecord | None:
        row = self.db.one(
            f"SELECT {_ATTEMPT_COLUMNS} FROM document_processing_attempts"
            " WHERE id=? AND company_id=?",
            (attempt_id, company_id),
        )
        return _attempt(row) if row else None

    def store_processed(
        self,
        company_id: str,
        document_id: str,
        attempt_id: str,
        markdown: str,
        *,
        metadata: dict | None = None,
    ) -> ArtifactRecord:
        """Write the Markdown sidecar. Does NOT promote it — finish_attempt does."""
        original = self.get_original(company_id, document_id)
        if original is None:
            raise LookupError(f"no original artifact for document {document_id}")

        content = markdown.encode("utf-8")
        checksum = _checksum(content)
        path = self._processed_path(company_id, document_id)
        _write_verified(path, content, checksum)

        payload = dict(metadata or {})
        payload["input_checksum"] = original.checksum

        artifact_id = new_id("art")
        stamp = now()
        self.db.execute(
            f"INSERT INTO document_artifacts({_ARTIFACT_COLUMNS})"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                artifact_id, document_id, company_id, "processed",
                f"{Path(original.filename).stem or 'document'}.md", "text/markdown",
                content, checksum, len(content), str(path), attempt_id,
                json_dump(payload), stamp,
            ),
        )
        return self.get_artifact(company_id, artifact_id)  # type: ignore[return-value]

    def finish_attempt(
        self,
        company_id: str,
        attempt_id: str,
        public_status: str,
        *,
        processed_artifact_id: str | None = None,
        reason_code: str | None = None,
        diagnostic: str | None = None,
        public_message: str | None = None,
        internal_stage: str = "completed",
    ) -> AttemptRecord:
        """Close the attempt and, only on success, promote its artifact.

        Promotion happens in the same transaction that marks the attempt done,
        so a document is never Ready while pointing at nothing — and a failing
        retry leaves the previously promoted artifact exactly where it was.
        """
        if public_status not in PUBLIC_STATUSES:
            raise ValueError(f"{public_status!r} is not a customer-visible status")

        attempt = self.get_attempt(company_id, attempt_id)
        if attempt is None:
            raise LookupError(f"unknown attempt {attempt_id}")

        promote = public_status == "ready" and processed_artifact_id is not None
        output_checksum = None
        if promote:
            artifact = self.get_artifact(company_id, processed_artifact_id)  # type: ignore[arg-type]
            if artifact is None or artifact.document_id != attempt.document_id:
                raise LookupError(f"unknown processed artifact {processed_artifact_id}")
            output_checksum = artifact.checksum

        stamp = now()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE document_processing_attempts SET public_status=?, public_message=?,"
                " internal_stage=?, reason_code=?, diagnostic=?, output_checksum=?,"
                " completed_at=? WHERE id=? AND company_id=?",
                (public_status, public_message, internal_stage, reason_code, diagnostic,
                 output_checksum, stamp, attempt_id, company_id),
            )
            if promote:
                conn.execute(
                    "UPDATE documents SET status='ready', status_detail=NULL,"
                    " active_processed_artifact_id=?, current_processing_attempt_id=NULL,"
                    " ready_at=?, updated_at=? WHERE id=? AND company_id=?",
                    (processed_artifact_id, stamp, stamp, attempt.document_id, company_id),
                )
            else:
                conn.execute(
                    "UPDATE documents SET status=?, status_detail=?,"
                    " current_processing_attempt_id=NULL, updated_at=?"
                    " WHERE id=? AND company_id=?",
                    (public_status, public_message, stamp, attempt.document_id, company_id),
                )
        return self.get_attempt(company_id, attempt_id)  # type: ignore[return-value]

    # ── recovery ───────────────────────────────────────────────────────────

    def materialize(self, company_id: str, artifact_id: str) -> Path:
        """Return a verified on-disk path, rebuilding the mirror when needed."""
        row = self.db.one(
            "SELECT content, checksum, local_path FROM document_artifacts"
            " WHERE id=? AND company_id=?",
            (artifact_id, company_id),
        )
        if row is None:
            raise LookupError(f"unknown artifact {artifact_id}")

        path = Path(row["local_path"])
        checksum = row["checksum"]
        try:
            if path.is_file() and _checksum(path.read_bytes()) == checksum:
                return path
        except OSError:
            pass
        return _write_verified(path, bytes(row["content"]), checksum)

    # ── deletion ───────────────────────────────────────────────────────────

    def delete_document(self, company_id: str, document_id: str) -> dict:
        """Remove both forms. Rows go first; the mirror is best-effort cleanup."""
        owner = self.db.one(
            "SELECT id FROM documents WHERE id=? AND company_id=?",
            (document_id, company_id),
        )
        if owner is None:
            raise LookupError(f"unknown document {document_id}")

        directory = self._document_dir(company_id, document_id)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE documents SET active_processed_artifact_id=NULL,"
                " current_processing_attempt_id=NULL WHERE id=? AND company_id=?",
                (document_id, company_id),
            )
            conn.execute(
                "DELETE FROM document_artifacts WHERE document_id=? AND company_id=?",
                (document_id, company_id),
            )
            conn.execute(
                "DELETE FROM document_processing_attempts WHERE document_id=? AND company_id=?",
                (document_id, company_id),
            )
            conn.execute(
                "DELETE FROM documents WHERE id=? AND company_id=?",
                (document_id, company_id),
            )

        # Only this document's own directory, never a parent. A cleanup failure
        # is recorded, not retried by resurrecting the rows we just removed.
        cleanup_error = None
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            pass
        except OSError as exc:
            cleanup_error = f"{type(exc).__name__}: {exc.strerror or ''}".strip()
        return {"deleted": True, "cleanup_error": cleanup_error}

    # ── legacy migration ───────────────────────────────────────────────────

    def backfill_existing_documents(self, *, resolver=None) -> dict:
        """Give pre-artifact document rows an original artifact.

        ``resolver`` is called only for non-local ``storage_path`` values (a
        Supabase object). It exists so this one migration read can go through
        the old storage backend; nothing after the backfill depends on it.
        """
        summary = {"backfilled": 0, "missing": 0, "already_current": 0}
        rows = self.db.all(
            "SELECT d.id, d.company_id, d.name, d.content_type, d.storage_path"
            " FROM documents d WHERE NOT EXISTS ("
            "   SELECT 1 FROM document_artifacts a"
            "   WHERE a.document_id=d.id AND a.role='original')"
        )
        current = self.db.all(
            "SELECT DISTINCT document_id FROM document_artifacts WHERE role='original'"
        )
        summary["already_current"] = len(current)

        for row in rows:
            content = self._legacy_bytes(row["storage_path"], resolver)
            if content is None:
                summary["missing"] += 1
                self.db.execute(
                    "UPDATE documents SET status='needs_attention', status_detail=?,"
                    " updated_at=? WHERE id=? AND company_id=?",
                    ("This file needs attention before it can be used.", now(),
                     row["id"], row["company_id"]),
                )
                continue
            self.store_original(
                row["company_id"], row["id"], row["name"] or "document",
                row["content_type"] or "application/octet-stream", content,
            )
            self.db.execute(
                "UPDATE documents SET status='uploaded', status_detail=NULL, updated_at=?"
                " WHERE id=? AND company_id=? AND status NOT IN ('processing','ready')",
                (now(), row["id"], row["company_id"]),
            )
            summary["backfilled"] += 1
        return summary

    def documents_awaiting_processing(self) -> list[tuple[str, str]]:
        """(company_id, document_id) for stored originals that have no sidecar.

        Covers both the legacy rows the backfill just created and any document
        whose processing was interrupted by a restart.
        """
        return [
            (row["company_id"], row["id"])
            for row in self.db.all(
                "SELECT d.id, d.company_id FROM documents d"
                " WHERE d.active_processed_artifact_id IS NULL"
                "   AND d.status IN ('uploaded','processing')"
                "   AND EXISTS (SELECT 1 FROM document_artifacts a"
                "               WHERE a.document_id=d.id AND a.role='original')"
            )
        ]

    @staticmethod
    def _legacy_bytes(storage_path: str | None, resolver) -> bytes | None:
        if not storage_path:
            return None
        if storage_path.startswith("supabase://"):
            if resolver is None:
                return None
            try:
                import httpx

                response = httpx.get(resolver(storage_path), timeout=60)
                response.raise_for_status()
                return response.content
            except Exception:
                return None
        try:
            return Path(storage_path).read_bytes()
        except OSError:
            return None
