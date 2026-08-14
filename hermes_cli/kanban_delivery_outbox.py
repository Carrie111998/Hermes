"""Durable capability-aware parent/child delivery outbox."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from typing import Any, Iterable, Mapping

STATES = ("pending", "leased", "sending", "sent", "failed", "dead", "audited")


class CapabilityError(ValueError):
    pass


class TransitionError(RuntimeError):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kanban_delivery_parents (
 parent_id TEXT PRIMARY KEY,
 source_json TEXT NOT NULL,
 capability_version TEXT NOT NULL,
 capability_json TEXT NOT NULL,
 capability_sha256 TEXT NOT NULL,
 manifest_sha256 TEXT NOT NULL,
 child_count INTEGER NOT NULL,
 required_child_count INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending',
 created_at INTEGER NOT NULL DEFAULT (unixepoch()),
 updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
 completed_at INTEGER
);
CREATE TABLE IF NOT EXISTS kanban_delivery_children (
 child_id TEXT PRIMARY KEY,
 parent_id TEXT NOT NULL REFERENCES kanban_delivery_parents(parent_id),
 kind TEXT NOT NULL CHECK(kind IN ('primary_text','artifact_upload','creator_wake')),
 ordinal INTEGER NOT NULL,
 component_json TEXT NOT NULL,
 required INTEGER NOT NULL CHECK(required IN (0,1)),
 policy_version TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','leased','sending','sent','failed','dead','audited')),
 attempt_count INTEGER NOT NULL DEFAULT 0,
 next_attempt_at INTEGER,
 lease_owner TEXT,
 lease_token_hash TEXT,
 lease_expires_at INTEGER,
 sending_at INTEGER,
 sent_at INTEGER,
 safe_receipt TEXT,
 last_error_class TEXT,
 created_at INTEGER NOT NULL DEFAULT (unixepoch()),
 updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
 UNIQUE(parent_id, kind, ordinal)
);
CREATE TABLE IF NOT EXISTS kanban_delivery_attempts (
 attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
 child_id TEXT NOT NULL REFERENCES kanban_delivery_children(child_id),
 attempt_number INTEGER NOT NULL,
 lease_token_hash TEXT NOT NULL,
 started_at INTEGER NOT NULL,
 ended_at INTEGER,
 transition TEXT NOT NULL,
 safe_receipt TEXT,
 error_class TEXT,
 UNIQUE(child_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS kanban_delivery_audit (
 audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
 child_id TEXT NOT NULL REFERENCES kanban_delivery_children(child_id),
 actor TEXT NOT NULL,
 reason_code TEXT NOT NULL,
 evidence_sha256 TEXT NOT NULL,
 completion_permitted INTEGER NOT NULL,
 created_at INTEGER NOT NULL DEFAULT (unixepoch())
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(*parts: Any) -> str:
    h = hashlib.sha256()
    for part in parts:
        raw = _canonical(part).encode("utf-8")
        h.update(len(raw).to_bytes(8, "big"))
        h.update(raw)
    return h.hexdigest()


def _validate_capability(capability: Mapping[str, Any]) -> dict[str, Any]:
    cap = dict(capability)
    if cap.get("version") != "route-capability-v1":
        raise CapabilityError("unknown route capability version")
    push = cap.get("supports_async_delivery")
    if not isinstance(push, bool):
        raise CapabilityError("supports_async_delivery must be boolean")
    if not push and (
        not cap.get("creator_wake_applicable") or not cap.get("creator_session_id")
    ):
        raise CapabilityError("non-push route requires a stable creator wake target")
    return cap


def materialize_parent(
    conn: sqlite3.Connection,
    *,
    source: Mapping[str, Any],
    capability: Mapping[str, Any],
    text: str,
    artifacts: Iterable[Mapping[str, Any]] = (),
) -> str:
    source_frozen = dict(source)
    parent_id = _digest("kanban-delivery-parent-v2", source_frozen)
    existing = conn.execute(
        "SELECT parent_id FROM kanban_delivery_parents WHERE parent_id=?", (parent_id,)
    ).fetchone()
    if existing:
        return str(existing[0])
    cap = _validate_capability(capability)
    children: list[dict[str, Any]] = []
    if cap["supports_async_delivery"]:
        if not text:
            raise CapabilityError("push route requires text")
        children.append(
            {"kind": "primary_text", "ordinal": 0, "required": True, "component": {"payload_digest": _digest(text)}, "policy": "text-policy-v1"}
        )
    else:
        children.append(
            {"kind": "creator_wake", "ordinal": 0, "required": True, "component": {"creator_session_id": cap["creator_session_id"], "wake_reason_version": "terminal-v1"}, "policy": "wake-policy-v1"}
        )
    for index, artifact in enumerate(artifacts):
        item = dict(artifact)
        ordinal = int(item.get("ordinal", index))
        if not item.get("manifest_id") or not item.get("sha256"):
            raise CapabilityError("artifact manifest identity and sha256 are required")
        children.append(
            {"kind": "artifact_upload", "ordinal": ordinal + 1, "required": not bool(item.get("optional", False)), "component": item, "policy": cap.get("artifact_policy_version") or "artifact-policy-v1"}
        )
    cap_json = _canonical(cap)
    manifest = []
    for child in children:
        child["child_id"] = _digest(parent_id, child["kind"], child["ordinal"], child["component"])
        manifest.append(child)
    manifest_sha = _digest(manifest)
    with conn:
        conn.execute(
            "INSERT INTO kanban_delivery_parents(parent_id,source_json,capability_version,capability_json,capability_sha256,manifest_sha256,child_count,required_child_count) VALUES(?,?,?,?,?,?,?,?)",
            (parent_id, _canonical(source_frozen), cap["version"], cap_json, hashlib.sha256(cap_json.encode()).hexdigest(), manifest_sha, len(manifest), sum(bool(x["required"]) for x in manifest)),
        )
        for child in manifest:
            conn.execute(
                "INSERT INTO kanban_delivery_children(child_id,parent_id,kind,ordinal,component_json,required,policy_version) VALUES(?,?,?,?,?,?,?)",
                (child["child_id"], parent_id, child["kind"], child["ordinal"], _canonical(child["component"]), int(child["required"]), child["policy"]),
            )
    return parent_id


def lease_child(conn: sqlite3.Connection, child_id: str, owner: str, *, now: int, lease_seconds: int) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with conn:
        row = conn.execute("SELECT state,attempt_count,next_attempt_at FROM kanban_delivery_children WHERE child_id=?", (child_id,)).fetchone()
        if not row or row[0] not in ("pending", "failed") or (row[2] is not None and row[2] > now):
            raise TransitionError("child is not due for lease")
        attempt = int(row[1]) + 1
        changed = conn.execute(
            "UPDATE kanban_delivery_children SET state='leased',attempt_count=?,lease_owner=?,lease_token_hash=?,lease_expires_at=?,updated_at=? WHERE child_id=? AND state IN ('pending','failed')",
            (attempt, owner, token_hash, now + lease_seconds, now, child_id),
        ).rowcount
        if changed != 1:
            raise TransitionError("lease compare-and-swap failed")
        conn.execute(
            "INSERT INTO kanban_delivery_attempts(child_id,attempt_number,lease_token_hash,started_at,transition) VALUES(?,?,?,?,?)",
            (child_id, attempt, token_hash, now, "leased"),
        )
    return token


def _assert_token(row: sqlite3.Row | tuple, token: str, expected: str) -> str:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if not row or row[0] != expected or row[1] != token_hash:
        raise TransitionError("invalid state or lease token")
    return token_hash


def mark_sending(conn: sqlite3.Connection, child_id: str, token: str, *, now: int) -> None:
    with conn:
        row = conn.execute("SELECT state,lease_token_hash FROM kanban_delivery_children WHERE child_id=?", (child_id,)).fetchone()
        _assert_token(row, token, "leased")
        conn.execute("UPDATE kanban_delivery_children SET state='sending',sending_at=?,updated_at=? WHERE child_id=?", (now, now, child_id))


def mark_sent(conn: sqlite3.Connection, child_id: str, token: str, *, receipt: str, now: int) -> None:
    if not receipt or len(receipt) > 1024:
        raise TransitionError("safe receipt required")
    with conn:
        row = conn.execute("SELECT state,lease_token_hash,parent_id,attempt_count FROM kanban_delivery_children WHERE child_id=?", (child_id,)).fetchone()
        token_hash = _assert_token(row, token, "sending")
        conn.execute("UPDATE kanban_delivery_children SET state='sent',sent_at=?,safe_receipt=?,lease_owner=NULL,lease_token_hash=NULL,lease_expires_at=NULL,updated_at=? WHERE child_id=?", (now, receipt, now, child_id))
        conn.execute("UPDATE kanban_delivery_attempts SET ended_at=?,transition='sent',safe_receipt=? WHERE child_id=? AND attempt_number=? AND lease_token_hash=?", (now, receipt, child_id, row[3], token_hash))
        _derive_parent(conn, row[2], now)


def mark_failed(conn: sqlite3.Connection, child_id: str, token: str, *, error_class: str, now: int, retry_at: int) -> None:
    with conn:
        row = conn.execute("SELECT state,lease_token_hash,parent_id,attempt_count FROM kanban_delivery_children WHERE child_id=?", (child_id,)).fetchone()
        token_hash = _assert_token(row, token, "sending")
        conn.execute("UPDATE kanban_delivery_children SET state='failed',last_error_class=?,next_attempt_at=?,lease_owner=NULL,lease_token_hash=NULL,lease_expires_at=NULL,updated_at=? WHERE child_id=?", (error_class, retry_at, now, child_id))
        conn.execute("UPDATE kanban_delivery_attempts SET ended_at=?,transition='failed',error_class=? WHERE child_id=? AND attempt_number=? AND lease_token_hash=?", (now, error_class, child_id, row[3], token_hash))
        _derive_parent(conn, row[2], now)


def _derive_parent(conn: sqlite3.Connection, parent_id: str, now: int) -> None:
    rows = conn.execute("SELECT state FROM kanban_delivery_children WHERE parent_id=?", (parent_id,)).fetchall()
    complete = bool(rows) and all(row[0] in ("sent", "audited") for row in rows)
    state = "sent" if complete and all(row[0] == "sent" for row in rows) else "audited" if complete else "pending"
    conn.execute("UPDATE kanban_delivery_parents SET state=?,completed_at=?,updated_at=? WHERE parent_id=?", (state, now if complete else None, now, parent_id))


def parent_complete(conn: sqlite3.Connection, parent_id: str) -> bool:
    row = conn.execute("SELECT state FROM kanban_delivery_parents WHERE parent_id=?", (parent_id,)).fetchone()
    return bool(row and row[0] in ("sent", "audited"))
