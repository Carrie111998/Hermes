#!/usr/bin/env python3
"""
Session Embedder — Semantic Search for Hermes Sessions

Reuses the local bge-small-zh-v1.5 ONNX model (from chroma_memory plugin)
to compute embeddings for session titles + first messages, then provides
semantic (cosine similarity) search on top.

Maintains its own SQLite index at ~/.hermes/session_embeddings.db so it
doesn't interfere with the main session DB or ChromaDB.

Usage:
    from session_embedder import SessionEmbeddingIndex

    idx = SessionEmbeddingIndex()
    idx.index_session(session_id="abc123", title="部署方案讨论", first_message="我们该用K8s还是Swarm")
    results = idx.search_semantic(query="容器编排", limit=5)
"""

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────
# Model location is configurable via HERMES_EMBEDDER_MODEL_DIR; by default we
# reuse the BGE checkpoint shipped with the chroma_memory plugin (if present),
# so users without the plugin can point at their own bge-small-zh ONNX export.
def _hermes_home() -> Path:
    """Resolve the active profile's hermes home (mirrors session_search_tool)."""
    try:
        from hermes_cli import profiles as profiles_mod
        return profiles_mod.get_profile_dir(profiles_mod.get_active_profile())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))

_MODEL_DIR = Path(os.environ.get(
    "HERMES_EMBEDDER_MODEL_DIR",
    str(_hermes_home() / "plugins" / "chroma_memory"),
))
_MODEL_ONNX = str(_MODEL_DIR / "model_fp16.onnx")
_TOKENIZER_DIR = _MODEL_DIR / "tokenizer"

# Our own index DB (separate from state.db and chroma_db)
_INDEX_DB = Path(os.environ.get(
    "HERMES_SESSION_EMBEDDINGS_DB",
    str(_hermes_home() / "session_embeddings.db"),
))

# bge-small-zh-v1.5 outputs 512-dim vectors
_EMBED_DIM = 512


# ═══════════════════════════════════════════════════════════════════════════
# BGE ONNX Embedding Engine
# ═══════════════════════════════════════════════════════════════════════════

class BGEEmbeddingEngine:
    """Lightweight wrapper around the bge-small-zh ONNX model.

    Thread-safe (locks on model load). Uses the exact same model files as
    the chroma_memory plugin, loaded fresh so we don't depend on plugin state.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._session = None
                    obj._tokenizer = None
                    obj._input_name = None
                    obj._output_name = None
                    obj._init_lock = threading.Lock()
                    cls._instance = obj
        return cls._instance

    def _ensure_loaded(self):
        if self._session is not None:
            return
        with self._init_lock:
            if self._session is not None:
                return

        import onnxruntime

        # Validate the model exists
        if not Path(_MODEL_ONNX).exists():
            raise FileNotFoundError(
                f"BGE model not found at {_MODEL_ONNX}. "
                "Is chroma_memory plugin installed?"
            )

        # ONNX session
        opts = onnxruntime.SessionOptions()
        opts.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.intra_op_num_threads = 2
        self._session = onnxruntime.InferenceSession(
            _MODEL_ONNX,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )

        self._input_name = self._session.get_inputs()[0].name
        # sentence_embedding output (index 1) — already mean-pooled + normalized
        self._output_name = self._session.get_outputs()[1].name

        # Load tokenizer
        self._load_tokenizer()

    def _load_tokenizer(self):
        """Load HuggingFace tokenizer from the plugin's tokenizer directory."""
        try:
            from transformers import AutoTokenizer

            if _TOKENIZER_DIR.exists() and (_TOKENIZER_DIR / "tokenizer.json").exists():
                self._tokenizer = AutoTokenizer.from_pretrained(
                    str(_TOKENIZER_DIR),
                    use_fast=True,
                    local_files_only=True,
                )
            else:
                self._tokenizer = AutoTokenizer.from_pretrained(
                    "onnx-community/bge-small-zh-v1.5",
                    use_fast=True,
                )
        except Exception as e:
            logger.warning("Failed to load tokenizer: %s", e)
            self._tokenizer = None

    def embed(self, texts: List[str]) -> np.ndarray:
        """Compute embeddings for a list of texts.

        Returns a (N, 512) float32 numpy array.
        """
        self._ensure_loaded()

        if self._tokenizer is None:
            raise RuntimeError("Tokenizer not loaded — cannot embed")

        tokens = self._tokenizer(
            list(texts),
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="np",
        )

        outputs = self._session.run(
            [self._output_name],
            {
                self._input_name: tokens["input_ids"],
                "attention_mask": tokens["attention_mask"],
                "token_type_ids": tokens.get("token_type_ids",
                                              np.zeros_like(tokens["input_ids"])),
            },
        )

        return np.array(outputs[0], dtype=np.float32)

    def embed_one(self, text: str) -> np.ndarray:
        """Embed a single text, returns (512,) float32 array."""
        return self.embed([text])[0]


