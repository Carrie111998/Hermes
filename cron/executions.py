"""Profile-local durable audit ledger for cron execution attempts.

The ledger records what is known about each attempt; it is not a retry queue.
Interrupted attempts become ``unknown`` only after their exact owner process is
proved gone. Terminal states are immutable.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from cron.context import (
    DELIVERY_STATUSES,
    UNKNOWN,
    normalize_delivery_status,
    normalize_invocation_kind,
)
from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

# Optional test override. Production resolves the path at transaction time so
# dashboard operations that temporarily enter another profile cannot leak that
# profile's execution records into the import-time home.
EXECUTIONS_FILE: Optional[Path] = None
MAX_TERMINAL_EXECUTIONS = 1000
_TERMINAL_STATES = ("completed", "failed", "unknown")
_EXECUTION_MIGRATED_COLUMNS = {
    "invocation_kind": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "intended_fire_at": "TEXT",
    "claim_owner": "TEXT",
    "delivery_status": "TEXT NOT NULL DEFAULT 'NOT_ATTEMPTED'",
    "delivery_target": "TEXT",
    "delivery_target_class": "TEXT",
    "delivery_content_sha256": "TEXT",
    "delivery_attempted_at": "TEXT",
    "delivery_completed_at": "TEXT",
    "delivery_error": "TEXT",
    "delivery_receipt_id": "TEXT",
    "delivery_consumption_status": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "output_path": "TEXT",
    "output_sha256": "TEXT",
    "founder_card_path": "TEXT",
    "founder_card_sha256": "TEXT",
}
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex


def _connect() -> sqlite3.Connection:
    path = EXECUTIONS_FILE or (get_hermes_home().resolve() / "cron" / "executions.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path, timeout=5)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    apply_wal_with_fallback(conn, db_label="cron/executions.db")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS executions (
             id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             source TEXT NOT NULL,
             process_id TEXT NOT NULL,
             pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown')),
             claimed_at TEXT NOT NULL,
             started_at TEXT,
             finished_at TEXT,
             error TEXT,
             invocation_kind TEXT NOT NULL DEFAULT 'UNKNOWN',
             intended_fire_at TEXT,
             claim_owner TEXT,
             delivery_status TEXT NOT NULL DEFAULT 'NOT_ATTEMPTED',
             delivery_target TEXT,
             delivery_target_class TEXT,
             delivery_content_sha256 TEXT,
             delivery_attempted_at TEXT,
             delivery_completed_at TEXT,
             delivery_error TEXT,
             delivery_receipt_id TEXT,
             delivery_consumption_status TEXT NOT NULL DEFAULT 'UNKNOWN',
             output_path TEXT,
             output_sha256 TEXT,
             founder_card_path TEXT,
             founder_card_sha256 TEXT
           )"""
    )
    # Additive migration for ledgers created before Phase A.  Existing rows
    # remain byte-for-byte intact in their original columns and receive only
    # explicit fail-closed defaults for the new attestation fields.
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(executions)").fetchall()
    }
    for name, definition in _EXECUTION_MIGRATED_COLUMNS.items():
        if name not in existing_columns:
            conn.execute(f"ALTER TABLE executions ADD COLUMN {name} {definition}")
    conn.execute(
        "UPDATE executions SET invocation_kind='UNKNOWN' "
        "WHERE invocation_kind IS NULL OR invocation_kind=''"
    )
    conn.execute(
        "UPDATE executions SET delivery_status='NOT_ATTEMPTED' "
        "WHERE delivery_status IS NULL OR delivery_status=''"
    )
    conn.execute(
        "UPDATE executions SET delivery_consumption_status='UNKNOWN' "
        "WHERE delivery_consumption_status IS NULL OR delivery_consumption_status=''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
        "ON executions(job_id, claimed_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_status_claimed "
        "ON executions(status, claimed_at DESC, id DESC)"
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, always close.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back
    the transaction; it does not close the connection. Relying on that alone
    leaks a connection (and its WAL/SHM file descriptors) on every call,
    since closing then depends on the garbage collector. Schema init runs
    inside the ``try`` too, so a PRAGMA/DDL failure after a successful
    ``connect()`` still closes the connection instead of leaking it.
    """
    with _lock:
        conn = _connect()
        try:
            _initialize_schema(conn)
            with conn:
                yield conn
        finally:
            conn.close()


def _record(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def _emit_execution_state(
    record: Optional[Dict[str, Any]], *, delivery_outcome: Optional[str] = None
) -> None:
    """Project durable state to monitoring without affecting ledger behavior."""
    try:
        from agent.monitoring.cron_health import emit_execution_state

        emit_execution_state(record, delivery_outcome=delivery_outcome)
    except Exception:
        pass


def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_is_live(pid: int, started_at: Optional[int]) -> bool:
    try:
        from gateway.status import _pid_exists
        if not _pid_exists(pid):
            return False
    except Exception:
        return True  # fail safe: inability to prove death must not rewrite state
    if started_at is None:
        return pid == os.getpid()
    current = _process_start_time(pid)
    return current is not None and current == started_at


def _prune_unlocked(conn: sqlite3.Connection) -> None:
    limit = max(0, int(MAX_TERMINAL_EXECUTIONS))
    conn.execute(
        """DELETE FROM executions WHERE id IN (
             SELECT id FROM executions
             WHERE status IN ('completed','failed','unknown')
             ORDER BY claimed_at DESC, id DESC LIMIT -1 OFFSET ?
           )""",
        (limit,),
    )


def create_execution(
    job_id: str,
    *,
    source: str,
    invocation_kind: str = UNKNOWN,
    intended_fire_at: Optional[str] = None,
    claim_owner: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a claimed attempt before executor/provider dispatch."""
    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    normalized_kind = normalize_invocation_kind(invocation_kind)
    with _transaction() as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at, invocation_kind, intended_fire_at,
                claim_owner, delivery_status, delivery_consumption_status)
               VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?,
                       'NOT_ATTEMPTED', 'UNKNOWN')""",
            (execution_id, str(job_id), str(source), _PROCESS_ID, pid,
             _process_start_time(pid), now, normalized_kind,
             str(intended_fire_at) if intended_fire_at is not None else None,
             str(claim_owner) if claim_owner else None),
        )
        row = conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone()
    record = _record(row)
    _emit_execution_state(record)
    return record  # type: ignore[return-value]


def bind_execution_claim(
    execution_id: str,
    *,
    invocation_kind: str,
    intended_fire_at: Optional[str] = None,
    claim_owner: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Bind the store fire claim to an already-created scheduler row once."""
    normalized_kind = normalize_invocation_kind(invocation_kind)
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET invocation_kind=?, intended_fire_at=?, claim_owner=?
               WHERE id=? AND status='claimed'""",
            (
                normalized_kind,
                str(intended_fire_at) if intended_fire_at is not None else None,
                str(claim_owner) if claim_owner else None,
                execution_id,
            ),
        )
        if cur.rowcount != 1:
            return None
        record = _record(
            conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
        )
    _emit_execution_state(record)
    return record


def mark_execution_running(execution_id: str) -> Optional[Dict[str, Any]]:
    """Transition one claimed attempt to running exactly once."""
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET status='running', started_at=?
               WHERE id=? AND status='claimed'""",
            (now, execution_id),
        )
        if cur.rowcount != 1:
            return None
        record = _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())
    _emit_execution_state(record)
    return record


