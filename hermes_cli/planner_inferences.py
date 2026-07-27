"""Immutable exact lineage for probabilistic objective-planner invocations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any, Mapping, Optional


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS planner_inferences (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
    objective_id TEXT NOT NULL, inbox_event_id TEXT,
    planner_identity TEXT NOT NULL, task TEXT NOT NULL,
    model TEXT, request_json TEXT NOT NULL, request_sha256 TEXT NOT NULL,
    response_text TEXT, response_sha256 TEXT,
    parse_status TEXT NOT NULL, error TEXT,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    started_at INTEGER NOT NULL, finished_at INTEGER NOT NULL,
    FOREIGN KEY(objective_id) REFERENCES objectives(id)
);
CREATE INDEX IF NOT EXISTS idx_planner_inferences_objective
    ON planner_inferences(objective_id,started_at,id);
CREATE TRIGGER IF NOT EXISTS planner_inferences_immutable_update
BEFORE UPDATE ON planner_inferences
BEGIN SELECT RAISE(ABORT, 'planner inferences are immutable'); END;
CREATE TRIGGER IF NOT EXISTS planner_inferences_immutable_delete
BEFORE DELETE ON planner_inferences
BEGIN SELECT RAISE(ABORT, 'planner inferences are immutable'); END;
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def record(
    conn: sqlite3.Connection,
    *,
    objective_id: str,
    inbox_event_id: Optional[str],
    planner_identity: str,
    task: str,
    model: Optional[str],
    request: Mapping[str, Any],
    response_text: Optional[str],
    parse_status: str,
    error: Optional[str],
    input_tokens: int,
    output_tokens: int,
    started_at: int,
    finished_at: Optional[int] = None,
) -> str:
    """Persist one completed or failed inference without later mutation."""
    ensure_schema(conn)
    if parse_status not in {"parsed", "invalid_response", "call_failed"}:
        raise ValueError("invalid planner inference parse status")
    objective = conn.execute(
        "SELECT organization_id FROM objectives WHERE id=?", (objective_id,)
    ).fetchone()
    if objective is None:
        raise KeyError(f"objective not found: {objective_id}")
    if inbox_event_id is not None:
        event = conn.execute(
            "SELECT objective_id FROM objective_inbox WHERE id=?",
            (inbox_event_id,),
        ).fetchone()
        if event is None or str(event["objective_id"]) != objective_id:
            raise PermissionError("planner inference event belongs elsewhere")
    request_json = _canonical(dict(request))
    response_hash = (
        hashlib.sha256(response_text.encode()).hexdigest()
        if response_text is not None
        else None
    )
    inference_id = f"inference_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO planner_inferences (
                 id,organization_id,objective_id,inbox_event_id,planner_identity,
                 task,model,request_json,request_sha256,response_text,
                 response_sha256,parse_status,error,input_tokens,output_tokens,
                 started_at,finished_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                inference_id,
                str(objective["organization_id"]),
                objective_id,
                inbox_event_id,
                planner_identity,
                task,
                model,
                request_json,
                hashlib.sha256(request_json.encode()).hexdigest(),
                response_text,
                response_hash,
                parse_status,
                error,
                max(0, int(input_tokens)),
                max(0, int(output_tokens)),
                int(started_at),
                int(time.time()) if finished_at is None else int(finished_at),
            ),
        )
    return inference_id
