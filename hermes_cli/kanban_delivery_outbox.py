"""Durable capability-aware parent/child delivery outbox."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from pathlib import Path
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
 subscription_id TEXT NOT NULL,
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
 correlation_key TEXT NOT NULL,
 idempotency_mode TEXT NOT NULL DEFAULT 'correlation-only',
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
 from_state TEXT NOT NULL,
 to_state TEXT NOT NULL,
 policy_version TEXT NOT NULL,
 idempotency_requested INTEGER NOT NULL DEFAULT 0,
 idempotency_confirmed INTEGER NOT NULL DEFAULT 0,
 duplicate_evidence TEXT,
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
 policy_version TEXT NOT NULL DEFAULT 'terminal-audit-v1',
 from_state TEXT NOT NULL DEFAULT 'dead',
 to_state TEXT NOT NULL DEFAULT 'audited',
 created_at INTEGER NOT NULL DEFAULT (unixepoch())
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _ensure_column(conn, "kanban_delivery_parents", "subscription_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "kanban_delivery_children", "correlation_key", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "kanban_delivery_children", "idempotency_mode", "TEXT NOT NULL DEFAULT 'correlation-only'")
    _ensure_column(conn, "kanban_delivery_attempts", "from_state", "TEXT NOT NULL DEFAULT 'pending'")
    _ensure_column(conn, "kanban_delivery_attempts", "to_state", "TEXT NOT NULL DEFAULT 'leased'")
    _ensure_column(conn, "kanban_delivery_attempts", "policy_version", "TEXT NOT NULL DEFAULT 'retry-v1'")
    _ensure_column(conn, "kanban_delivery_attempts", "idempotency_requested", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "kanban_delivery_attempts", "idempotency_confirmed", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "kanban_delivery_attempts", "duplicate_evidence", "TEXT")
    _ensure_column(conn, "kanban_delivery_audit", "policy_version", "TEXT NOT NULL DEFAULT 'terminal-audit-v1'")
    _ensure_column(conn, "kanban_delivery_audit", "from_state", "TEXT NOT NULL DEFAULT 'dead'")
    _ensure_column(conn, "kanban_delivery_audit", "to_state", "TEXT NOT NULL DEFAULT 'audited'")
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    present = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in present:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


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
    allowed = {
        "version", "adapter_type", "adapter_version", "route_kind",
        "supports_async_delivery", "creator_wake_applicable",
        "creator_session_id", "wake_required", "wake_policy_version",
        "artifact_transport", "artifact_policy_version", "idempotency_mode",
    }
    unknown = sorted(set(cap) - allowed)
    if unknown:
        raise CapabilityError(f"unknown route capability fields: {','.join(unknown)}")
    if cap.get("version") != "route-capability-v1":
        raise CapabilityError("unknown route capability version")
    push = cap.get("supports_async_delivery")
    if not isinstance(push, bool):
        raise CapabilityError("supports_async_delivery must be boolean")
    route_kind = cap.get("route_kind")
    if route_kind is not None and route_kind not in ("push", "non_push"):
        raise CapabilityError("route_kind must be push or non_push")
    if route_kind is not None and (route_kind == "push") is not push:
        raise CapabilityError("route_kind contradicts supports_async_delivery")
    wake_applicable = cap.get("creator_wake_applicable")
    if not isinstance(wake_applicable, bool):
        raise CapabilityError("creator_wake_applicable must be boolean")
    if not push and (not wake_applicable or not cap.get("creator_session_id")):
        raise CapabilityError("non-push route requires a stable creator wake target")
    if cap.get("wake_required") and not wake_applicable:
        raise CapabilityError("required wake must be applicable")
    registered = {
        "wake_policy_version": {None, "wake-policy-v1"},
        "artifact_policy_version": {None, "artifact-policy-v1"},
        "idempotency_mode": {None, "correlation-only", "adapter-key-v1"},
    }
    for field, versions in registered.items():
        if cap.get(field) not in versions:
            raise CapabilityError(f"unregistered {field}")
    adapter = cap.get("adapter_type")
    transport = cap.get("artifact_transport")
    if adapter is not None and (not isinstance(adapter, str) or not adapter.strip()):
        raise CapabilityError("adapter_type must be a registered non-empty name")
    if transport is not None and transport not in {
        adapter, "local", "creator_session"
    }:
        raise CapabilityError("unregistered artifact transport")
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
    subscription_id = str(source_frozen.get("subscription_id") or "")
    if not subscription_id:
        raise CapabilityError("immutable subscription_id is required")
    parent_id = _digest("kanban-delivery-parent-v2", source_frozen)
    init_schema(conn)
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
            {"kind": "primary_text", "ordinal": 0, "required": True, "component": {"payload_digest": _digest(text), "text": text}, "policy": "text-policy-v1"}
        )
        if cap.get("creator_wake_applicable"):
            if not cap.get("creator_session_id"):
                raise CapabilityError("applicable creator wake requires a stable target")
            children.append(
                {"kind": "creator_wake", "ordinal": 1, "required": bool(cap.get("wake_required", True)), "component": {"creator_session_id": cap["creator_session_id"], "wake_reason_version": "terminal-v1"}, "policy": cap.get("wake_policy_version") or "wake-policy-v1"}
            )
    else:
        children.append(
            {"kind": "creator_wake", "ordinal": 0, "required": True, "component": {"creator_session_id": cap["creator_session_id"], "wake_reason_version": "terminal-v1"}, "policy": "wake-policy-v1"}
        )
    for index, artifact in enumerate(artifacts):
        item = dict(artifact)
        ordinal = int(item.get("ordinal", index))
        if set(item) - {"manifest_id", "sha256", "ordinal", "path", "optional"}:
            raise CapabilityError("unknown artifact manifest fields")
        if not item.get("manifest_id") or not item.get("sha256"):
            raise CapabilityError("artifact manifest identity and sha256 are required")
        expected = str(item["sha256"]).lower()
        if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
            raise CapabilityError("artifact sha256 must be a lowercase hex digest")
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise CapabilityError("artifact transport path is required")
        path = Path(os.path.expanduser(raw_path)).resolve(strict=False)
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, ValueError) as exc:
            raise CapabilityError("advertised artifact is unavailable") from exc
        if actual != expected:
            raise CapabilityError("advertised artifact digest mismatch")
        item["path"] = str(path)
        item["sha256"] = expected
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
            "INSERT INTO kanban_delivery_parents(parent_id,source_json,capability_version,capability_json,capability_sha256,manifest_sha256,subscription_id,child_count,required_child_count) VALUES(?,?,?,?,?,?,?,?,?)",
            (parent_id, _canonical(source_frozen), cap["version"], cap_json, hashlib.sha256(cap_json.encode()).hexdigest(), manifest_sha, subscription_id, len(manifest), sum(bool(x["required"]) for x in manifest)),
        )
        for child in manifest:
            conn.execute(
                "INSERT INTO kanban_delivery_children(child_id,parent_id,kind,ordinal,component_json,required,policy_version,correlation_key,idempotency_mode) VALUES(?,?,?,?,?,?,?,?,?)",
                (child["child_id"], parent_id, child["kind"], child["ordinal"], _canonical(child["component"]), int(child["required"]), child["policy"], child["child_id"], cap.get("idempotency_mode") or "correlation-only"),
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
            "INSERT INTO kanban_delivery_attempts(child_id,attempt_number,lease_token_hash,started_at,transition,from_state,to_state,policy_version,idempotency_requested) VALUES(?,?,?,?,?,?,?,?,?)",
            (child_id, attempt, token_hash, now, "leased", row[0], "leased", "retry-v1", 1),
        )
    return token


def due_children(
    conn: sqlite3.Connection,
    *,
    now: int,
    parent_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return immutable pending/retryable child work in delivery order."""
    params: list[Any] = [now]
    where = "state IN ('pending','failed') AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
    if parent_id is not None:
        where += " AND parent_id=?"
        params.append(parent_id)
    params.append(max(1, int(limit)))
    rows = conn.execute(
        f"SELECT * FROM kanban_delivery_children WHERE {where} "
        "ORDER BY CASE kind WHEN 'primary_text' THEN 0 WHEN 'artifact_upload' THEN 1 ELSE 2 END, ordinal, child_id LIMIT ?",
        params,
    ).fetchall()
    children: list[dict[str, Any]] = []
    for row in rows:
        child = dict(row)
        child["component"] = json.loads(child.pop("component_json"))
        children.append(child)
    return children