# ═══════════════════════════════════════════════════════════════════════════
# SQLite-backed Session Embedding Index
# ═══════════════════════════════════════════════════════════════════════════

class SessionEmbeddingIndex:
    """Manages a SQLite index of session embeddings for semantic search.

    Schema:
        session_id TEXT PRIMARY KEY  — matches state.db sessions.id
        title      TEXT              — session title
        preview    TEXT              — first user message (for matching context)
        embedding  BLOB              — float32[512] packed as binary
        created_at REAL              — when this index entry was created
        updated_at REAL              — when this entry was last updated
    """

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = str(db_path or _INDEX_DB)
        self._engine = BGEEmbeddingEngine()
        self._init_db()

    def _init_db(self):
        """Create the schema if it doesn't exist."""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_embeddings (
                    session_id TEXT PRIMARY KEY,
                    title TEXT DEFAULT '',
                    preview TEXT DEFAULT '',
                    embedding BLOB NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.commit()

    def _connect(self):
        """Open a connection (autocommit off for batch operations)."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── Public API ──────────────────────────────────────────────────────

    def index_session(self, session_id: str, title: str = "",
                      preview: str = "") -> bool:
        """Compute and store embedding for a single session.

        The ``preview`` should be the first user message — gives the
        embedding meaningful semantic content.

        Returns True if indexed, False if title+preview is empty.
        """
        text = _build_text(title, preview)
        if not text.strip():
            return False

        try:
            vec = self._engine.embed_one(text)
            blob = vec.tobytes()
            now = time.time()

            with self._connect() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO session_embeddings
                        (session_id, title, preview, embedding, created_at, updated_at)
                    VALUES (?, ?, ?, ?, COALESCE(
                        (SELECT created_at FROM session_embeddings WHERE session_id = ?),
                        ?
                    ), ?)
                """, (session_id, title, preview, blob, session_id, now, now))
                conn.commit()
            return True

        except Exception as e:
            logger.error("Failed to index session %s: %s", session_id, e)
            return False

    def remove_session(self, session_id: str):
        """Delete a session's embedding from the index."""
        with self._connect() as conn:
            conn.execute("DELETE FROM session_embeddings WHERE session_id = ?",
                         (session_id,))
            conn.commit()

    def get_embedding(self, session_id: str) -> Optional[np.ndarray]:
        """Retrieve the stored embedding for a session, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT embedding FROM session_embeddings WHERE session_id = ?",
                (session_id,)
            ).fetchone()
        if row is None:
            return None
        return np.frombuffer(row["embedding"], dtype=np.float32)

    def has_index(self, session_id: str) -> bool:
        """Check if a session is already indexed."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM session_embeddings WHERE session_id = ?",
                (session_id,)
            ).fetchone()
        return row is not None

    def count(self) -> int:
        """Total indexed sessions."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM session_embeddings").fetchone()
        return row["c"] if row else 0

    def search_semantic(self, query: str, top_k: int = 10,
                        exclude_ids: Optional[List[str]] = None) -> List[Dict]:
        """Search indexed sessions by semantic similarity to ``query``.

        Returns list of dicts, sorted by similarity DESC, each:
            session_id, title, preview, score (cosine sim 0-1)
        """
        if exclude_ids is None:
            exclude_ids = []

        # Compute query embedding
        try:
            qvec = self._engine.embed_one(query)
        except Exception as e:
            logger.error("Semantic search embedding failed: %s", e)
            return []

        # Load all embeddings from DB
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT session_id, title, preview, embedding
                FROM session_embeddings
            """).fetchall()

        if not rows:
            return []

        # Compute cosine similarity in numpy (vectorised)
        session_ids = []
        titles = []
        previews = []
        vecs = []

        for r in rows:
            sid = r["session_id"]
            if sid in exclude_ids:
                continue
            session_ids.append(sid)
            titles.append(r["title"] or "")
            previews.append(r["preview"] or "")
            vecs.append(np.frombuffer(r["embedding"], dtype=np.float32))

        if not vecs:
            return []

        # Stack into (N, 512) matrix
        mat = np.stack(vecs, axis=0)  # (N, dim)

        # Cosine similarity (vectors are already normalized by BGE, but be safe)
        q_norm = qvec / (np.linalg.norm(qvec) + 1e-12)
        mat_norm = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12)
        scores = np.dot(mat_norm, q_norm)  # (N,)

        # Top-k
        top_indices = np.argsort(-scores)[:top_k]

        results = []
        for idx_idx in top_indices:
            i = int(idx_idx)
            results.append({
                "session_id": session_ids[i],
                "title": titles[i],
                "preview": previews[i],
                "score": round(float(scores[i]), 4),
            })

        return results

    # ── Batch Indexing ──────────────────────────────────────────────────

    def batch_index_existing(self, max_sessions: int = 500) -> Tuple[int, int]:
        """Scan the main session DB and index any sessions that are missing.

        Uses the state.db to fetch session titles and first user messages.
        Returns (total_scanned, total_newly_indexed).
        """
        from hermes_state import SessionDB

        db = SessionDB()
        sessions = db.list_sessions_rich(limit=max_sessions, offset=0)

        indexed = 0
        skipped = 0
        for s in sessions:
            sid = s.get("id", "")
            if not sid:
                continue
            if self.has_index(sid):
                skipped += 1
                continue

            title = s.get("title") or ""
            preview = self._get_first_message(db, sid)

            if self.index_session(sid, title=title, preview=preview):
                indexed += 1
            else:
                skipped += 1

        db.close()
        return len(sessions), indexed

    def _get_first_message(self, db, session_id: str, max_chars: int = 500) -> str:
        """Fetch the first user message from a session for embedding context."""
        try:
            messages = db.get_messages(session_id, limit=5)
            for m in messages:
                if m.get("role") == "user":
                    content = (m.get("content") or "").strip()
                    if content:
                        return content[:max_chars]
        except Exception:
            pass
        return ""

    def delete_all(self):
        """Wipe the entire index (for rebuilds)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM session_embeddings")
            conn.commit()


# ── Helpers ──────────────────────────────────────────────────────────────

def _build_text(title: str, preview: str) -> str:
    """Build the text to embed from title + preview."""
    parts = [t for t in [title.strip(), preview.strip()] if t]
    if not parts:
        return ""
    return " | ".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════════════════

def main():
    """CLI for managing the session embedding index.

    Usage:
        python session_embedder.py index       # Index all existing sessions
        python session_embedder.py search <q>  # Test semantic search
        python session_embedder.py status      # Show index stats
        python session_embedder.py rebuild     # Wipe and re-index all
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s: %(message)s")

    idx = SessionEmbeddingIndex()

    if len(sys.argv) < 2:
        print("Usage: session_embedder.py <index|search|status|rebuild> [...]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "index":
        logger.info("Indexing existing sessions...")
        total, new = idx.batch_index_existing()
        logger.info("Done: %d scanned, %d newly indexed, %d total in index",
                     total, new, idx.count())

    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: session_embedder.py search '<query>'")
            sys.exit(1)
        query = sys.argv[2]
        results = idx.search_semantic(query, top_k=10)
        if not results:
            print("No matches found.")
        else:
            print(f"Top {len(results)} semantic matches for: {query!r}")
            print("─" * 60)
            for r in results:
                title = r["title"] or "(no title)"
                preview = r["preview"][:80] if r["preview"] else "(no preview)"
                print(f"  [{r['score']:.3f}] {title}")
                print(f"         {preview}")
                print(f"         session: {r['session_id']}")
                print()

    elif cmd == "status":
        print(f"Index DB:      {_INDEX_DB}")
        print(f"Model:         {_MODEL_ONNX}")
        print(f"Indexed:       {idx.count()} sessions")
        with idx._connect() as conn:
            row = conn.execute("SELECT MIN(created_at) AS oldest, MAX(updated_at) AS newest FROM session_embeddings").fetchone()
        if row and row["oldest"]:
            print(f"Oldest entry:  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['oldest']))}")
            print(f"Newest entry:  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['newest']))}")
        else:
            print("Oldest entry:  (empty)")
            print("Newest entry:  (empty)")

    elif cmd == "rebuild":
        logger.info("Wiping index and re-indexing...")
        idx.delete_all()
        total, new = idx.batch_index_existing()
        logger.info("Rebuild complete: %d scanned, %d indexed, %d total",
                     total, new, idx.count())

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
