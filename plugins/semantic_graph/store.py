"""SQLite store for the semantic-graph plugin (connection-per-operation)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger("hermes.plugins.semantic_graph")

SCHEMA_VERSION = 1

DDL_CORE = """
CREATE TABLE IF NOT EXISTS graph_runs (
    run_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    objective TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    finalized_at TEXT,
    summary_artifact_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT,
    artifact_type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    authority TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    truncated INTEGER NOT NULL DEFAULT 0,
    redaction_count INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES graph_runs(run_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_session_turn ON artifacts(session_id, turn_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_hash ON artifacts(content_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_artifact_dedupe
    ON artifacts(session_id, turn_id, artifact_type, content_hash);

CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    node_type TEXT NOT NULL,
    subtype TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL,
    normalized_label TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    identity_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    authority TEXT NOT NULL,
    confidence REAL NOT NULL,
    salience REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_type_status ON nodes(node_type, status);
CREATE INDEX IF NOT EXISTS idx_nodes_identity ON nodes(node_type, subtype, identity_key);
CREATE INDEX IF NOT EXISTS idx_nodes_label ON nodes(normalized_label);

CREATE TABLE IF NOT EXISTS run_nodes (
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, node_id),
    FOREIGN KEY(run_id) REFERENCES graph_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS edges (
    edge_id TEXT PRIMARY KEY,
    source_node_id TEXT NOT NULL,
    target_node_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    relation_label TEXT NOT NULL DEFAULT '',
    strength REAL NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(source_node_id) REFERENCES nodes(node_id) ON DELETE CASCADE,
    FOREIGN KEY(target_node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_edge_shape
    ON edges(source_node_id, target_node_id, edge_type, relation_label);

CREATE TABLE IF NOT EXISTS run_edges (
    run_id TEXT NOT NULL,
    edge_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, edge_id),
    FOREIGN KEY(run_id) REFERENCES graph_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY(edge_id) REFERENCES edges(edge_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS evidence_spans (
    evidence_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    node_id TEXT,
    edge_id TEXT,
    relation TEXT NOT NULL,
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL,
    quote TEXT NOT NULL,
    quote_hash TEXT NOT NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE,
    FOREIGN KEY(edge_id) REFERENCES edges(edge_id) ON DELETE CASCADE,
    CHECK (
        (node_id IS NOT NULL AND edge_id IS NULL)
        OR
        (node_id IS NULL AND edge_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_evidence_artifact ON evidence_spans(artifact_id);
CREATE INDEX IF NOT EXISTS idx_evidence_node ON evidence_spans(node_id);
CREATE INDEX IF NOT EXISTS idx_evidence_edge ON evidence_spans(edge_id);

CREATE TABLE IF NOT EXISTS fragments (
    fragment_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    producer_role TEXT NOT NULL,
    producer_type TEXT NOT NULL,
    producer_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'submitted',
    created_at TEXT NOT NULL,
    UNIQUE(run_id, payload_hash),
    FOREIGN KEY(run_id) REFERENCES graph_runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id TEXT PRIMARY KEY,
    run_id TEXT,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    evaluator_role TEXT NOT NULL,
    verdict TEXT NOT NULL,
    score REAL NOT NULL,
    criteria_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT NOT NULL DEFAULT '',
    suggested_revision TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES graph_runs(run_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_evaluations_target ON evaluations(target_type, target_id);

CREATE TABLE IF NOT EXISTS graph_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT,
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES graph_runs(run_id) ON DELETE SET NULL
);
"""

DDL_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    node_id UNINDEXED,
    label,
    summary,
    tokenize='unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts USING fts5(
    artifact_id UNINDEXED,
    title,
    content,
    tokenize='unicode61'
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _dumps(obj: Any) -> str:
    return json.dumps(obj if obj is not None else {}, ensure_ascii=False, separators=(",", ":"))


def new_id() -> str:
    return uuid.uuid4().hex


class SemanticGraphStore:
    """Per-operation SQLite access. Never share a connection across threads."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.fts_enabled = False
        self._ready = False
        self._lock = threading.Lock()

    def ensure_ready(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                self._migrate(conn)
            self._ready = True

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None,
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            yield conn
        finally:
            conn.close()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version < 1:
            conn.executescript(DDL_CORE)
            try:
                conn.executescript(DDL_FTS)
                self.fts_enabled = True
            except sqlite3.Error as exc:
                logger.warning("semantic-graph: FTS5 unavailable, LIKE fallback: %s", exc)
                self.fts_enabled = False
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        else:
            # Detect whether FTS tables exist.
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='nodes_fts'"
            ).fetchone()
            self.fts_enabled = row is not None

    def get_status_counts(self) -> dict[str, Any]:
        self.ensure_ready()
        with self._connect() as conn:
            def _c(table: str) -> int:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

            return {
                "schema_version": SCHEMA_VERSION,
                "fts_enabled": self.fts_enabled,
                "runs": _c("graph_runs"),
                "nodes": _c("nodes"),
                "edges": _c("edges"),
                "artifacts": _c("artifacts"),
                "fragments": _c("fragments"),
                "evaluations": _c("evaluations"),
            }

    def create_run(
        self,
        *,
        objective: str,
        scope: str = "run",
        title: str = "",
        session_id: str = "",
        turn_id: str = "",
        model: str = "",
        platform: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self.ensure_ready()
        run_id = new_id()
        now = _utcnow()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO graph_runs("
                "run_id, title, objective, scope, status, session_id, turn_id, "
                "model, platform, schema_version, created_at, metadata_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    title or "",
                    objective,
                    scope,
                    "open",
                    session_id,
                    turn_id,
                    model,
                    platform,
                    SCHEMA_VERSION,
                    now,
                    _dumps(metadata or {}),
                ),
            )
        return {"run_id": run_id, "created_at": now, "status": "open"}

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        self.ensure_ready()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM graph_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return dict(row) if row else None

    def upsert_artifact(self, artifact: dict[str, Any]) -> dict[str, Any]:
        """Insert artifact; skip if same session/turn/type/hash already exists."""
        self.ensure_ready()
        artifact_id = artifact.get("artifact_id") or new_id()
        now = artifact.get("created_at") or _utcnow()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT artifact_id FROM artifacts "
                "WHERE session_id=? AND turn_id=? AND artifact_type=? AND content_hash=?",
                (
                    artifact.get("session_id", ""),
                    artifact.get("turn_id", ""),
                    artifact.get("artifact_type", ""),
                    artifact.get("content_hash", ""),
                ),
            ).fetchone()
            if existing:
                return {
                    "artifact_id": existing["artifact_id"],
                    "duplicate": True,
                }
            conn.execute(
                "INSERT INTO artifacts("
                "artifact_id, run_id, artifact_type, title, content, content_hash, "
                "authority, session_id, turn_id, task_id, model, platform, "
                "truncated, redaction_count, metadata_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    artifact.get("run_id"),
                    artifact.get("artifact_type", "text"),
                    artifact.get("title", ""),
                    artifact.get("content", ""),
                    artifact.get("content_hash", ""),
                    artifact.get("authority", "assistant"),
                    artifact.get("session_id", ""),
                    artifact.get("turn_id", ""),
                    artifact.get("task_id", ""),
                    artifact.get("model", ""),
                    artifact.get("platform", ""),
                    1 if artifact.get("truncated") else 0,
                    int(artifact.get("redaction_count") or 0),
                    _dumps(artifact.get("metadata") or {}),
                    now,
                ),
            )
            if self.fts_enabled:
                conn.execute(
                    "INSERT INTO artifacts_fts(artifact_id, title, content) VALUES (?,?,?)",
                    (
                        artifact_id,
                        artifact.get("title", ""),
                        artifact.get("content", ""),
                    ),
                )
        return {"artifact_id": artifact_id, "duplicate": False}

    def get_artifact(self, artifact_id: str) -> Optional[dict[str, Any]]:
        self.ensure_ready()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            return dict(row) if row else None

    def upsert_node(self, node: dict[str, Any]) -> dict[str, Any]:
        self.ensure_ready()
        node_id = node["node_id"]
        now = _utcnow()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            if existing:
                # Do not unconditionally raise confidence.
                new_conf = float(node.get("confidence", existing["confidence"]))
                old_conf = float(existing["confidence"])
                conf = min(new_conf, old_conf) if new_conf > old_conf else new_conf
                # Prefer non-empty higher-information summary without blind length win.
                summary = node.get("summary") or existing["summary"]
                if existing["summary"] and node.get("summary"):
                    if len(node["summary"]) <= len(existing["summary"]) * 1.1:
                        summary = existing["summary"] if existing["summary"] else node["summary"]
                meta_old = json.loads(existing["metadata_json"] or "{}")
                meta_new = node.get("metadata") or {}
                merged_meta = dict(meta_old)
                conflicts = dict(meta_old.get("metadata_conflicts") or {})
                for k, v in meta_new.items():
                    if k in merged_meta and merged_meta[k] != v:
                        conflicts[k] = {"old": merged_meta[k], "new": v}
                    else:
                        merged_meta[k] = v
                if conflicts:
                    merged_meta["metadata_conflicts"] = conflicts
                conn.execute(
                    "UPDATE nodes SET subtype=?, label=?, normalized_label=?, summary=?, "
                    "identity_key=?, status=?, authority=?, confidence=?, salience=?, "
                    "metadata_json=?, updated_at=? WHERE node_id=?",
                    (
                        node.get("subtype", existing["subtype"]),
                        node.get("label", existing["label"]),
                        node.get("normalized_label", existing["normalized_label"]),
                        summary,
                        node.get("identity_key", existing["identity_key"]),
                        node.get("status", existing["status"]),
                        node.get("authority", existing["authority"]),
                        conf,
                        float(node.get("salience", existing["salience"])),
                        _dumps(merged_meta),
                        now,
                        node_id,
                    ),
                )
                if self.fts_enabled:
                    conn.execute("DELETE FROM nodes_fts WHERE node_id = ?", (node_id,))
                    conn.execute(
                        "INSERT INTO nodes_fts(node_id, label, summary) VALUES (?,?,?)",
                        (node_id, node.get("label", existing["label"]), summary),
                    )
                return {"node_id": node_id, "updated": True}
            conn.execute(
                "INSERT INTO nodes("
                "node_id, node_type, subtype, label, normalized_label, summary, "
                "identity_key, status, authority, confidence, salience, "
                "metadata_json, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    node_id,
                    node["node_type"],
                    node.get("subtype", ""),
                    node["label"],
                    node.get("normalized_label", ""),
                    node.get("summary", ""),
                    node.get("identity_key", ""),
                    node.get("status", "candidate"),
                    node.get("authority", "assistant"),
                    float(node.get("confidence", 0.5)),
                    float(node.get("salience", 0.5)),
                    _dumps(node.get("metadata") or {}),
                    now,
                    now,
                ),
            )
            if self.fts_enabled:
                conn.execute(
                    "INSERT INTO nodes_fts(node_id, label, summary) VALUES (?,?,?)",
                    (node_id, node["label"], node.get("summary", "")),
                )
        return {"node_id": node_id, "updated": False}

    def upsert_edge(self, edge: dict[str, Any]) -> dict[str, Any]:
        self.ensure_ready()
        edge_id = edge["edge_id"]
        now = _utcnow()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT edge_id FROM edges WHERE source_node_id=? AND target_node_id=? "
                "AND edge_type=? AND relation_label=?",
                (
                    edge["source_node_id"],
                    edge["target_node_id"],
                    edge["edge_type"],
                    edge.get("relation_label", ""),
                ),
            ).fetchone()
            if existing:
                return {"edge_id": existing["edge_id"], "duplicate": True}
            conn.execute(
                "INSERT INTO edges("
                "edge_id, source_node_id, target_node_id, edge_type, relation_label, "
                "strength, confidence, status, rationale, metadata_json, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    edge_id,
                    edge["source_node_id"],
                    edge["target_node_id"],
                    edge["edge_type"],
                    edge.get("relation_label", ""),
                    float(edge.get("strength", 0.5)),
                    float(edge.get("confidence", 0.5)),
                    edge.get("status", "candidate"),
                    edge.get("rationale", ""),
                    _dumps(edge.get("metadata") or {}),
                    now,
                    now,
                ),
            )
        return {"edge_id": edge_id, "duplicate": False}

    def link_run_node(self, run_id: str, node_id: str, role: str = "member") -> None:
        self.ensure_ready()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO run_nodes(run_id, node_id, role, created_at) VALUES (?,?,?,?)",
                (run_id, node_id, role, _utcnow()),
            )

    def link_run_edge(self, run_id: str, edge_id: str) -> None:
        self.ensure_ready()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO run_edges(run_id, edge_id, created_at) VALUES (?,?,?)",
                (run_id, edge_id, _utcnow()),
            )

    def insert_evidence(self, evidence: dict[str, Any]) -> str:
        self.ensure_ready()
        eid = evidence.get("evidence_id") or new_id()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO evidence_spans("
                "evidence_id, artifact_id, node_id, edge_id, relation, start_char, "
                "end_char, quote, quote_hash, confidence, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    eid,
                    evidence["artifact_id"],
                    evidence.get("node_id"),
                    evidence.get("edge_id"),
                    evidence.get("relation", "supports"),
                    int(evidence["start_char"]),
                    int(evidence["end_char"]),
                    evidence.get("quote", ""),
                    evidence.get("quote_hash", ""),
                    float(evidence.get("confidence", 0.5)),
                    _utcnow(),
                ),
            )
        return eid

    def insert_fragment(self, fragment: dict[str, Any]) -> dict[str, Any]:
        self.ensure_ready()
        fid = fragment.get("fragment_id") or new_id()
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO fragments("
                    "fragment_id, run_id, producer_role, producer_type, producer_id, "
                    "model, payload_json, payload_hash, status, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        fid,
                        fragment["run_id"],
                        fragment.get("producer_role", "unknown"),
                        fragment.get("producer_type", "subagent"),
                        fragment.get("producer_id", ""),
                        fragment.get("model", ""),
                        fragment.get("payload_json", "{}"),
                        fragment["payload_hash"],
                        fragment.get("status", "submitted"),
                        _utcnow(),
                    ),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT fragment_id FROM fragments WHERE run_id=? AND payload_hash=?",
                    (fragment["run_id"], fragment["payload_hash"]),
                ).fetchone()
                if row is None:
                    # Not a duplicate-payload hit (e.g. foreign-key violation).
                    raise
                return {
                    "success": True,
                    "duplicate": True,
                    "fragment_id": row["fragment_id"],
                }
        return {"success": True, "duplicate": False, "fragment_id": fid}

    def insert_evaluation(self, evaluation: dict[str, Any]) -> str:
        self.ensure_ready()
        eid = evaluation.get("evaluation_id") or new_id()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO evaluations("
                "evaluation_id, run_id, target_type, target_id, evaluator_role, "
                "verdict, score, criteria_json, notes, suggested_revision, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    eid,
                    evaluation.get("run_id"),
                    evaluation["target_type"],
                    evaluation["target_id"],
                    evaluation.get("evaluator_role", "assistant"),
                    evaluation["verdict"],
                    float(evaluation.get("score", 0.0)),
                    _dumps(evaluation.get("criteria") or {}),
                    evaluation.get("notes", ""),
                    evaluation.get("suggested_revision", ""),
                    _utcnow(),
                ),
            )
        return eid

    def insert_event(self, event: dict[str, Any]) -> str:
        self.ensure_ready()
        eid = event.get("event_id") or new_id()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO graph_events("
                "event_id, run_id, event_type, actor_type, actor_id, session_id, "
                "turn_id, task_id, payload_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    eid,
                    event.get("run_id"),
                    event["event_type"],
                    event.get("actor_type", "system"),
                    event.get("actor_id", ""),
                    event.get("session_id", ""),
                    event.get("turn_id", ""),
                    event.get("task_id", ""),
                    _dumps(event.get("payload") or {}),
                    _utcnow(),
                ),
            )
        return eid

    def get_node(self, node_id: str) -> Optional[dict[str, Any]]:
        self.ensure_ready()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            return dict(row) if row else None

    def get_edge(self, edge_id: str) -> Optional[dict[str, Any]]:
        self.ensure_ready()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM edges WHERE edge_id=?", (edge_id,)).fetchone()
            return dict(row) if row else None

    def get_fragment(self, fragment_id: str) -> Optional[dict[str, Any]]:
        self.ensure_ready()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM fragments WHERE fragment_id=?", (fragment_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_evaluation(self, evaluation_id: str) -> Optional[dict[str, Any]]:
        self.ensure_ready()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluations WHERE evaluation_id=?", (evaluation_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_nodes(
        self,
        *,
        statuses: Optional[list[str]] = None,
        node_types: Optional[list[str]] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.ensure_ready()
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if node_types:
            clauses.append(f"node_type IN ({','.join('?' for _ in node_types)})")
            params.extend(node_types)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM nodes{where} ORDER BY updated_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_edges(self, *, include_rejected: bool = False, limit: int = 500) -> list[dict[str, Any]]:
        self.ensure_ready()
        with self._connect() as conn:
            if include_rejected:
                rows = conn.execute(
                    "SELECT * FROM edges ORDER BY updated_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM edges WHERE status NOT IN ('rejected','superseded') "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def list_artifacts(self, *, run_id: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        self.ensure_ready()
        with self._connect() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at DESC LIMIT ?",
                    (run_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM artifacts ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    def list_evidence_for_node(self, node_id: str) -> list[dict[str, Any]]:
        self.ensure_ready()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evidence_spans WHERE node_id=?", (node_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def neighbors(self, node_id: str, max_neighbors: int = 20) -> list[dict[str, Any]]:
        self.ensure_ready()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM edges WHERE source_node_id=? OR target_node_id=? LIMIT ?",
                (node_id, node_id, max_neighbors),
            ).fetchall()
            return [dict(r) for r in rows]

    def search_nodes(
        self,
        query: str,
        *,
        statuses: Optional[list[str]] = None,
        node_types: Optional[list[str]] = None,
        top_k: int = 8,
        min_confidence: float = 0.0,
    ) -> list[dict[str, Any]]:
        self.ensure_ready()
        statuses = statuses or ["asserted", "accepted"]
        q = (query or "").strip()
        if not q:
            return []
        with self._connect() as conn:
            if self.fts_enabled:
                try:
                    # Escape FTS special chars lightly.
                    fts_q = '"' + q.replace('"', " ") + '"'
                    rows = conn.execute(
                        "SELECT n.*, bm25(nodes_fts) AS bm25_score "
                        "FROM nodes_fts "
                        "JOIN nodes n ON n.node_id = nodes_fts.node_id "
                        f"WHERE nodes_fts MATCH ? AND n.status IN ({','.join('?' for _ in statuses)}) "
                        "AND n.confidence >= ? "
                        + (
                            f"AND n.node_type IN ({','.join('?' for _ in node_types)}) "
                            if node_types
                            else ""
                        )
                        + "ORDER BY bm25(nodes_fts) LIMIT ?",
                        (
                            fts_q,
                            *statuses,
                            min_confidence,
                            *(node_types or []),
                            top_k * 3,
                        ),
                    ).fetchall()
                    return [dict(r) for r in rows]
                except sqlite3.Error:
                    pass
            like = f"%{q}%"
            rows = conn.execute(
                "SELECT *, 1.0 AS bm25_score FROM nodes "
                f"WHERE (label LIKE ? OR summary LIKE ? OR identity_key LIKE ? OR subtype LIKE ?) "
                f"AND status IN ({','.join('?' for _ in statuses)}) AND confidence >= ? "
                + (
                    f"AND node_type IN ({','.join('?' for _ in node_types)}) "
                    if node_types
                    else ""
                )
                + "ORDER BY confidence DESC, salience DESC LIMIT ?",
                (
                    like,
                    like,
                    like,
                    like,
                    *statuses,
                    min_confidence,
                    *(node_types or []),
                    top_k * 3,
                ),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_node_status(self, node_id: str, status: str) -> bool:
        self.ensure_ready()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE nodes SET status=?, updated_at=? WHERE node_id=?",
                (status, _utcnow(), node_id),
            )
            return cur.rowcount > 0

    def update_edge_status(self, edge_id: str, status: str) -> bool:
        self.ensure_ready()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE edges SET status=?, updated_at=? WHERE edge_id=?",
                (status, _utcnow(), edge_id),
            )
            return cur.rowcount > 0

    def finalize_run(
        self,
        run_id: str,
        *,
        status: str = "finalized",
        summary_artifact_id: Optional[str] = None,
    ) -> None:
        self.ensure_ready()
        with self._connect() as conn:
            conn.execute(
                "UPDATE graph_runs SET status=?, finalized_at=?, summary_artifact_id=? "
                "WHERE run_id=?",
                (status, _utcnow(), summary_artifact_id, run_id),
            )

    def list_fragments_for_run(self, run_id: str) -> list[dict[str, Any]]:
        self.ensure_ready()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM fragments WHERE run_id=? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def purge_before(self, before_iso_date: str) -> dict[str, int]:
        """Delete rejected/superseded nodes and old artifacts before cutoff date."""
        self.ensure_ready()
        cutoff = before_iso_date.strip()
        if len(cutoff) == 10:
            cutoff = cutoff + "T00:00:00+00:00"
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                a = conn.execute(
                    "DELETE FROM artifacts WHERE created_at < ?", (cutoff,)
                ).rowcount
                n = conn.execute(
                    "DELETE FROM nodes WHERE status IN ('rejected','superseded') "
                    "AND updated_at < ?",
                    (cutoff,),
                ).rowcount
                e = conn.execute(
                    "DELETE FROM edges WHERE status IN ('rejected','superseded') "
                    "AND updated_at < ?",
                    (cutoff,),
                ).rowcount
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {"artifacts": a, "nodes": n, "edges": e}

    def vacuum(self) -> None:
        self.ensure_ready()
        with self._connect() as conn:
            conn.execute("VACUUM")
