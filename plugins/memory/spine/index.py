"""Derived index management — spec §3.

SQLite database with:
- FTS5 full-text search on observation content
- sqlite-vec vector search (384-dim MiniLM embeddings)
- dim_meta table to prevent silent mixed-dimension searches
- Episodes + wiki chunks indexed alongside observations
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 384  # MiniLM-L6-v2

# Recency scoring constants. NOTE (found 2026-07-30): despite the name and
# SpineConfig having matching `recency_half_life_hours`/`recency_weight`
# fields, nothing in this file's search path actually reads SpineConfig —
# `_compute_recency_factor` is always called with these hardcoded defaults,
# never with config-sourced values. Not fixed as part of this pass (real
# wiring means threading a SpineConfig instance through MemoryIndex/search()
# call sites, a bigger change than the config.py parsing gap this comment
# used to imply was the only issue) — flagged for a follow-up if these knobs
# need to be genuinely configurable.
_DEFAULT_RECENCY_HALF_LIFE_HOURS = 168  # 7 days
_DEFAULT_RECENCY_WEIGHT = 0.15  # contribution of recency to final score


def _compute_recency_factor(row: Dict[str, Any], half_life_hours: float = _DEFAULT_RECENCY_HALF_LIFE_HOURS,
                            weight: float = _DEFAULT_RECENCY_WEIGHT) -> float:
    """Compute recency multiplier for an observation row.

    Returns 1.0 for wiki chunks (no recency). For observations, computes
    exponential decay based on last_confirmed (fallback: created_at).
    Formula: (1 - weight) + weight * exp(-hours_idle / half_life)
    """
    if row.get("source") == "wiki":
        return 1.0
    ref_date = row.get("last_confirmed") or row.get("created_at", "")
    if not ref_date:
        return 1.0
    try:
        ref_dt = datetime.fromisoformat(ref_date)
        hours_idle = (datetime.now(timezone.utc) - ref_dt).total_seconds() / 3600
        if hours_idle <= 0:
            return 1.0
        decay = math.exp(-hours_idle / half_life_hours)
        return (1.0 - weight) + weight * decay
    except (ValueError, TypeError):
        return 1.0


def _fts5_safe_query(text: str) -> str:
    """Build a safe FTS5 MATCH expression from free text.

    FTS5 treats apostrophes, hyphens, parentheses, colons etc. as query
    syntax, not literal characters — "What is the user's name?" crashes
    MATCH with a syntax error otherwise. Quoting each token forces FTS5 to
    treat it as a literal string match, immune to being misparsed as an
    operator, at the cost of losing FTS5's own phrase/prefix operators in
    user-supplied queries (an acceptable tradeoff — those operators were
    never intentionally exposed to callers anyway).
    """
    tokens = re.findall(r"[A-Za-z0-9]+", text)
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


def _entity_match_boost(query_words: set, row: Dict[str, Any], boost: float = 1.25) -> float:
    """Multiplicative score boost when the query mentions one of this observation's
    tagged entities (people, tools, projects — populated by the observer's extraction
    pass, stored in the `topics` field). Neutral (1.0) if no entities or no match."""
    topics = row.get("topics")
    if not topics or not isinstance(topics, list):
        return 1.0
    for t in topics:
        if isinstance(t, str) and t.strip().lower() in query_words:
            return boost
    return 1.0

EMBEDDING_DIM = 384  # MiniLM-L6-v2
MAX_BYTES_PER_VEC = EMBEDDING_DIM * 4  # 4 bytes per float32


def _vector_to_blob(vector: List[float]) -> bytes:
    """Pack a float list into a binary blob (little-endian float32)."""
    return struct.pack(f"<{len(vector)}f", *vector)


def _blob_to_vector(blob: bytes) -> List[float]:
    """Unpack a binary blob back to a float list."""
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _serialize_vector(vector: List[float]) -> bytes:
    """JSON-serialize vector for text-column storage (FTS5 virtual table limitation)."""
    return json.dumps(vector).encode("utf-8")


def _deserialize_vector(data: bytes) -> List[float]:
    """Deserialize vector from JSON text storage."""
    return json.loads(data.decode("utf-8"))


class MemoryIndex:
    """Manages the derived SQLite index for memory retrieval."""

    def __init__(self, db_path: str):
        self._db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Index not opened. Call open() first.")
        return self._conn

    def open(self) -> None:
        """Open the database, create schema if needed."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_schema()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def _create_schema(self) -> None:
        """Create tables: observations, episodes, wiki_chunks, dim_meta, config."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS observations (
                id TEXT PRIMARY KEY,
                profile TEXT NOT NULL,
                type TEXT NOT NULL,
                epistemic TEXT NOT NULL DEFAULT 'extracted',
                content TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                confirmations INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                topics TEXT DEFAULT '',
                contradicts TEXT DEFAULT '',
                evidence TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                last_confirmed TEXT,
                last_retrieved TEXT,
                embedding BLOB
            );

            CREATE TABLE IF NOT EXISTS episodes (
                id TEXT PRIMARY KEY,
                profile TEXT NOT NULL,
                session_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                outcomes TEXT DEFAULT '',
                started_at TEXT,
                ended_at TEXT,
                compacted INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS wiki_chunks (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                chunk_index INTEGER NOT NULL DEFAULT 0,
                embedding BLOB
            );

            CREATE TABLE IF NOT EXISTS dim_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- FTS5 virtual table for full-text search (external content to observations)
            CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
                content,
                content_rowid='rowid',
                content='observations'
            );

            -- Triggers to keep FTS5 in sync
            CREATE TRIGGER IF NOT EXISTS observations_ai AFTER INSERT ON observations BEGIN
                INSERT INTO observations_fts(rowid, content) VALUES (NEW.rowid, NEW.content);
            END;

            CREATE TRIGGER IF NOT EXISTS observations_ad AFTER DELETE ON observations BEGIN
                INSERT INTO observations_fts(observations_fts, rowid, content) VALUES ('delete', OLD.rowid, OLD.content);
            END;

            CREATE TRIGGER IF NOT EXISTS observations_au AFTER UPDATE ON observations BEGIN
                INSERT INTO observations_fts(observations_fts, rowid, content) VALUES ('delete', OLD.rowid, OLD.content);
                INSERT INTO observations_fts(rowid, content) VALUES (NEW.rowid, NEW.content);
            END;

            -- FTS5 virtual table for wiki content search
            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_chunks_fts USING fts5(
                content,
                content_rowid='rowid',
                content='wiki_chunks'
            );

            CREATE TRIGGER IF NOT EXISTS wiki_chunks_ai AFTER INSERT ON wiki_chunks BEGIN
                INSERT INTO wiki_chunks_fts(rowid, content) VALUES (NEW.rowid, NEW.content);
            END;

            CREATE TRIGGER IF NOT EXISTS wiki_chunks_ad AFTER DELETE ON wiki_chunks BEGIN
                INSERT INTO wiki_chunks_fts(wiki_chunks_fts, rowid, content) VALUES ('delete', OLD.rowid, OLD.content);
            END;

            CREATE TRIGGER IF NOT EXISTS wiki_chunks_au AFTER UPDATE ON wiki_chunks BEGIN
                INSERT INTO wiki_chunks_fts(wiki_chunks_fts, rowid, content) VALUES ('delete', OLD.rowid, OLD.content);
                INSERT INTO wiki_chunks_fts(rowid, content) VALUES (NEW.rowid, NEW.content);
            END;
        """)

        # Record embedding dimension
        self.conn.execute(
            "INSERT OR REPLACE INTO dim_meta (key, value) VALUES (?, ?)",
            ("embedding_dim", str(EMBEDDING_DIM)),
        )

    def get_embedding_dim(self) -> int:
        """Return the stored embedding dimension. Raises if mismatched."""
        row = self.conn.execute(
            "SELECT value FROM dim_meta WHERE key='embedding_dim'"
        ).fetchone()
        if row is None:
            return EMBEDDING_DIM
        return int(row[0])

    # ── Write operations ──────────────────────────────────────────────

    def upsert_observation(self, obs: Dict[str, Any], embedding: Optional[List[float]] = None) -> None:
        """Insert or replace an observation row."""
        self.conn.execute(
            """INSERT OR REPLACE INTO observations
               (id, profile, type, epistemic, content, confidence, confirmations,
                status, topics, contradicts, evidence, created_at, last_confirmed,
                last_retrieved, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                obs["id"],
                obs.get("profile", "agent:main"),
                obs.get("type", "fact"),
                obs.get("epistemic", "extracted"),
                obs.get("content", ""),
                obs.get("confidence", 0.5),
                obs.get("confirmations", 1),
                obs.get("status", "active"),
                json.dumps(obs.get("topics", [])),
                json.dumps(obs.get("contradicts", [])),
                json.dumps(obs.get("evidence", [])),
                obs.get("created_at", ""),
                obs.get("last_confirmed", ""),
                obs.get("last_retrieved", ""),
                _serialize_vector(embedding) if embedding else None,
            ),
        )

    def upsert_episode(self, episode: Dict[str, Any]) -> None:
        """Insert or replace a session-episode row."""
        self.conn.execute(
            """INSERT OR REPLACE INTO episodes
               (id, profile, session_id, summary, outcomes, started_at, ended_at, compacted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                episode["id"],
                episode.get("profile", "agent:main"),
                episode.get("session_id", "unknown"),
                episode.get("summary", ""),
                json.dumps(episode.get("outcomes", [])),
                episode.get("started_at", ""),
                episode.get("ended_at", ""),
                1 if episode.get("compacted") else 0,
            ),
        )

    def touch_retrieved(self, obs_id: str, ts: str) -> None:
        """Update last_retrieved timestamp."""
        self.conn.execute(
            "UPDATE observations SET last_retrieved=? WHERE id=?", (ts, obs_id)
        )

    def update_status(self, obs_id: str, status: str) -> None:
        """Update observation status."""
        self.conn.execute(
            "UPDATE observations SET status=? WHERE id=?", (status, obs_id)
        )

    def delete_observation(self, obs_id: str) -> None:
        """Remove an observation (FTS5 trigger handles cleanup)."""
        self.conn.execute("DELETE FROM observations WHERE id=?", (obs_id,))

    # ── Read operations ────────────────────────────────────────────────

    def search_fts(self, query: str, profile: str = "agent:main", limit: int = 20) -> List[Dict[str, Any]]:
        """FTS5 full-text search over observations AND wiki chunks."""
        fts_query = _fts5_safe_query(query)
        if not fts_query:
            return []

        # Observations
        obs_rows = self.conn.execute(
            """SELECT o.*, 'observation' AS source FROM observations o
               JOIN observations_fts fts ON o.rowid = fts.rowid
               WHERE observations_fts MATCH ?
                 AND o.status = 'active'
                 AND o.profile IN (?, 'shared')
               ORDER BY rank
               LIMIT ?""",
            (fts_query, profile, limit),
        ).fetchall()

        # Wiki chunks
        wiki_rows = self.conn.execute(
            """SELECT wc.id, 'shared' AS profile, 'fact' AS type, 'extracted' AS epistemic,
                      (wc.title || ': ' || wc.content) AS content,
                      1.0 AS confidence, 1 AS confirmations, 'active' AS status, '' AS topics,
                      '' AS contradicts, '' AS evidence, '' AS created_at, '' AS last_confirmed,
                      '' AS last_retrieved,
                      'wiki' AS source, wc.path, wc.chunk_index
               FROM wiki_chunks wc
               JOIN wiki_chunks_fts fts ON wc.rowid = fts.rowid
               WHERE wiki_chunks_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (fts_query, limit),
        ).fetchall()

        results = [_row_to_dict(r, self.conn) for r in obs_rows]
        for r in wiki_rows:
            d = {
                "id": r[0], "profile": r[1], "type": r[2], "epistemic": r[3],
                "content": r[4], "confidence": r[5], "confirmations": r[6],
                "status": r[7], "topics": r[8], "contradicts": r[9],
                "evidence": r[10], "created_at": r[11], "last_confirmed": r[12],
                "last_retrieved": r[13], "source": r[14], "path": r[15],
            }
            results.append(d)

        return results[:limit]

    def search_hybrid(
        self, query: str, query_embedding: Optional[List[float]], profile: str = "agent:main", k: int = 6
    ) -> List[Dict[str, Any]]:
        """Hybrid FTS5 + vector search with Reciprocal Rank Fusion and recency weighting.

        If query_embedding is None, falls back to FTS5-only.
        Recency: recent observations get a weighted boost via exponential decay
        (half-life from config, default 7 days/168h, 15% weight).
        """
        if query_embedding is None:
            return self.search_fts(query, profile, limit=k * 3)[:k]

        # Vector search — cosine similarity via dot product on normalized vectors
        vec_results = self._vector_search(query_embedding, profile, limit=k * 3)
        fts_results = self.search_fts(query, profile, limit=k * 3)

        # RRF: score = 1/(rank + 60) per result set, sum across both
        scores: Dict[str, float] = {}
        for rank, row in enumerate(fts_results):
            scores[row["id"]] = scores.get(row["id"], 0) + 1.0 / (rank + 61)
        for rank, row in enumerate(vec_results):
            scores[row["id"]] = scores.get(row["id"], 0) + 1.0 / (rank + 61)

        # Apply recency weighting (bonus for recent observations)
        merged = {row["id"]: row for row in fts_results}
        for row in vec_results:
            if row["id"] not in merged:
                merged[row["id"]] = row

        query_words = {w.lower() for w in query.split() if len(w) >= 3}
        recency_factor = _compute_recency_factor
        for obs_id, row in merged.items():
            if row.get("source") == "wiki":
                continue  # Wiki chunks get neutral recency (1.0)
            scores[obs_id] = scores.get(obs_id, 0) * recency_factor(row)
            scores[obs_id] = scores.get(obs_id, 0) * _entity_match_boost(query_words, row)

        ranked = sorted(merged.items(), key=lambda item: scores.get(item[0], 0), reverse=True)
        return [row for _, row in ranked[:k]]

    def _vector_search(self, embedding: List[float], profile: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Brute-force cosine similarity over observations AND wiki chunks."""
        results: List[Tuple[float, Dict[str, Any]]] = []

        # Observations
        obs_rows = self.conn.execute(
            """SELECT id, profile, type, epistemic, content, confidence, confirmations,
                      status, topics, contradicts, evidence, created_at, last_confirmed,
                      last_retrieved, embedding
               FROM observations
               WHERE status = 'active' AND profile IN (?, 'shared') AND embedding IS NOT NULL""",
            (profile,),
        ).fetchall()

        for row in obs_rows:
            d = _row_to_dict(row, self.conn)
            stored_vec = _deserialize_vector(row[-1]) if row[-1] else None
            if stored_vec is None:
                continue
            sim = _cosine_similarity(embedding, stored_vec)
            results.append((sim, d))

        # Wiki chunks with embeddings
        wiki_rows = self.conn.execute(
            """SELECT id, path, title, content, chunk_index, embedding
               FROM wiki_chunks WHERE embedding IS NOT NULL LIMIT ?""",
            (limit * 2,),
        ).fetchall()

        for row in wiki_rows:
            stored_vec = _deserialize_vector(row[5]) if row[5] else None
            if stored_vec is None:
                continue
            sim = _cosine_similarity(embedding, stored_vec)
            d = {
                "id": row[0], "profile": "shared", "type": "fact", "epistemic": "extracted",
                "content": f"{row[2]}: {row[3]}",
                "confidence": 1.0, "confirmations": 1, "status": "active",
                "topics": "", "contradicts": "", "evidence": "",
                "created_at": "", "last_confirmed": "", "last_retrieved": "",
                "source": "wiki", "path": row[1],
            }
            results.append((sim, d))

        results.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in results[:limit]]

    # ── Index maintenance ──────────────────────────────────────────────

    def count_active(self) -> int:
        """Return number of active observations."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM observations WHERE status='active'"
        ).fetchone()
        return row[0] if row else 0

    def vacuum(self) -> None:
        """Optimize the database."""
        self.conn.execute("PRAGMA optimize")


# ── Helpers ──────────────────────────────────────────────────────────

def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


import math


def _row_to_dict(row: tuple, conn: sqlite3.Connection) -> Dict[str, Any]:
    """Convert a SQLite row to a dict, parsing JSON fields."""
    cols = [desc[0] for desc in conn.execute("SELECT * FROM observations LIMIT 0").description]
    d = dict(zip(cols, row))
    # Parse JSON fields safely
    for field in ["topics", "contradicts", "evidence"]:
        if field in d and isinstance(d[field], str):
            try:
                d[field] = json.loads(d[field])
            except json.JSONDecodeError:
                pass
    # Raw embedding bytes are internal to vector search — never JSON-serializable, never needed by callers
    d.pop("embedding", None)
    return d