def finish_execution(
    execution_id: str, *, success: bool, error: Optional[str] = None,
    delivery_outcome: Optional[str] = None,
    delivery_status: Optional[str] = None,
    delivery_target: Optional[str] = None,
    delivery_target_class: Optional[str] = None,
    delivery_content_sha256: Optional[str] = None,
    delivery_attempted_at: Optional[str] = None,
    delivery_completed_at: Optional[str] = None,
    delivery_error: Optional[str] = None,
    delivery_receipt_id: Optional[str] = None,
    output_path: Optional[str] = None,
    output_sha256: Optional[str] = None,
    founder_card_path: Optional[str] = None,
    founder_card_sha256: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten.

    ``delivery_outcome`` remains the legacy monitoring projection.  The
    uppercase ``delivery_status`` and paired evidence fields are the durable
    execution-keyed attestation used by Phase A consumers.
    """
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    normalized_delivery_status = (
        normalize_delivery_status(delivery_status)
        if delivery_status is not None
        else None
    )
    if normalized_delivery_status is not None and normalized_delivery_status not in DELIVERY_STATUSES:
        normalized_delivery_status = "UNKNOWN"
    delivery_detail = (
        str(delivery_error)
        if delivery_error is not None
        else (str(error) if normalized_delivery_status == "FAILED" and error else None)
    )
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET
                 status=?, finished_at=?, error=?,
                 delivery_status=COALESCE(?, delivery_status),
                 delivery_target=COALESCE(?, delivery_target),
                 delivery_target_class=COALESCE(?, delivery_target_class),
                 delivery_content_sha256=COALESCE(?, delivery_content_sha256),
                 delivery_attempted_at=COALESCE(?, delivery_attempted_at),
                 delivery_completed_at=COALESCE(?, delivery_completed_at),
                 delivery_error=COALESCE(?, delivery_error),
                 delivery_receipt_id=COALESCE(?, delivery_receipt_id),
                 output_path=COALESCE(?, output_path),
                 output_sha256=COALESCE(?, output_sha256),
                 founder_card_path=COALESCE(?, founder_card_path),
                 founder_card_sha256=COALESCE(?, founder_card_sha256),
                 delivery_consumption_status=CASE
                   WHEN COALESCE(?, delivery_status)='PROVIDER_ACCEPTED'
                     THEN 'UNKNOWN'
                   ELSE delivery_consumption_status END
               WHERE id=? AND status IN ('claimed','running')""",
            (
                status,
                now,
                detail,
                normalized_delivery_status,
                delivery_target,
                delivery_target_class,
                delivery_content_sha256,
                delivery_attempted_at,
                delivery_completed_at,
                delivery_detail,
                delivery_receipt_id,
                output_path,
                output_sha256,
                founder_card_path,
                founder_card_sha256,
                normalized_delivery_status,
                execution_id,
            ),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        record = _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())
    _emit_execution_state(record, delivery_outcome=delivery_outcome)
    return record


