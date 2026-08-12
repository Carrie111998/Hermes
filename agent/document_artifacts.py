"""Profile-scoped document artifacts for standalone Hermes surfaces.

Interfaze has a tenant database behind it; the CLI, TUI, desktop app, and
messaging gateways do not. They still need the same guarantee: a file the user
attached keeps its original bytes AND gains a Markdown form the model can
actually read, both surviving a restart and a cleared cache directory.

This is the same contract as ``server/document_artifacts.py``, scoped to a
profile (``HERMES_HOME``) rather than a company, and synchronous — an attachment
is prepared while the user waits, not queued behind a worker pool.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from hermes_constants import get_hermes_home

from .document_processing import ProcessingDisposition, is_processable_document, process_document

__all__ = [
    "ProfileDocument",
    "ProfileDocumentArtifactStore",
    "get_store",
    "reset_store_cache",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT '',
    original_checksum TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'uploaded',
    status_detail TEXT,
    session_id TEXT,
    origin TEXT,
    processed_artifact_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('original','processed')),
    filename TEXT NOT NULL,
    content BLOB NOT NULL,
    checksum TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    local_path TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    reason_code TEXT,
    diagnostic TEXT,
    input_checksum TEXT NOT NULL,
    started_at REAL NOT NULL,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS ix_documents_checksum ON documents(original_checksum);
CREATE UNIQUE INDEX IF NOT EXISTS ix_artifacts_doc_role ON artifacts(document_id, role);
"""

# reason_code → product-safe sentence. Same rule as the server: never name the
# processor, the format, or the words conversion / OCR / Markdown.
_PUBLIC_DETAIL = {
    "encrypted": "This file is locked. Attach an unlocked copy to use it.",
    "advanced_processing_unavailable": "This file needs attention before it can be used.",
    "no_extractable_text": "No text could be read from this file.",
}
_DEFAULT_DETAIL = "This file couldn't be prepared."


@dataclass(frozen=True)
class ProfileDocument:
    id: str
    filename: str
    status: str
    status_detail: str | None
    original_path: str
    processed_path: str | None
    checksum: str


def _checksum(content: bytes) -> str:
    return sha256(content).hexdigest()