def recover_expired(conn: sqlite3.Connection, *, now: int) -> list[str]:
    """Recover expired leases while preserving uncertain send evidence."""
    recovered: list[str] = []
    with conn:
        rows = conn.execute(
            "SELECT child_id,state,attempt_count,lease_token_hash,parent_id "
            "FROM kanban_delivery_children WHERE state IN ('leased','sending') "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
            (now,),
        ).fetchall()
        for row in rows:
            error_class = (
                "uncertain_after_expired_sending"
                if row[1] == "sending"
                else "expired_before_send"
            )
            conn.execute(
                "UPDATE kanban_delivery_children SET state='failed',last_error_class=?,"
                "next_attempt_at=?,lease_owner=NULL,lease_token_hash=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE child_id=? AND state=?",
                (error_class, now, now, row[0], row[1]),
            )
            conn.execute(
                "UPDATE kanban_delivery_attempts SET ended_at=?,transition='failed',error_class=? "
                "WHERE child_id=? AND attempt_number=? AND lease_token_hash=?",
                (now, error_class, row[0], row[2], row[3]),
            )
            _derive_parent(conn, row[4], now)
            recovered.append(str(row[0]))
    return recovered


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
        conn.execute("UPDATE kanban_delivery_attempts SET ended_at=?,transition='sent',from_state='sending',to_state='sent',safe_receipt=?,idempotency_confirmed=1 WHERE child_id=? AND attempt_number=? AND lease_token_hash=?", (now, receipt, child_id, row[3], token_hash))
        _derive_parent(conn, row[2], now)


