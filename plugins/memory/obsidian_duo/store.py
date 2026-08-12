"""SQLite persistence for Memory Duo's durable and derived state."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .contracts import (
    Authority,
    EvidenceRecord,
    MemoryCandidate,
    MemoryRecord,
    MemoryStatus,
    Verification,
)
from .security import assert_candidate_safe_to_persist, assert_safe_to_persist, assert_safe_value, redact_value


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class SearchHit:
    memory_id: str
    title: str
    body: str
    rank: float


class SqliteMemoryStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "connection", None)
        if conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            self._local.connection = conn
            if not self._initialized:
                self.initialize()
        return conn

    def initialize(self) -> None:
        with self._init_lock:
            conn = getattr(self._local, "connection", None)
            if conn is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                conn = self._connect()
                self._local.connection = conn
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    memory_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    status TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    verification TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    importance REAL NOT NULL,
                    evidence_ids TEXT NOT NULL,
                    relationships TEXT NOT NULL,
                    source_session_id TEXT NOT NULL DEFAULT '',
                    task_id TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    child_session_id TEXT NOT NULL DEFAULT '',
                    mission_id TEXT NOT NULL DEFAULT '',
                    agent_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS memory_versions (
                    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    version_reason TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(memory_id) REFERENCES memories(memory_id)
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    session_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_evidence (
                    memory_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    PRIMARY KEY(memory_id, evidence_id),
                    FOREIGN KEY(memory_id) REFERENCES memories(memory_id),
                    FOREIGN KEY(evidence_id) REFERENCES evidence(evidence_id)
                );
                CREATE TABLE IF NOT EXISTS relationships (
                    relationship_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relationship TEXT NOT NULL,
                    metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conflicts (
                    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL,
                    conflicting_memory_id TEXT,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'disputed',
                    FOREIGN KEY(memory_id) REFERENCES memories(memory_id)
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'staged',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS note_index (
                    path TEXT PRIMARY KEY,
                    memory_id TEXT,
                    mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    parse_status TEXT NOT NULL DEFAULT 'indexed',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS external_index (
                    path TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS external_catalog (
                    path TEXT PRIMARY KEY,
                    mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    content_hash TEXT NOT NULL DEFAULT '',
                    memory_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS journal (
                    txn_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS metrics (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    memory_id UNINDEXED, title, body, tags, entities
                );
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
            for name in (
                "source_session_id", "task_id", "project_id", "child_session_id",
                "mission_id", "agent_id",
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            if "created_at" not in columns:
                conn.execute("ALTER TABLE memories ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                (str(self.SCHEMA_VERSION),),
            )
            conn.commit()
            self._initialized = True

    def _serialize_memory(self, record: MemoryRecord) -> tuple:
        return (
            record.memory_id,
            record.content,
            record.memory_type,
            record.scope,
            record.status.value,
            record.authority.value,
            record.verification.value,
            record.confidence,
            record.importance,
            json.dumps(record.evidence_ids),
            json.dumps(record.relationships),
            record.source_session_id,
            record.task_id,
            record.project_id,
            record.child_session_id,
            record.mission_id,
            record.agent_id,
            record.created_at,
        )

    def upsert_memory(self, record: MemoryRecord, version_reason: str) -> None:
        assert_safe_to_persist(record.content)
        assert_safe_value((
            record.memory_id, record.memory_type, record.scope,
            record.authority.value, record.verification.value,
            record.evidence_ids, record.relationships,
            record.source_session_id, record.task_id, record.project_id,
            record.child_session_id, record.mission_id, record.agent_id,
        ))
        conn = self.connection()
        with conn:
            old = conn.execute("SELECT * FROM memories WHERE memory_id=?", (record.memory_id,)).fetchone()
            if old is not None:
                conn.execute(
                    "INSERT INTO memory_versions(memory_id, content, payload, version_reason) VALUES(?,?,?,?)",
                    (record.memory_id, redact_value(old["content"]), json.dumps(redact_value(dict(old))), version_reason),
                )
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (record.memory_id,))
            conn.execute(
                """INSERT INTO memories(
                    memory_id, content, memory_type, scope, status, authority,
                    verification, confidence, importance, evidence_ids, relationships,
                    source_session_id, task_id, project_id, child_session_id, mission_id,
                    agent_id, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    content=excluded.content, memory_type=excluded.memory_type,
                    scope=excluded.scope, status=excluded.status,
                    authority=excluded.authority, verification=excluded.verification,
                    confidence=excluded.confidence, importance=excluded.importance,
                    evidence_ids=excluded.evidence_ids, relationships=excluded.relationships,
                    source_session_id=excluded.source_session_id, task_id=excluded.task_id,
                    project_id=excluded.project_id, child_session_id=excluded.child_session_id,
                    mission_id=excluded.mission_id, agent_id=excluded.agent_id,
                    updated_at=CURRENT_TIMESTAMP""",
                self._serialize_memory(record),
            )
            conn.execute(
                "INSERT INTO memory_fts(memory_id,title,body,tags,entities) VALUES(?,?,?,?,?)",
                (record.memory_id, record.memory_type, record.content, record.scope, ""),
            )

    def get_memory(self, memory_id: str) -> Optional[MemoryRecord]:
        row = self.connection().execute(
            "SELECT * FROM memories WHERE memory_id=?", (memory_id,)
        ).fetchone()
        if row is None:
            return None
        return MemoryRecord(
            memory_id=row["memory_id"], content=row["content"],
            memory_type=row["memory_type"], scope=row["scope"],
            status=MemoryStatus(row["status"]), authority=Authority(row["authority"]),
            verification=Verification(row["verification"]), confidence=row["confidence"],
            importance=row["importance"], evidence_ids=tuple(json.loads(row["evidence_ids"])),
            relationships=tuple(json.loads(row["relationships"])),
            source_session_id=row["source_session_id"], task_id=row["task_id"],
            project_id=row["project_id"], child_session_id=row["child_session_id"],
            mission_id=row["mission_id"], agent_id=row["agent_id"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def insert_evidence(self, record: EvidenceRecord) -> None:
        assert_safe_to_persist(record.content)
        assert_safe_value((record.evidence_id, record.kind, record.source, record.session_id))
        with self.connection():
            self.connection().execute(
                "INSERT OR REPLACE INTO evidence(evidence_id,kind,content,source,session_id) VALUES(?,?,?,?,?)",
                (record.evidence_id, record.kind, record.content, record.source, record.session_id),
            )

    def link_evidence(self, memory_id: str, evidence_id: str) -> None:
        with self.connection():
            self.connection().execute(
                "INSERT OR IGNORE INTO memory_evidence(memory_id,evidence_id) VALUES(?,?)",
                (memory_id, evidence_id),
            )

    def search_fts(self, query: str, limit: int = 12) -> list[SearchHit]:
        rows = self.connection().execute(
            "SELECT memory_id,title,body,bm25(memory_fts) AS rank FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
        return [SearchHit(row["memory_id"], row["title"], row["body"], row["rank"]) for row in rows]

    def record_relationship(self, source_id: str, target_id: str, relationship: str, metadata: Optional[dict] = None) -> None:
        with self.connection():
            self.connection().execute(
                "INSERT INTO relationships(source_id,target_id,relationship,metadata) VALUES(?,?,?,?)",
                (source_id, target_id, relationship, json.dumps(metadata or {})),
            )

    def record_conflict(self, memory_id: str, conflicting_memory_id: Optional[str], reason: str) -> None:
        with self.connection():
            self.connection().execute(
                "INSERT INTO conflicts(memory_id,conflicting_memory_id,reason) VALUES(?,?,?)",
                (memory_id, conflicting_memory_id, reason),
            )

    def stage_candidate(self, candidate: MemoryCandidate) -> str:
        assert_candidate_safe_to_persist(candidate)
        candidate_id = new_id("candidate")
        payload = {
            "content": candidate.content,
            "memory_type": candidate.memory_type,
            "scope": candidate.scope,
            "authority": candidate.authority.value,
            "verification": candidate.verification.value,
            "evidence_ids": [item.evidence_id for item in candidate.evidence],
            "metadata": dict(candidate.metadata),
        }
        with self.connection():
            self.connection().execute(
                "INSERT INTO candidates(candidate_id,payload) VALUES(?,?)",
                (candidate_id, json.dumps(payload)),
            )
        return candidate_id

    def record_journal(self, txn_id: str, operation: str, state: str, payload: dict) -> None:
        assert_safe_value(payload)
        with self.connection():
            self.connection().execute(
                "INSERT OR REPLACE INTO journal(txn_id,operation,state,payload,updated_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)",
                (txn_id, operation, state, json.dumps(payload)),
            )

    def set_note_index(self, path: str, memory_id: Optional[str], mtime_ns: int, size: int, content_hash: str, parse_status: str = "indexed") -> None:
        with self.connection():
            self.connection().execute(
                """INSERT INTO note_index(path,memory_id,mtime_ns,size,content_hash,parse_status)
                VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET memory_id=excluded.memory_id,
                mtime_ns=excluded.mtime_ns,size=excluded.size,content_hash=excluded.content_hash,
                parse_status=excluded.parse_status,updated_at=CURRENT_TIMESTAMP""",
                (path, memory_id, mtime_ns, size, content_hash, parse_status),
            )

    def set_external_index(self, path: str, memory_id: str, mtime_ns: int, size: int, content_hash: str) -> None:
        with self.connection():
            self.connection().execute(
                """INSERT INTO external_index(path,memory_id,mtime_ns,size,content_hash)
                VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET memory_id=excluded.memory_id,
                mtime_ns=excluded.mtime_ns,size=excluded.size,content_hash=excluded.content_hash,
                updated_at=CURRENT_TIMESTAMP""",
                (path, memory_id, mtime_ns, size, content_hash),
            )

    def external_catalog_rows(self) -> list[sqlite3.Row]:
        return self.connection().execute(
            "SELECT path,mtime_ns,size,content_hash,memory_id,status FROM external_catalog ORDER BY path"
        ).fetchall()

    def upsert_external_catalog(self, path: str, mtime_ns: int, size: int, *, status: str = "pending", memory_id: str = "", content_hash: str = "") -> None:
        with self.connection():
            self.connection().execute(
                """INSERT INTO external_catalog(path,mtime_ns,size,content_hash,memory_id,status)
                VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                mtime_ns=excluded.mtime_ns,size=excluded.size,content_hash=excluded.content_hash,
                memory_id=CASE WHEN excluded.memory_id != '' THEN excluded.memory_id ELSE external_catalog.memory_id END,
                status=excluded.status,updated_at=CURRENT_TIMESTAMP""",
                (path, mtime_ns, size, content_hash, memory_id, status),
            )

    def delete_external_catalog(self, path: str) -> None:
        row = self.connection().execute(
            "SELECT memory_id FROM external_catalog WHERE path=?", (path,)
        ).fetchone()
        with self.connection():
            self.connection().execute("DELETE FROM external_catalog WHERE path=?", (path,))
            self.connection().execute("DELETE FROM external_index WHERE path=?", (path,))
            if row and row["memory_id"]:
                self.connection().execute("DELETE FROM memory_fts WHERE memory_id=?", (row["memory_id"],))
                self.connection().execute("DELETE FROM memories WHERE memory_id=?", (row["memory_id"],))

    def set_external_catalog_indexed(self, path: str, memory_id: str, mtime_ns: int, size: int, content_hash: str) -> None:
        self.upsert_external_catalog(
            path, mtime_ns, size, status="indexed", memory_id=memory_id, content_hash=content_hash,
        )

    def get_schema_value(self, key: str, default: str = "") -> str:
        row = self.connection().execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row is not None else default

    def set_schema_value(self, key: str, value: str) -> None:
        with self.connection():
            self.connection().execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES(?,?)", (key, value)
            )

    def metrics_increment(self, name: str, value: int = 1) -> None:
        with self.connection():
            self.connection().execute(
                "INSERT INTO metrics(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value",
                (name, value),
            )

    def rebuild_fts(self) -> None:
        conn = self.connection()
        with conn:
            conn.execute("DELETE FROM memory_fts")
            conn.execute(
                "INSERT INTO memory_fts(memory_id,title,body,tags,entities) SELECT memory_id,memory_type,content,scope,'' FROM memories"
            )

    def hot_memory_candidates(self, limit: int = 12) -> list[MemoryRecord]:
        rows = self.connection().execute(
            "SELECT memory_id FROM memories WHERE status='active' ORDER BY importance DESC, confidence DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [record for row in rows if (record := self.get_memory(row["memory_id"])) is not None]

    def close(self) -> None:
        conn = getattr(self._local, "connection", None)
        if conn is not None:
            conn.close()
            self._local.connection = None
