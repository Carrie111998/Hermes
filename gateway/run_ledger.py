"""Durable correlation and lifecycle ledger for ``/v1/runs``."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from hermes_constants import get_hermes_home


TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
ACTIVE_STATUSES = frozenset({"queued", "running", "waiting_for_approval", "stopping"})
RETENTION_SECONDS = 24 * 60 * 60
_ALLOWED_TRANSITIONS = {
    "queued": frozenset({"running", "stopping", *TERMINAL_STATUSES}),
    "running": frozenset({"waiting_for_approval", "stopping", *TERMINAL_STATUSES}),
    "waiting_for_approval": frozenset({"running", "stopping", *TERMINAL_STATUSES}),
    "stopping": TERMINAL_STATUSES,
}


class IdempotencyConflictError(RuntimeError):
    """The same key was reused for a materially different request."""


_lock = threading.RLock()


def transition_allowed(current: Optional[str], requested: str) -> bool:
    """Return whether a lifecycle update is monotonic."""
    if current in TERMINAL_STATUSES:
        return False
    if current is None or current == requested:
        return True
    return requested in _ALLOWED_TRANSITIONS.get(current, frozenset())


def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(conn, db_label="state.db (run_ledger)")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS api_runs (
                run_id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE,
                request_fingerprint TEXT,
                status TEXT NOT NULL,
                data TEXT NOT NULL,
                owner_pid INTEGER,
                owner_started_at INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_runs_updated_at "
            "ON api_runs(status, updated_at)"
        )
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _owner_stamp() -> tuple[int, Optional[int]]:
    pid = os.getpid()
    try:
        from gateway.status import get_process_start_time

        return pid, get_process_start_time(pid)
    except Exception:
        return pid, None


def _owner_is_live(pid: Any, started_at: Any) -> bool:
    if not pid:
        return False
    try:
        pid = int(pid)
        from gateway.status import _pid_exists, get_process_start_time

        if not _pid_exists(pid):
            return False
        if started_at is None:
            return pid == os.getpid()
        current = get_process_start_time(pid)
        return current is not None and int(current) == int(started_at)
    except Exception:
        # Inability to prove owner death must not rewrite live state.
        return True


def _decode(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    data = json.loads(row["data"])
    data["run_id"] = row["run_id"]
    data["status"] = row["status"]
    data["idempotency_key"] = row["idempotency_key"]
    data["created_at"] = row["created_at"]
    data["updated_at"] = row["updated_at"]
    return data


def reserve_run(
    *,
    run_id: str,
    idempotency_key: Optional[str],
    request_fingerprint: Optional[str],
    data: Dict[str, Any],
) -> Tuple[Dict[str, Any], bool]:
    """Atomically reserve a run identity before executor dispatch."""
    now = time.time()
    pid, started_at = _owner_stamp()
    with _transaction() as conn:
        if idempotency_key:
            existing = conn.execute(
                "SELECT * FROM api_runs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise IdempotencyConflictError(idempotency_key)
                return _decode(existing), False

        record = dict(data)
        record.update({
            "object": "hermes.run",
            "run_id": run_id,
            "status": "queued",
            "idempotency_key": idempotency_key,
            "created_at": now,
            "updated_at": now,
        })
        conn.execute(
            """INSERT INTO api_runs
               (run_id, idempotency_key, request_fingerprint, status, data,
                owner_pid, owner_started_at, created_at, updated_at)
               VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)""",
            (
                run_id,
                idempotency_key,
                request_fingerprint,
                json.dumps(record, sort_keys=True, separators=(",", ":"), default=str),
                pid,
                started_at,
                now,
                now,
            ),
        )
        return record, True


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        try:
            return _decode(
                conn.execute(
                    "SELECT * FROM api_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )
        finally:
            conn.close()


def get_run_by_idempotency_key(key: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        try:
            return _decode(
                conn.execute(
                    "SELECT * FROM api_runs WHERE idempotency_key = ?", (key,)
                ).fetchone()
            )
        finally:
            conn.close()


def update_run(run_id: str, status: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Persist one lifecycle transition; terminal rows cannot regress."""
    now = time.time()
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM api_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        if not transition_allowed(row["status"], status):
            return _decode(row)
        data = _decode(row) or {}
        data.update(fields)
        data.update({"status": status, "updated_at": now})
        terminal = status in TERMINAL_STATUSES
        conn.execute(
            """UPDATE api_runs
               SET status = ?, data = ?, owner_pid = ?, owner_started_at = ?, updated_at = ?
               WHERE run_id = ?""",
            (
                status,
                json.dumps(data, sort_keys=True, separators=(",", ":"), default=str),
                None if terminal else row["owner_pid"],
                None if terminal else row["owner_started_at"],
                now,
                run_id,
            ),
        )
        return data


def recover_interrupted_runs(active_run_ids: Optional[Iterable[str]] = None) -> List[str]:
    """Classify rows that have no live owner and adapter task."""
    now = time.time()
    recovered: List[str] = []
    active = None if active_run_ids is None else set(active_run_ids)
    with _transaction() as conn:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        rows = conn.execute(
            f"SELECT * FROM api_runs WHERE status IN ({placeholders})",
            tuple(ACTIVE_STATUSES),
        ).fetchall()
        for row in rows:
            if _owner_is_live(row["owner_pid"], row["owner_started_at"]) and (
                active is None or row["run_id"] in active
            ):
                continue
            data = _decode(row) or {}
            data.update({
                "status": "interrupted",
                "last_event": "run.interrupted",
                "error": "Gateway restarted before the run reached a terminal state.",
                "updated_at": now,
            })
            conn.execute(
                """UPDATE api_runs
                   SET status = 'interrupted', data = ?, owner_pid = NULL,
                       owner_started_at = NULL, updated_at = ?
                   WHERE run_id = ?""",
                (
                    json.dumps(
                        data, sort_keys=True, separators=(",", ":"), default=str
                    ),
                    now,
                    row["run_id"],
                ),
            )
            recovered.append(row["run_id"])
    return recovered


def purge_terminal_runs(now: Optional[float] = None) -> List[str]:
    """Delete terminal correlation rows after the advertised retention window."""
    cutoff = (time.time() if now is None else now) - RETENTION_SECONDS
    with _transaction() as conn:
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        rows = conn.execute(
            f"SELECT run_id FROM api_runs "
            f"WHERE status IN ({placeholders}) AND updated_at < ?",
            (*TERMINAL_STATUSES, cutoff),
        ).fetchall()
        run_ids = [row["run_id"] for row in rows]
        if run_ids:
            delete_placeholders = ",".join("?" for _ in run_ids)
            conn.execute(
                f"DELETE FROM api_runs WHERE run_id IN ({delete_placeholders})",
                tuple(run_ids),
            )
        return run_ids