def mark_failed(conn: sqlite3.Connection, child_id: str, token: str, *, error_class: str, now: int, retry_at: int) -> None:
    with conn:
        row = conn.execute("SELECT state,lease_token_hash,parent_id,attempt_count FROM kanban_delivery_children WHERE child_id=?", (child_id,)).fetchone()
        token_hash = _assert_token(row, token, "sending")
        conn.execute("UPDATE kanban_delivery_children SET state='failed',last_error_class=?,next_attempt_at=?,lease_owner=NULL,lease_token_hash=NULL,lease_expires_at=NULL,updated_at=? WHERE child_id=?", (error_class, retry_at, now, child_id))
        conn.execute("UPDATE kanban_delivery_attempts SET ended_at=?,transition='failed',error_class=? WHERE child_id=? AND attempt_number=? AND lease_token_hash=?", (now, error_class, child_id, row[3], token_hash))
        _derive_parent(conn, row[2], now)


def mark_dead(conn: sqlite3.Connection, child_id: str, *, error_class: str, now: int) -> None:
    with conn:
        row = conn.execute(
            "SELECT state,parent_id FROM kanban_delivery_children WHERE child_id=?",
            (child_id,),
        ).fetchone()
        if not row or row[0] != "failed":
            raise TransitionError("only failed children can become dead")
        conn.execute(
            "UPDATE kanban_delivery_children SET state='dead',last_error_class=?,updated_at=? WHERE child_id=?",
            (error_class, now, child_id),
        )
        _derive_parent(conn, row[1], now)


def audit_dead_child(
    conn: sqlite3.Connection,
    child_id: str,
    *,
    actor: str,
    reason_code: str,
    evidence: str,
    completion_permitted: bool,
    now: int,
) -> None:
    if not actor or not reason_code or not evidence:
        raise TransitionError("actor, reason and evidence are required")
    with conn:
        row = conn.execute(
            "SELECT state,parent_id FROM kanban_delivery_children WHERE child_id=?",
            (child_id,),
        ).fetchone()
        if not row or row[0] != "dead":
            raise TransitionError("only dead children can be audited")
        conn.execute(
            "INSERT INTO kanban_delivery_audit(child_id,actor,reason_code,evidence_sha256,completion_permitted,created_at) VALUES(?,?,?,?,?,?)",
            (child_id, actor, reason_code, hashlib.sha256(evidence.encode()).hexdigest(), int(completion_permitted), now),
        )
        if completion_permitted:
            conn.execute(
                "UPDATE kanban_delivery_children SET state='audited',updated_at=? WHERE child_id=?",
                (now, child_id),
            )
        _derive_parent(conn, row[1], now)


def _derive_parent(conn: sqlite3.Connection, parent_id: str, now: int) -> None:
    rows = conn.execute("SELECT state FROM kanban_delivery_children WHERE parent_id=?", (parent_id,)).fetchall()
    complete = bool(rows) and all(row[0] in ("sent", "audited") for row in rows)
    state = "sent" if complete and all(row[0] == "sent" for row in rows) else "audited" if complete else "pending"
    conn.execute("UPDATE kanban_delivery_parents SET state=?,completed_at=?,updated_at=? WHERE parent_id=?", (state, now if complete else None, now, parent_id))


def parent_complete(conn: sqlite3.Connection, parent_id: str) -> bool:
    row = conn.execute("SELECT state FROM kanban_delivery_parents WHERE parent_id=?", (parent_id,)).fetchone()
    return bool(row and row[0] in ("sent", "audited"))


async def process_parent(
    conn: sqlite3.Connection,
    parent_id: str,
    *,
    owner: str,
    send_child,
    now: int | None = None,
    lease_seconds: int = 60,
    retry_seconds: int = 5,
) -> bool:
    """Lease, send and durably acknowledge each incomplete child independently."""
    import time

    current = int(time.time()) if now is None else int(now)
    recover_expired(conn, now=current)
    for child in due_children(conn, parent_id=parent_id, now=current):
        token = lease_child(
            conn,
            child["child_id"],
            owner,
            now=current,
            lease_seconds=lease_seconds,
        )
        mark_sending(conn, child["child_id"], token, now=current)
        try:
            receipt = await send_child(child)
            mark_sent(
                conn,
                child["child_id"],
                token,
                receipt=str(receipt or ""),
                now=current,
            )
        except Exception as exc:
            mark_failed(
                conn,
                child["child_id"],
                token,
                error_class=type(exc).__name__,
                now=current,
                retry_at=current + retry_seconds,
            )
    return parent_complete(conn, parent_id)
