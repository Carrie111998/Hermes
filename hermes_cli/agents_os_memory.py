"""Small, profile-aware shared-memory registry for Agents OS.

The registry stores canonical object metadata and provenance.  Search indexes are
derived state.  Callers must always provide a profile and an explicit set of
scopes; the module never guesses that two runtime identities are equivalent.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any, Iterable


MEMORY_SCOPES = frozenset({"private", "profile", "project", "task", "shared"})


def ensure_memory_schema(conn: sqlite3.Connection) -> None:
    """Create the shared-memory tables and FTS projection idempotently."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_objects (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            body_text TEXT NOT NULL DEFAULT '',
            body_uri TEXT,
            content_hash TEXT NOT NULL,
            scope TEXT NOT NULL CHECK(scope IN ('private','profile','project','task','shared')),
            profile_id TEXT NOT NULL,
            project_id TEXT,
            task_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(content_hash, scope, profile_id, project_id, task_id)
        );
        CREATE INDEX IF NOT EXISTS idx_memory_scope
            ON memory_objects(scope, profile_id, project_id, task_id);
        CREATE TABLE IF NOT EXISTS memory_provenance (
            object_id TEXT PRIMARY KEY,
            producer_runtime TEXT NOT NULL,
            producer_agent TEXT NOT NULL,
            source_uri TEXT,
            source_object_id TEXT,
            session_id TEXT,
            parent_session_id TEXT,
            task_id TEXT,
            run_id TEXT,
            workflow TEXT,
            write_origin TEXT,
            tool_name TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(object_id) REFERENCES memory_objects(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS memory_candidates (
            id TEXT PRIMARY KEY,
            result_hash TEXT NOT NULL,
            result_text TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            producer_runtime TEXT NOT NULL,
            producer_agent TEXT NOT NULL,
            task_id TEXT,
            run_id TEXT,
            state TEXT NOT NULL DEFAULT 'candidate'
                CHECK(state IN ('candidate','accepted','rejected')),
            feedback TEXT NOT NULL DEFAULT '',
            object_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(result_hash, profile_id, producer_runtime, producer_agent, task_id, run_id),
            FOREIGN KEY(object_id) REFERENCES memory_objects(id)
        );
        """
    )
    try:
        conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_objects_fts USING fts5(
                title, body_text, content='memory_objects', content_rowid='rowid'
            );
            CREATE TRIGGER IF NOT EXISTS memory_objects_ai AFTER INSERT ON memory_objects BEGIN
              INSERT INTO memory_objects_fts(rowid,title,body_text)
              VALUES(new.rowid,new.title,new.body_text);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_objects_ad AFTER DELETE ON memory_objects BEGIN
              INSERT INTO memory_objects_fts(memory_objects_fts,rowid,title,body_text)
              VALUES('delete',old.rowid,old.title,old.body_text);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_objects_au AFTER UPDATE ON memory_objects BEGIN
              INSERT INTO memory_objects_fts(memory_objects_fts,rowid,title,body_text)
              VALUES('delete',old.rowid,old.title,old.body_text);
              INSERT INTO memory_objects_fts(rowid,title,body_text)
              VALUES(new.rowid,new.title,new.body_text);
            END;
            """
        )
    except sqlite3.OperationalError:
        # Minimal SQLite builds may omit FTS5; LIKE search remains available.
        pass


def _row(row: sqlite3.Row | tuple[Any, ...] | None, columns: Iterable[str]) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(zip(columns, row))


def _hash(text: str, uri: str | None = None) -> str:
    payload = json.dumps({"text": text, "uri": uri}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_scope(scope: str, project_id: str | None, task_id: str | None) -> None:
    if scope not in MEMORY_SCOPES:
        raise ValueError(f"unsupported memory scope: {scope}")
    if scope == "project" and not project_id:
        raise ValueError("project scope requires project_id")
    if scope == "task" and not task_id:
        raise ValueError("task scope requires task_id")


def create_memory_object(
    conn: sqlite3.Connection,
    *,
    kind: str,
    title: str,
    body_text: str = "",
    body_uri: str | None = None,
    scope: str,
    profile_id: str,
    producer_runtime: str,
    producer_agent: str,
    project_id: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    provenance: dict[str, Any] | None = None,
    object_id: str | None = None,
) -> dict[str, Any]:
    """Create or return an identical scoped object without merging identities."""
    ensure_memory_schema(conn)
    _validate_scope(scope, project_id, task_id)
    if not profile_id or not producer_runtime or not producer_agent:
        raise ValueError("profile_id, producer_runtime and producer_agent are required")
    digest = _hash(body_text, body_uri)
    existing = conn.execute(
        """SELECT id FROM memory_objects WHERE content_hash=? AND scope=? AND profile_id=?
           AND project_id IS ? AND task_id IS ?""",
        (digest, scope, profile_id, project_id, task_id),
    ).fetchone()
    if existing:
        return get_memory_object(conn, existing[0])  # type: ignore[return-value]

    oid = object_id or f"memory-{uuid.uuid4().hex}"
    prov = dict(provenance or {})
    conn.execute(
        """INSERT INTO memory_objects
           (id,kind,title,body_text,body_uri,content_hash,scope,profile_id,project_id,task_id)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (oid, kind, title, body_text, body_uri, digest, scope, profile_id, project_id, task_id),
    )
    conn.execute(
        """INSERT INTO memory_provenance
           (object_id,producer_runtime,producer_agent,source_uri,source_object_id,session_id,
            parent_session_id,task_id,run_id,workflow,write_origin,tool_name,metadata)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            oid, producer_runtime, producer_agent, prov.pop("source_uri", body_uri),
            prov.pop("source_object_id", None), prov.pop("session_id", None),
            prov.pop("parent_session_id", None), task_id, run_id,
            prov.pop("workflow", None), prov.pop("write_origin", None),
            prov.pop("tool_name", None), json.dumps(prov, ensure_ascii=False, sort_keys=True),
        ),
    )
    return get_memory_object(conn, oid)  # type: ignore[return-value]


def get_memory_object(conn: sqlite3.Connection, object_id: str) -> dict[str, Any] | None:
    ensure_memory_schema(conn)
    cursor = conn.execute(
        """SELECT o.*, p.producer_runtime, p.producer_agent, p.source_uri,
                  p.source_object_id, p.session_id, p.parent_session_id,
                  p.run_id, p.workflow, p.write_origin, p.tool_name, p.metadata
           FROM memory_objects o LEFT JOIN memory_provenance p ON p.object_id=o.id
           WHERE o.id=?""",
        (object_id,),
    )
    item = _row(cursor.fetchone(), [d[0] for d in cursor.description])
    if item is not None:
        item["metadata"] = json.loads(item.get("metadata") or "{}")
    return item


def search_memory(
    conn: sqlite3.Connection,
    query: str,
    *,
    profile_id: str,
    scopes: Iterable[str],
    project_id: str | None = None,
    task_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search only the caller-declared scopes; shared must be opted into."""
    ensure_memory_schema(conn)
    requested = tuple(dict.fromkeys(scopes))
    if not requested or any(scope not in MEMORY_SCOPES for scope in requested):
        raise ValueError("scopes must be a non-empty collection of known scopes")
    clauses: list[str] = []
    params: list[Any] = []
    for scope in requested:
        if scope == "shared":
            clauses.append("o.scope='shared'")
        elif scope == "project":
            clauses.append("(o.scope='project' AND o.profile_id=? AND o.project_id=?)")
            params.extend((profile_id, project_id))
        elif scope == "task":
            clauses.append("(o.scope='task' AND o.profile_id=? AND o.task_id=?)")
            params.extend((profile_id, task_id))
        else:
            clauses.append("(o.scope=? AND o.profile_id=?)")
            params.extend((scope, profile_id))
    visible = "(" + " OR ".join(clauses) + ")"
    try:
        sql = f"""SELECT o.id FROM memory_objects_fts f
                  JOIN memory_objects o ON o.rowid=f.rowid
                  WHERE memory_objects_fts MATCH ? AND {visible}
                  ORDER BY bm25(memory_objects_fts) LIMIT ?"""
        rows = conn.execute(sql, (query, *params, max(1, min(limit, 100)))).fetchall()
    except sqlite3.OperationalError:
        needle = f"%{query}%"
        sql = f"""SELECT o.id FROM memory_objects o
                  WHERE (o.title LIKE ? OR o.body_text LIKE ?) AND {visible}
                  ORDER BY o.updated_at DESC LIMIT ?"""
        rows = conn.execute(sql, (needle, needle, *params, max(1, min(limit, 100)))).fetchall()
    return [item for row in rows if (item := get_memory_object(conn, row[0])) is not None]


def create_memory_candidate(
    conn: sqlite3.Connection,
    *,
    result_text: str,
    profile_id: str,
    producer_runtime: str,
    producer_agent: str,
    task_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    ensure_memory_schema(conn)
    digest = hashlib.sha256(result_text.encode("utf-8")).hexdigest()
    existing = conn.execute(
        """SELECT id FROM memory_candidates WHERE result_hash=? AND profile_id=?
           AND producer_runtime=? AND producer_agent=? AND task_id IS ? AND run_id IS ?""",
        (digest, profile_id, producer_runtime, producer_agent, task_id, run_id),
    ).fetchone()
    cid = existing[0] if existing else f"candidate-{uuid.uuid4().hex}"
    if not existing:
        conn.execute(
            """INSERT INTO memory_candidates
               (id,result_hash,result_text,profile_id,producer_runtime,producer_agent,task_id,run_id)
               VALUES(?,?,?,?,?,?,?,?)""",
            (cid, digest, result_text, profile_id, producer_runtime, producer_agent, task_id, run_id),
        )
    return get_memory_candidate(conn, cid)  # type: ignore[return-value]


def get_memory_candidate(conn: sqlite3.Connection, candidate_id: str) -> dict[str, Any] | None:
    ensure_memory_schema(conn)
    cursor = conn.execute("SELECT * FROM memory_candidates WHERE id=?", (candidate_id,))
    return _row(cursor.fetchone(), [d[0] for d in cursor.description])


def record_memory_feedback(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    state: str,
    feedback: str = "",
    object_id: str | None = None,
) -> dict[str, Any]:
    if state not in {"candidate", "accepted", "rejected"}:
        raise ValueError("unsupported candidate state")
    ensure_memory_schema(conn)
    changed = conn.execute(
        """UPDATE memory_candidates SET state=?,feedback=?,object_id=?,updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (state, feedback, object_id, candidate_id),
    ).rowcount
    if not changed:
        raise KeyError(candidate_id)
    return get_memory_candidate(conn, candidate_id)  # type: ignore[return-value]
