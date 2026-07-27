"""Durable saga compensation obligations for partially successful effects."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any, Mapping, Optional


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS compensation_obligations (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
    objective_id TEXT NOT NULL, original_action_id TEXT NOT NULL UNIQUE,
    compensation_action_type TEXT NOT NULL,
    compensation_payload_scope_json TEXT NOT NULL,
    compensation_payload_scope_sha256 TEXT NOT NULL,
    compensation_capability TEXT NOT NULL,
    compensation_verification_method TEXT NOT NULL,
    status TEXT NOT NULL, created_at INTEGER NOT NULL,
    compensation_action_id TEXT, verification_id TEXT, resolved_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_compensation_objective
    ON compensation_obligations(objective_id,status,created_at);
CREATE TRIGGER IF NOT EXISTS compensation_contract_immutable
BEFORE UPDATE ON compensation_obligations
WHEN OLD.id IS NOT NEW.id OR OLD.organization_id IS NOT NEW.organization_id
 OR OLD.objective_id IS NOT NEW.objective_id
 OR OLD.original_action_id IS NOT NEW.original_action_id
 OR OLD.compensation_action_type IS NOT NEW.compensation_action_type
 OR OLD.compensation_payload_scope_json IS NOT NEW.compensation_payload_scope_json
 OR OLD.compensation_payload_scope_sha256 IS NOT NEW.compensation_payload_scope_sha256
 OR OLD.compensation_capability IS NOT NEW.compensation_capability
 OR OLD.compensation_verification_method IS NOT NEW.compensation_verification_method
 OR OLD.created_at IS NOT NEW.created_at
BEGIN SELECT RAISE(ABORT, 'compensation contract is immutable'); END;
CREATE TRIGGER IF NOT EXISTS compensation_obligations_immutable_delete
BEFORE DELETE ON compensation_obligations
BEGIN SELECT RAISE(ABORT, 'compensation obligations cannot be deleted'); END;
"""

VOLATILE_STATE_FIELDS = frozenset(
    {"observed_state_at", "max_state_age_seconds", "state_evidence"}
)


class CompensationError(PermissionError):
    pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction and conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='compensation_obligations'"
    ).fetchone():
        return
    conn.executescript(SCHEMA_SQL)


def payload_scope(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in VOLATILE_STATE_FIELDS and not key.startswith("_")
    }


def _scope_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload_scope(payload), separators=(",", ":"), sort_keys=True)


def create_for_failed_verification(
    conn: sqlite3.Connection,
    *,
    action_id: str,
) -> Optional[str]:
    ensure_schema(conn)
    row = conn.execute(
        """SELECT a.*,o.organization_id FROM candidate_actions a
           JOIN objectives o ON o.id=a.objective_id WHERE a.id=?""",
        (action_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"action not found: {action_id}")
    if not row["reversible"] or not row["compensation_action_type"]:
        return None
    existing = conn.execute(
        "SELECT id FROM compensation_obligations WHERE original_action_id=?",
        (action_id,),
    ).fetchone()
    if existing is not None:
        return str(existing["id"])
    scope_json = _scope_json(json.loads(row["compensation_payload_json"]))
    obligation_id = f"compensation_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO compensation_obligations
               (id,organization_id,objective_id,original_action_id,
                compensation_action_type,compensation_payload_scope_json,
                compensation_payload_scope_sha256,compensation_capability,
                compensation_verification_method,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,'required',?)""",
            (
                obligation_id, row["organization_id"], row["objective_id"],
                action_id, row["compensation_action_type"], scope_json,
                hashlib.sha256(scope_json.encode()).hexdigest(),
                row["compensation_capability"],
                row["compensation_verification_method"], int(time.time()),
            ),
        )
    return obligation_id


def outstanding(conn: sqlite3.Connection, objective_id: str) -> Optional[dict[str, Any]]:
    ensure_schema(conn)
    row = conn.execute(
        """SELECT * FROM compensation_obligations
           WHERE objective_id=? AND status='required'
           ORDER BY created_at,id LIMIT 1""",
        (objective_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def assert_action_matches_obligation(
    conn: sqlite3.Connection,
    *,
    objective_id: str,
    action_type: str,
    payload: Mapping[str, Any],
    capability: str,
    verification_method: str,
) -> Optional[str]:
    obligation = outstanding(conn, objective_id)
    if obligation is None:
        return None
    scope_json = _scope_json(payload)
    if (
        action_type != obligation["compensation_action_type"]
        or capability != obligation["compensation_capability"]
        or verification_method != obligation["compensation_verification_method"]
        or hashlib.sha256(scope_json.encode()).hexdigest()
        != obligation["compensation_payload_scope_sha256"]
    ):
        raise CompensationError(
            "objective has an outstanding exact compensation obligation"
        )
    return str(obligation["id"])


def resolve_for_action(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    verification_id: str,
) -> Optional[str]:
    ensure_schema(conn)
    action = conn.execute(
        """SELECT objective_id,action_type,payload_json,required_capability,
                  verification_method FROM candidate_actions WHERE id=?""",
        (action_id,),
    ).fetchone()
    if action is None:
        return None
    obligation_id = assert_action_matches_obligation(
        conn,
        objective_id=action["objective_id"],
        action_type=action["action_type"],
        payload=json.loads(action["payload_json"]),
        capability=action["required_capability"],
        verification_method=action["verification_method"],
    )
    if obligation_id is None:
        return None
    with conn:
        conn.execute(
            """UPDATE compensation_obligations SET status='verified',
               compensation_action_id=?,verification_id=?,resolved_at=?
               WHERE id=? AND status='required'""",
            (action_id, verification_id, int(time.time()), obligation_id),
        )
    return obligation_id