def recover_interrupted_executions() -> int:
    """Mark provably abandoned attempts unknown without scheduling retries."""
    now = _hermes_now().isoformat()
    changed = 0
    recovered: List[Dict[str, Any]] = []
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT id, process_id, pid, process_started_at FROM executions
               WHERE status IN ('claimed','running')"""
        ).fetchall()
        for row in rows:
            if row["process_id"] == _PROCESS_ID:
                continue
            if _owner_is_live(int(row["pid"]), row["process_started_at"]):
                continue
            cur = conn.execute(
                """UPDATE executions SET status='unknown', finished_at=?, error=?,
                          delivery_status='UNKNOWN', delivery_completed_at=?,
                          delivery_error=?
                   WHERE id=? AND status IN ('claimed','running')""",
                (
                    now,
                    "Scheduler restarted after this execution's owner exited before a durable "
                    "terminal state; whether side effects ran is unknown.",
                    now,
                    "Scheduler restarted before delivery outcome was durable.",
                    row["id"],
                ),
            )
            changed += cur.rowcount
            if cur.rowcount:
                record = _record(conn.execute(
                    "SELECT * FROM executions WHERE id=?", (row["id"],)
                ).fetchone())
                if record is not None:
                    recovered.append(record)
        if changed:
            _prune_unlocked(conn)
    for record in recovered:
        _emit_execution_state(record)
    return changed


def list_executions(
    *, job_id: Optional[str] = None, limit: int = 50,
    before_claimed_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return indexed, newest-first execution history with cursor pagination."""
    clauses: List[str] = []
    params: List[Any] = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(str(job_id))
    if before_claimed_at is not None:
        clauses.append("claimed_at < ?")
        params.append(str(before_claimed_at))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM executions" + where
            + " ORDER BY claimed_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def latest_execution(job_id: str) -> Optional[Dict[str, Any]]:
    rows = list_executions(job_id=job_id, limit=1)
    return rows[0] if rows else None


def latest_executions(job_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Load latest execution for many jobs in one indexed query."""
    clean = [str(job_id) for job_id in dict.fromkeys(job_ids) if job_id]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    with _transaction() as conn:
        rows = conn.execute(
            f"""SELECT e.* FROM executions e
                WHERE e.job_id IN ({placeholders})
                  AND e.id=(SELECT e2.id FROM executions e2
                            WHERE e2.job_id=e.job_id
                            ORDER BY e2.claimed_at DESC, e2.id DESC LIMIT 1)""",
            clean,
        ).fetchall()
    return {row["job_id"]: dict(row) for row in rows}
