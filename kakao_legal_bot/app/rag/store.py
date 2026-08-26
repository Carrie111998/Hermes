"""Local document retrieval (주석서 · 법률서적 · 사무실 내부자료).

BM25 over a Korean bigram index, optionally re-ranked with embeddings.
Kept in its own SQLite file so the corpus can be rebuilt from scratch
without touching live conversation state.
"""

from __future__ import annotations

import json
import math
import sqlite3
import struct
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .tokenize import index_blob, match_query

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    meta       TEXT NOT NULL DEFAULT '{}',
    sha        TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_source ON docs(source);

CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id     INTEGER NOT NULL,
    ord        INTEGER NOT NULL,
    text       TEXT NOT NULL,
    locator    TEXT NOT NULL DEFAULT '',
    embedding  BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, ord);

CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    blob,
    tokenize = 'unicode61 remove_diacritics 0'
);
"""


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    text: str
    title: str
    source: str
    locator: str
    score: float
    # Which index this came from. Empty for a single-index deployment.
    collection: str = ""

    @property
    def citation(self) -> str:
        parts = [part for part in (self.title or self.source, self.locator) if part]
        return " ".join(parts)


def pack_embedding(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_embedding(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob[: count * 4]))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    """Split on paragraph boundaries, packing up to ``size`` characters."""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        while len(para) > size:
            # A single monster paragraph (scanned book pages do this).
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:size])
            para = para[max(size - overlap, 1) :]
        if len(current) + len(para) + 2 > size and current:
            chunks.append(current)
            tail = current[-overlap:] if overlap > 0 else ""
            current = f"{tail}\n\n{para}".strip() if tail else para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current.strip():
        chunks.append(current.strip())
    return chunks


class RagStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── ingest ───────────────────────────────────────────────────────────
    def upsert_document(
        self,
        source: str,
        title: str,
        chunks: Sequence[str],
        meta: dict[str, Any] | None = None,
        sha: str = "",
        locators: Sequence[str] | None = None,
    ) -> int:
        """Replace a document and all its chunks. Returns the chunk count."""
        locators = list(locators or [])
        with self._lock:
            row = self._conn.execute("SELECT id, sha FROM docs WHERE source = ?", (source,)).fetchone()
            if row is not None:
                if sha and row["sha"] == sha:
                    return 0  # unchanged — skip the re-embed cost
                self._delete_doc_locked(int(row["id"]))
            cur = self._conn.execute(
                "INSERT INTO docs(source, title, meta, sha, created_at) VALUES(?, ?, ?, ?, ?)",
                (source, title, json.dumps(meta or {}, ensure_ascii=False), sha, time.time()),
            )
            doc_id = int(cur.lastrowid or 0)
            for ordinal, text in enumerate(chunks):
                locator = locators[ordinal] if ordinal < len(locators) else ""
                chunk_cur = self._conn.execute(
                    "INSERT INTO chunks(doc_id, ord, text, locator) VALUES(?, ?, ?, ?)",
                    (doc_id, ordinal, text, locator),
                )
                self._conn.execute(
                    "INSERT INTO chunk_fts(rowid, blob) VALUES(?, ?)",
                    (chunk_cur.lastrowid, index_blob(f"{title}\n{text}")),
                )
            self._conn.commit()
            return len(chunks)

    def _delete_doc_locked(self, doc_id: int) -> None:
        ids = [
            int(row["id"])
            for row in self._conn.execute("SELECT id FROM chunks WHERE doc_id = ?", (doc_id,))
        ]
        for chunk_id in ids:
            self._conn.execute("DELETE FROM chunk_fts WHERE rowid = ?", (chunk_id,))
        self._conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        self._conn.execute("DELETE FROM docs WHERE id = ?", (doc_id,))

    def delete_document(self, source: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT id FROM docs WHERE source = ?", (source,)).fetchone()
            if row is None:
                return False
            self._delete_doc_locked(int(row["id"]))
            self._conn.commit()
            return True

    def set_embedding(self, chunk_id: int, vector: Sequence[float]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE chunks SET embedding = ? WHERE id = ?", (pack_embedding(vector), chunk_id)
            )
            self._conn.commit()

    def chunks_without_embeddings(self, limit: int = 256) -> list[tuple[int, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text FROM chunks WHERE embedding IS NULL ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [(int(row["id"]), str(row["text"])) for row in rows]

    # ── query ────────────────────────────────────────────────────────────
    def stats(self) -> dict[str, int]:
        with self._lock:
            docs = self._conn.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
            chunks = self._conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
            embedded = self._conn.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE embedding IS NOT NULL"
            ).fetchone()["n"]
        return {"documents": int(docs), "chunks": int(chunks), "embedded": int(embedded)}

    def search(self, query: str, top_k: int = 6, candidates: int = 40) -> list[Hit]:
        expression = match_query(query)
        if not expression:
            return []
        sql = """
            SELECT c.id AS chunk_id, c.text AS text, c.locator AS locator,
                   d.title AS title, d.source AS source, bm25(chunk_fts) AS score
              FROM chunk_fts
              JOIN chunks c ON c.id = chunk_fts.rowid
              JOIN docs d ON d.id = c.doc_id
             WHERE chunk_fts MATCH ?
             ORDER BY score
             LIMIT ?
        """
        with self._lock:
            try:
                rows = self._conn.execute(sql, (expression, candidates)).fetchall()
            except sqlite3.OperationalError:
                # Malformed MATCH expression (weird punctuation in the
                # question) — a failed search must never break the answer.
                return []
        hits = [
            Hit(
                chunk_id=int(row["chunk_id"]),
                text=str(row["text"]),
                title=str(row["title"]),
                source=str(row["source"]),
                locator=str(row["locator"]),
                # bm25() is "lower is better"; flip it so bigger = better
                # and every scorer in this file points the same way.
                score=-float(row["score"]),
            )
            for row in rows
        ]
        return hits[:top_k]

    def search_with_embedding(
        self, query: str, query_vector: Sequence[float], top_k: int = 6, candidates: int = 40
    ) -> list[Hit]:
        """BM25 recall, embedding precision.

        The lexical pass picks candidates (cheap, no API call per chunk),
        then cosine similarity reorders them. Chunks with no stored vector
        keep their lexical rank rather than dropping out.
        """
        lexical = self.search(query, top_k=candidates, candidates=candidates)
        if not lexical or not query_vector:
            return lexical[:top_k]
        ids = [hit.chunk_id for hit in lexical]
        placeholders = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, embedding FROM chunks WHERE id IN ({placeholders})",  # noqa: S608
                ids,
            ).fetchall()
        vectors = {int(row["id"]): unpack_embedding(row["embedding"]) for row in rows}

        best_lexical = max((hit.score for hit in lexical), default=0.0) or 1.0
        rescored: list[Hit] = []
        for hit in lexical:
            lexical_norm = hit.score / best_lexical if best_lexical else 0.0
            vector = vectors.get(hit.chunk_id)
            if vector:
                score = 0.35 * lexical_norm + 0.65 * cosine(query_vector, vector)
            else:
                score = lexical_norm * 0.5
            rescored.append(
                Hit(
                    chunk_id=hit.chunk_id,
                    text=hit.text,
                    title=hit.title,
                    source=hit.source,
                    locator=hit.locator,
                    score=score,
                )
            )
        rescored.sort(key=lambda hit: hit.score, reverse=True)
        return rescored[:top_k]
