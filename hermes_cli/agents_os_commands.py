"""Durable, side-effect-free Jarvis command lifecycle.

This module owns command state only.  It deliberately does not invoke an agent,
spawn a process, resolve the Agents OS approval record, or merge memory.  Those
operations belong to adapters which can drive this state machine.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


COMMAND_STATES = {
    "draft",
    "awaiting_approval",
    "queued",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
}
TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
FEEDBACK_VERDICTS = {"accepted", "corrected", "rejected"}


class CommandNotFound(LookupError):
    pass


class CommandConflict(RuntimeError):
    """The command changed or the requested transition is not valid."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS jarvis_commands (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    transcript TEXT NOT NULL,
    intent_json TEXT NOT NULL DEFAULT '{}',
    risk_class TEXT NOT NULL,
    approval_required INTEGER NOT NULL CHECK (approval_required IN (0, 1)),
    state TEXT NOT NULL CHECK (state IN (
        'draft','awaiting_approval','queued','running','cancelling',
        'succeeded','failed','cancelled'
    )),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    approval_id TEXT,
    run_id TEXT,
    result_json TEXT,
    error_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    cancel_requested_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jarvis_commands_state
    ON jarvis_commands(state, updated_at);

CREATE TABLE IF NOT EXISTS jarvis_command_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id TEXT NOT NULL REFERENCES jarvis_commands(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jarvis_command_events_command
    ON jarvis_command_events(command_id, id);

CREATE TABLE IF NOT EXISTS jarvis_feedback_candidates (
    id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL REFERENCES jarvis_commands(id) ON DELETE CASCADE,
    verdict TEXT NOT NULL CHECK (verdict IN ('accepted','corrected','rejected')),
    correction TEXT,
    candidate_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (status = 'candidate'),
    direct_memory_merge INTEGER NOT NULL DEFAULT 0 CHECK (direct_memory_merge = 0),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jarvis_feedback_command
    ON jarvis_feedback_candidates(command_id, created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decoded(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _request_hash(transcript: str, risk_class: str, approval_required: bool, intent: Any, metadata: Any) -> str:
    body = _json({
        "transcript": transcript,
        "risk_class": risk_class,
        "approval_required": bool(approval_required),
        "intent": intent or {},
        "metadata": metadata or {},
    })
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _row(conn: sqlite3.Connection, command_id: str) -> sqlite3.Row:
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM jarvis_commands WHERE id=?", (command_id,)).fetchone()
    finally:
        conn.row_factory = old_factory
    if row is None:
        raise CommandNotFound(command_id)
    return row


def _event(
    conn: sqlite3.Connection,
    command_id: str,
    event_type: str,
    state: str,
    version: int,
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO jarvis_command_events(command_id,event_type,state,version,payload_json,created_at) VALUES(?,?,?,?,?,?)",
        (command_id, event_type, state, version, _json(payload), _now()),
    )


def create_command(
    conn: sqlite3.Connection,
    *,
    transcript: str,
    idempotency_key: str,
    risk_class: str = "safe_local",
    approval_required: bool = False,
    intent: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    command_id: str | None = None,
) -> dict[str, Any]:
    """Create a draft, returning the original draft on an idempotent retry."""
    transcript = transcript.strip()
    idempotency_key = idempotency_key.strip()
    if not transcript:
        raise ValueError("transcript is required")
    if not idempotency_key:
        raise ValueError("idempotency_key is required")
    ensure_schema(conn)
    digest = _request_hash(transcript, risk_class, approval_required, intent, metadata)
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute("SELECT * FROM jarvis_commands WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    finally:
        conn.row_factory = old_factory
    if existing is not None:
        if existing["request_hash"] != digest:
            raise CommandConflict("idempotency_key_reused_with_different_request")
        return get_command(conn, existing["id"])

    command_id = command_id or f"jarvis-{uuid.uuid4().hex[:12]}"
    now = _now()
    try:
        with conn:
            conn.execute(
                """INSERT INTO jarvis_commands(
                    id,idempotency_key,request_hash,transcript,intent_json,risk_class,
                    approval_required,state,version,metadata_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,'draft',1,?,?,?)""",
                (command_id, idempotency_key, digest, transcript, _json(intent), risk_class,
                 1 if approval_required else 0, _json(metadata), now, now),
            )
            _event(conn, command_id, "command_created", "draft", 1)
    except sqlite3.IntegrityError as exc:
        # A concurrent creator may have won the unique-key race.
        old_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            existing = conn.execute("SELECT * FROM jarvis_commands WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        finally:
            conn.row_factory = old_factory
        if existing is None or existing["request_hash"] != digest:
            raise CommandConflict("command_create_conflict") from exc
        command_id = existing["id"]
    return get_command(conn, command_id)


def _transition(
    conn: sqlite3.Connection,
    command_id: str,
    *,
    expected_version: int,
    allowed_from: set[str],
    state: str,
    event_type: str,
    fields: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if state not in COMMAND_STATES:
        raise ValueError(f"unknown state: {state}")
    ensure_schema(conn)
    row = _row(conn, command_id)
    if row["version"] != expected_version:
        raise CommandConflict(f"version_conflict: expected {expected_version}, current {row['version']}")
    if row["state"] not in allowed_from:
        raise CommandConflict(f"invalid_transition: {row['state']} -> {state}")
    version = expected_version + 1
    updates: dict[str, Any] = {"state": state, "version": version, "updated_at": _now()}
    updates.update(fields or {})
    if state in TERMINAL_STATES:
        updates.setdefault("completed_at", _now())
    assignments = ",".join(f"{name}=?" for name in updates)
    values = list(updates.values()) + [command_id, expected_version]
    with conn:
        changed = conn.execute(
            f"UPDATE jarvis_commands SET {assignments} WHERE id=? AND version=?", values
        ).rowcount
        if changed != 1:
            raise CommandConflict("version_conflict")
        _event(conn, command_id, event_type, state, version, payload)
    return get_command(conn, command_id)


def confirm_command(
    conn: sqlite3.Connection,
    command_id: str,
    *,
    expected_version: int,
    approval_id: str | None = None,
) -> dict[str, Any]:
    """Confirm a draft; no execution is performed."""
    row = _row(conn, command_id)
    needs_approval = bool(row["approval_required"])
    if needs_approval and not approval_id:
        raise ValueError("approval_id is required for an approval-gated command")
    state = "awaiting_approval" if needs_approval else "queued"
    return _transition(
        conn, command_id, expected_version=expected_version, allowed_from={"draft"},
        state=state, event_type="command_confirmed",
        fields={"approval_id": approval_id}, payload={"approval_required": needs_approval},
    )


def resolve_command_approval(
    conn: sqlite3.Connection,
    command_id: str,
    *,
    expected_version: int,
    approved: bool,
    approval_id: str,
) -> dict[str, Any]:
    row = _row(conn, command_id)
    if row["approval_id"] != approval_id:
        raise CommandConflict("approval_id_mismatch")
    return _transition(
        conn, command_id, expected_version=expected_version,
        allowed_from={"awaiting_approval"}, state="queued" if approved else "cancelled",
        event_type="approval_approved" if approved else "approval_rejected",
        payload={"approval_id": approval_id},
    )


def mark_running(
    conn: sqlite3.Connection,
    command_id: str,
    *,
    expected_version: int,
    run_id: str,
) -> dict[str, Any]:
    if not run_id.strip():
        raise ValueError("run_id is required")
    return _transition(
        conn, command_id, expected_version=expected_version, allowed_from={"queued"},
        state="running", event_type="run_started", fields={"run_id": run_id},
        payload={"run_id": run_id},
    )


def record_progress(
    conn: sqlite3.Connection,
    command_id: str,
    *,
    expected_version: int,
    progress: dict[str, Any],
) -> dict[str, Any]:
    return _transition(
        conn, command_id, expected_version=expected_version, allowed_from={"running"},
        state="running", event_type="run_progress", payload=progress,
    )


def complete_command(
    conn: sqlite3.Connection,
    command_id: str,
    *,
    expected_version: int,
    succeeded: bool,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = "succeeded" if succeeded else "failed"
    return _transition(
        conn, command_id, expected_version=expected_version, allowed_from={"running"},
        state=state, event_type="run_completed" if succeeded else "run_failed",
        fields={"result_json": _json(result) if succeeded else None,
                "error_json": _json(error) if not succeeded else None},
        payload={"has_result": result is not None, "has_error": error is not None},
    )


def cancel_command(
    conn: sqlite3.Connection,
    command_id: str,
    *,
    expected_version: int,
    reason: str | None = None,
) -> dict[str, Any]:
    row = _row(conn, command_id)
    target = "cancelling" if row["state"] == "running" else "cancelled"
    return _transition(
        conn, command_id, expected_version=expected_version,
        allowed_from={"draft", "awaiting_approval", "queued", "running"},
        state=target, event_type="cancel_requested",
        fields={"cancel_requested_at": _now()}, payload={"reason": reason},
    )


def acknowledge_cancel(
    conn: sqlite3.Connection,
    command_id: str,
    *,
    expected_version: int,
) -> dict[str, Any]:
    return _transition(
        conn, command_id, expected_version=expected_version, allowed_from={"cancelling"},
        state="cancelled", event_type="cancel_acknowledged",
    )


def create_feedback_candidate(
    conn: sqlite3.Connection,
    command_id: str,
    *,
    expected_version: int,
    verdict: str,
    correction: str | None = None,
    candidate: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    feedback_id: str | None = None,
) -> dict[str, Any]:
    if verdict not in FEEDBACK_VERDICTS:
        raise ValueError(f"verdict must be one of: {', '.join(sorted(FEEDBACK_VERDICTS))}")
    if verdict == "corrected" and not (correction or "").strip():
        raise ValueError("correction is required for corrected feedback")
    ensure_schema(conn)
    row = _row(conn, command_id)
    if row["version"] != expected_version:
        raise CommandConflict(f"version_conflict: expected {expected_version}, current {row['version']}")
    if row["state"] not in {"succeeded", "failed", "cancelled"}:
        raise CommandConflict("feedback_requires_terminal_command")
    feedback_id = feedback_id or f"feedback-{uuid.uuid4().hex[:12]}"
    version = expected_version + 1
    now = _now()
    with conn:
        conn.execute(
            """INSERT INTO jarvis_feedback_candidates(
                id,command_id,verdict,correction,candidate_json,metadata_json,
                status,direct_memory_merge,created_at
            ) VALUES(?,?,?,?,?,?,'candidate',0,?)""",
            (feedback_id, command_id, verdict, correction, _json(candidate), _json(metadata), now),
        )
        changed = conn.execute(
            "UPDATE jarvis_commands SET version=?,updated_at=? WHERE id=? AND version=?",
            (version, now, command_id, expected_version),
        ).rowcount
        if changed != 1:
            raise CommandConflict("version_conflict")
        _event(conn, command_id, "feedback_candidate_created", row["state"], version,
               {"feedback_id": feedback_id, "verdict": verdict, "direct_memory_merge": False})
    return get_command(conn, command_id)


def get_command(conn: sqlite3.Connection, command_id: str) -> dict[str, Any]:
    ensure_schema(conn)
    row = _row(conn, command_id)
    command = dict(row)
    for key in ("intent_json", "metadata_json", "result_json", "error_json"):
        command[key.removesuffix("_json")] = _decoded(command.pop(key))
    command["approval_required"] = bool(command["approval_required"])
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        events = conn.execute(
            "SELECT id,event_type,state,version,payload_json,created_at FROM jarvis_command_events WHERE command_id=? ORDER BY id",
            (command_id,),
        ).fetchall()
        feedback = conn.execute(
            "SELECT * FROM jarvis_feedback_candidates WHERE command_id=? ORDER BY created_at,id",
            (command_id,),
        ).fetchall()
    finally:
        conn.row_factory = old_factory
    command["events"] = [
        {**{k: event[k] for k in event.keys() if k != "payload_json"}, "payload": _decoded(event["payload_json"])}
        for event in events
    ]
    command["feedback_candidates"] = [
        {
            **{k: item[k] for k in item.keys() if k not in {"candidate_json", "metadata_json"}},
            "candidate": _decoded(item["candidate_json"]),
            "metadata": _decoded(item["metadata_json"]),
            "direct_memory_merge": False,
        }
        for item in feedback
    ]
    return command
