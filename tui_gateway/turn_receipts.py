"""Durable, fenced turn receipts for the authenticated TUI gateway protocol.

A client first asks the gateway to prepare a turn for a durable session, then
submits that server-issued id.  SQLite compare-and-swap admission makes replay
safe across reconnects and processes; terminal receipts are persisted before
the corresponding stream event is emitted.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_DB_DIR = "desktop"
_DB_FILE = "turn_receipts.db"
_PROCESS_INSTANCE = uuid.uuid4().hex
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: set[str] = set()
_TERMINAL = frozenset({"committed", "failed", "interrupted"})


def _db_path(home: Path | str) -> Path:
    return Path(home) / _DB_DIR / _DB_FILE


def _connect(home: Path | str) -> sqlite3.Connection:
    path = _db_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    key = str(path.resolve())
    if key not in _SCHEMA_READY:
        with _SCHEMA_LOCK:
            if key not in _SCHEMA_READY:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS turn_receipts (
                        turn_id TEXT PRIMARY KEY,
                        session_key TEXT NOT NULL,
                        state TEXT NOT NULL,
                        execution_token TEXT,
                        owner_pid INTEGER,
                        owner_instance TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        receipt_json TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_turn_receipts_session "
                    "ON turn_receipts(session_key, created_at)"
                )
                conn.commit()
                _SCHEMA_READY.add(key)
    return conn


def _public(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {"known": False, "state": "unknown"}
    state = str(row["state"])
    public_state = state
    if state == "prepared":
        public_state = "did_not_run"
    elif state == "running" and row["owner_instance"] != _PROCESS_INSTANCE:
        # Admission is known, but a different backend process cannot prove how
        # the previous execution ended.  Never collapse this into did_not_run.
        public_state = "in_doubt"
    payload: dict[str, Any] = {
        "known": True,
        "state": public_state,
        "turn_id": str(row["turn_id"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }
    raw = row["receipt_json"]
    if raw:
        try:
            receipt = json.loads(raw)
        except (TypeError, ValueError):
            receipt = None
        if isinstance(receipt, dict):
            payload["receipt"] = receipt
    return payload


def prepare_turn(home: Path | str, session_key: str) -> dict[str, Any]:
    """Mint a server-owned turn id bound to ``session_key``."""
    if not session_key:
        raise ValueError("session_key required")
    turn_id = uuid.uuid4().hex
    now = time.time()
    with _connect(home) as conn:
        conn.execute(
            "INSERT INTO turn_receipts "
            "(turn_id, session_key, state, created_at, updated_at) "
            "VALUES (?, ?, 'prepared', ?, ?)",
            (turn_id, session_key, now, now),
        )
    return {"known": True, "state": "did_not_run", "turn_id": turn_id, "created_at": now, "updated_at": now}


def get_turn_status(home: Path | str, session_key: str, turn_id: str) -> dict[str, Any]:
    """Return only receipts bound to the authenticated durable session."""
    if not session_key or not turn_id:
        return {"known": False, "state": "unknown"}
    with _connect(home) as conn:
        row = conn.execute(
            "SELECT * FROM turn_receipts WHERE turn_id = ? AND session_key = ?",
            (turn_id, session_key),
        ).fetchone()
    return _public(row)


def claim_turn(home: Path | str, session_key: str, turn_id: str) -> tuple[str | None, dict[str, Any]]:
    """Atomically admit a prepared turn once and return its fence token."""
    token = uuid.uuid4().hex
    now = time.time()
    conn = _connect(home)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM turn_receipts WHERE turn_id = ? AND session_key = ?",
            (turn_id, session_key),
        ).fetchone()
        if row is None or row["state"] != "prepared":
            conn.rollback()
            return None, _public(row)
        changed = conn.execute(
            "UPDATE turn_receipts SET state = 'running', execution_token = ?, "
            "owner_pid = ?, owner_instance = ?, updated_at = ? "
            "WHERE turn_id = ? AND session_key = ? AND state = 'prepared'",
            (token, os.getpid(), _PROCESS_INSTANCE, now, turn_id, session_key),
        ).rowcount
        if changed != 1:
            conn.rollback()
            row = conn.execute(
                "SELECT * FROM turn_receipts WHERE turn_id = ? AND session_key = ?",
                (turn_id, session_key),
            ).fetchone()
            return None, _public(row)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM turn_receipts WHERE turn_id = ? AND session_key = ?",
            (turn_id, session_key),
        ).fetchone()
        return token, _public(row)
    finally:
        conn.close()


def finish_turn(
    home: Path | str,
    session_key: str,
    turn_id: str,
    execution_token: str,
    state: str,
    receipt: dict[str, Any] | None = None,
) -> bool:
    """Persist one immutable terminal receipt when the execution fence matches."""
    if state not in _TERMINAL:
        raise ValueError(f"invalid terminal turn state: {state}")
    now = time.time()
    receipt_payload = dict(receipt or {})
    receipt_payload.update({"state": state, "turn_id": turn_id, "recorded_at": now})
    encoded = json.dumps(receipt_payload, ensure_ascii=False, separators=(",", ":"))
    with _connect(home) as conn:
        changed = conn.execute(
            "UPDATE turn_receipts SET state = ?, updated_at = ?, receipt_json = ? "
            "WHERE turn_id = ? AND session_key = ? AND state = 'running' "
            "AND execution_token = ?",
            (state, now, encoded, turn_id, session_key, execution_token),
        ).rowcount
    return changed == 1