def _write_verified(path: Path, content: bytes, checksum: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(content)
    if _checksum(temporary.read_bytes()) != checksum:
        temporary.unlink(missing_ok=True)
        raise IOError("artifact checksum mismatch")
    temporary.replace(path)
    return path


class ProfileDocumentArtifactStore:
    def __init__(self, db_path: Path | None = None, root: Path | None = None):
        home = get_hermes_home()
        self.db_path = Path(db_path) if db_path else home / "document_artifacts.db"
        self.root = Path(root) if root else home / "cache" / "documents"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        # ponytail: one lock for the whole store — an attachment costs a file
        # conversion, so serializing the SQLite writes around it is free.
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    # ── sqlite plumbing ────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(sql, params).fetchone()

    def _execute(self, sql: str, params: tuple = ()) -> None:
        with self._connect() as conn:
            conn.execute(sql, params)
            conn.commit()

    def close(self) -> None:
        """No persistent handle to release; present so callers can be explicit."""

    # ── ingestion ──────────────────────────────────────────────────────────

    def ingest(
        self, path: Path, *, session_id: str = "", origin: str = ""
    ) -> ProfileDocument | None:
        """Store a document and produce its Markdown form.

        Returns ``None`` for files with no useful Markdown form (images, audio,
        archives) — those keep their existing attachment behavior untouched.
        """
        path = Path(path)
        if not is_processable_document(path.name):
            return None
        try:
            content = path.read_bytes()
        except OSError:
            return None

        checksum = _checksum(content)
        with self._lock:
            existing = self._one(
                "SELECT id FROM documents WHERE original_checksum=? AND status='ready'",
                (checksum,),
            )
            if existing:
                return self._load(existing["id"])

            document_id = f"pdoc_{uuid.uuid4().hex[:20]}"
            stamp = time.time()
            original_path = self.root / document_id / "original" / path.name
            _write_verified(original_path, content, checksum)

            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO documents(id,filename,content_type,original_checksum,status,"
                    "status_detail,session_id,origin,processed_artifact_id,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (document_id, path.name, "", checksum, "processing", None,
                     session_id, origin, None, stamp, stamp),
                )
                conn.execute(
                    "INSERT INTO artifacts(id,document_id,role,filename,content,checksum,"
                    "size_bytes,local_path,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"part_{uuid.uuid4().hex[:20]}", document_id, "original", path.name,
                     content, checksum, len(content), str(original_path), stamp),
                )
                conn.commit()

        self._process(document_id, original_path, path.name, checksum)
        return self._load(document_id)

    def _process(self, document_id: str, path: Path, filename: str, checksum: str) -> None:
        attempt_id = f"pat_{uuid.uuid4().hex[:20]}"
        started = time.time()
        self._execute(
            "INSERT INTO attempts(id,document_id,status,reason_code,diagnostic,"
            "input_checksum,started_at,completed_at) VALUES(?,?,?,?,?,?,?,?)",
            (attempt_id, document_id, "processing", None, None, checksum, started, None),
        )

        try:
            result = process_document(path=path, filename=filename)
        except BaseException as exc:  # noqa: BLE001 — an attachment must not crash the session
            self._settle(document_id, attempt_id, "failed", "processor_error",
                         type(exc).__name__)
            return

        if not result.ok or not (result.markdown or "").strip():
            status = (
                "needs_attention"
                if result.disposition is ProcessingDisposition.NEEDS_ATTENTION
                else "failed"
            )
            self._settle(document_id, attempt_id, status,
                         result.reason_code or "no_extractable_text", result.diagnostic)
            return

        content = result.markdown.encode("utf-8")
        output_checksum = _checksum(content)
        processed_path = self.root / document_id / "derived" / "content.md"
        _write_verified(processed_path, content, output_checksum)

        artifact_id = f"part_{uuid.uuid4().hex[:20]}"
        stamp = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO artifacts(id,document_id,role,filename,content,"
                "checksum,size_bytes,local_path,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (artifact_id, document_id, "processed", f"{Path(filename).stem}.md",
                 content, output_checksum, len(content), str(processed_path), stamp),
            )
            conn.execute(
                "UPDATE attempts SET status='ready',completed_at=? WHERE id=?",
                (stamp, attempt_id),
            )
            conn.execute(
                "UPDATE documents SET status='ready',status_detail=NULL,"
                "processed_artifact_id=?,updated_at=? WHERE id=?",
                (artifact_id, stamp, document_id),
            )
            conn.commit()

    def _settle(
        self, document_id: str, attempt_id: str, status: str,
        reason_code: str, diagnostic: str | None,
    ) -> None:
        stamp = time.time()
        detail = _PUBLIC_DETAIL.get(reason_code, _DEFAULT_DETAIL)
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE attempts SET status=?,reason_code=?,diagnostic=?,completed_at=?"
                " WHERE id=?",
                (status, reason_code, diagnostic, stamp, attempt_id),
            )
            conn.execute(
                "UPDATE documents SET status=?,status_detail=?,updated_at=? WHERE id=?",
                (status, detail, stamp, document_id),
            )
            conn.commit()

    # ── reads ──────────────────────────────────────────────────────────────

    def _load(self, document_id: str) -> ProfileDocument | None:
        row = self._one("SELECT * FROM documents WHERE id=?", (document_id,))
        if row is None:
            return None
        original = self._one(
            "SELECT local_path FROM artifacts WHERE document_id=? AND role='original'",
            (document_id,),
        )
        processed = None
        if row["status"] == "ready" and row["processed_artifact_id"]:
            path = self._materialize(row["processed_artifact_id"])
            processed = str(path) if path else None
        return ProfileDocument(
            id=row["id"],
            filename=row["filename"],
            status=row["status"],
            status_detail=row["status_detail"],
            original_path=original["local_path"] if original else "",
            processed_path=processed,
            checksum=row["original_checksum"],
        )

    def wait_until_settled(self, document_id: str, timeout: float = 30) -> ProfileDocument | None:
        """Ingestion is synchronous, so this only re-reads the settled row.

        It exists because callers legitimately hold a document id across a
        process boundary (the gateway stages a file, then prepares the prompt)
        and should not have to know which side did the work.
        """
        deadline = time.monotonic() + timeout
        while True:
            document = self._load(document_id)
            if document is None or document.status != "processing":
                return document
            if time.monotonic() >= deadline:
                return document
            time.sleep(0.02)

    def processed_path_for(self, original: Path) -> Path | None:
        """The verified Markdown sidecar for a file already in this store.

        Purely a lookup — it never ingests. A caller probing an arbitrary path
        must not cause that path to be read and persisted as a side effect.
        """
        original = Path(original)
        try:
            checksum = _checksum(original.read_bytes())
        except OSError:
            return None
        row = self._one(
            "SELECT processed_artifact_id FROM documents"
            " WHERE original_checksum=? AND status='ready' AND processed_artifact_id IS NOT NULL"
            " ORDER BY updated_at DESC LIMIT 1",
            (checksum,),
        )
        if row is None:
            return None
        return self._materialize(row["processed_artifact_id"])

    def _materialize(self, artifact_id: str) -> Path | None:
        row = self._one(
            "SELECT content, checksum, local_path FROM artifacts WHERE id=?", (artifact_id,)
        )
        if row is None:
            return None
        path = Path(row["local_path"])
        try:
            if path.is_file() and _checksum(path.read_bytes()) == row["checksum"]:
                return path
        except OSError:
            pass
        try:
            return _write_verified(path, bytes(row["content"]), row["checksum"])
        except (OSError, IOError):
            return None

    def forget(self, document_id: str) -> None:
        """Drop a document and its mirror. Used by cache cleanup, not by chat."""
        with self._lock:
            self._execute("DELETE FROM documents WHERE id=?", (document_id,))
        shutil.rmtree(self.root / document_id, ignore_errors=True)


# One store per resolved HERMES_HOME. Keyed rather than a bare global because
# tests and profile switches change HERMES_HOME within a live process, and a
# single cached instance would then read the previous profile's database.
_STORES: dict[str, ProfileDocumentArtifactStore] = {}
_STORES_LOCK = threading.Lock()


def get_store() -> ProfileDocumentArtifactStore:
    key = str(get_hermes_home())
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = ProfileDocumentArtifactStore()
            _STORES[key] = store
        return store


def reset_store_cache() -> None:
    with _STORES_LOCK:
        _STORES.clear()
