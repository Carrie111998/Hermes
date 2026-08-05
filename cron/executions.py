"""Profile-local durable ledger for cron execution and delivery attempts.

Delivery state machine (all transitions are committed in SQLite):

``pending -> delivering -> delivered``
``pending -> delivering -> retry_wait -> delivering`` (bounded retries)
``delivering -> exhausted`` (permanent failure or retry limit)
``pending|retry_wait -> exhausted`` (job cancellation)
``delivering -> unknown`` (only after its exact owner is proved dead)

``delivered``, ``exhausted``, and ``unknown`` are terminal. An interrupted
network send is deliberately ``unknown`` and is never retried because the
platform may have accepted it before the process died.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, cast

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

EXECUTIONS_FILE = get_hermes_home().resolve() / "cron" / "executions.db"
_IMPORT_EXECUTIONS_FILE = EXECUTIONS_FILE
MAX_TERMINAL_EXECUTIONS = 1000
MAX_DELIVERY_ATTEMPTS = 4
# The built-in scheduler wakes every 60 seconds, so a smaller first delay would
# promise timing it cannot deliver. These are minimum delays; Chronos retries
# only at its next warm callback/startup reconciliation point.
DELIVERY_RETRY_DELAYS_SECONDS = (60, 120, 600)
_TERMINAL_STATES = ("completed", "failed", "unknown")
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex


def _current_executions_file():
    """Resolve the active profile's ledger while honoring test overrides."""
    if EXECUTIONS_FILE != _IMPORT_EXECUTIONS_FILE:
        return EXECUTIONS_FILE
    return get_hermes_home().resolve() / "cron" / "executions.db"


def _connect() -> sqlite3.Connection:
    executions_file = _current_executions_file()
    executions_file.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(executions_file, timeout=5)


def _delivery_now():
    """Use UTC for lexically sortable retry timestamps across DST changes."""
    return _hermes_now().astimezone(timezone.utc)


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
             error TEXT
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
        "ON executions(job_id, claimed_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_status_claimed "
        "ON executions(status, claimed_at DESC, id DESC)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS deliveries (
             execution_id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             job_json TEXT,
             content TEXT,
             targets_json TEXT NOT NULL,
             status TEXT NOT NULL CHECK(status IN
               ('pending','delivering','retry_wait','delivered','exhausted','unknown')),
             attempt_count INTEGER NOT NULL DEFAULT 0,
             next_attempt_at TEXT,
             last_error TEXT,
             permanent_error TEXT,
             terminal_reason TEXT,
             claim_token TEXT,
             claim_process_id TEXT,
             claim_pid INTEGER,
             claim_process_started_at INTEGER,
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL
           )"""
    )
    # Additive migration for databases created by an earlier build of this
    # branch. No table rewrite and no change to the existing status CHECK.
    delivery_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(deliveries)").fetchall()
    }
    for column, ddl in (
        ("permanent_error", "TEXT"),
        ("terminal_reason", "TEXT"),
        ("claim_token", "TEXT"),
        ("claim_process_id", "TEXT"),
        ("claim_pid", "INTEGER"),
        ("claim_process_started_at", "INTEGER"),
    ):
        if column not in delivery_columns:
            conn.execute(f"ALTER TABLE deliveries ADD COLUMN {column} {ddl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_deliveries_due "
        "ON deliveries(status, next_attempt_at, created_at)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS delivery_attempts (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             execution_id TEXT NOT NULL,
             attempt_number INTEGER NOT NULL,
             targets_json TEXT NOT NULL,
             started_at TEXT NOT NULL,
             finished_at TEXT,
             outcome TEXT,
             error TEXT,
             UNIQUE(execution_id, attempt_number)
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_delivery_attempts_execution "
        "ON delivery_attempts(execution_id, attempt_number)"
    )


@contextmanager
def _transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """Open one explicit SQLite transaction and always close its connection.

    Schema setup/migration is committed first. Write-side state transitions use
    ``BEGIN IMMEDIATE`` so the read/compare/update sequence owns SQLite's write
    reservation before it observes state; a second process waits, then sees the
    committed winner and cannot claim the same delivery. Read-only callers use
    a deferred transaction. Commit/rollback is explicit and the connection is
    closed on every path.
    """
    with _lock:
        conn = _connect()
        try:
            _initialize_schema(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            conn.close()


def _record(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def _delivery_record(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    record = dict(row)
    job_json = cast(Optional[str], record.pop("job_json", None))
    targets_json = cast(str, record.pop("targets_json", "[]"))
    record["job"] = json.loads(job_json) if job_json else None
    record["targets"] = json.loads(targets_json or "[]")
    return record


def _attempt_record(row: sqlite3.Row) -> Dict[str, Any]:
    record = dict(row)
    targets_json = cast(str, record.pop("targets_json", "[]"))
    record["targets"] = json.loads(targets_json or "[]")
    return record


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
        # The PID exists but its start-time identity is unavailable. That is
        # insufficient evidence to declare the owner dead; fail closed even
        # when the live PID belongs to another scheduler process.
        return True
    current = _process_start_time(pid)
    return current is not None and current == started_at


def _prune_unlocked(conn: sqlite3.Connection) -> None:
    limit = max(0, int(MAX_TERMINAL_EXECUTIONS))
    rows = conn.execute(
        """SELECT id FROM executions e
           WHERE status IN ('completed','failed','unknown')
             AND NOT EXISTS (
               SELECT 1 FROM deliveries d
               WHERE d.execution_id=e.id
                 AND d.status IN ('pending','delivering','retry_wait')
             )
           ORDER BY claimed_at DESC, id DESC LIMIT -1 OFFSET ?""",
        (limit,),
    ).fetchall()
    stale_ids = [row["id"] for row in rows]
    if not stale_ids:
        return
    placeholders = ",".join("?" for _ in stale_ids)
    conn.execute(
        f"DELETE FROM delivery_attempts WHERE execution_id IN ({placeholders})",
        stale_ids,
    )
    conn.execute(
        f"DELETE FROM deliveries WHERE execution_id IN ({placeholders})",
        stale_ids,
    )
    conn.execute(
        f"DELETE FROM executions WHERE id IN ({placeholders})",
        stale_ids,
    )


def create_execution(job_id: str, *, source: str) -> Dict[str, Any]:
    """Persist a claimed attempt before executor/provider dispatch."""
    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    with _transaction(immediate=True) as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at)
               VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?)""",
            (execution_id, str(job_id), str(source), _PROCESS_ID, pid,
             _process_start_time(pid), now),
        )
        row = conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone()
    record = _record(row)
    _emit_execution_state(record)
    return record  # type: ignore[return-value]


def mark_execution_running(execution_id: str) -> Optional[Dict[str, Any]]:
    """Transition one claimed attempt to running exactly once."""
    now = _hermes_now().isoformat()
    with _transaction(immediate=True) as conn:
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
) -> Optional[Dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten."""
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    with _transaction(immediate=True) as conn:
        cur = conn.execute(
            """UPDATE executions SET status=?, finished_at=?, error=?
               WHERE id=? AND status IN ('claimed','running')""",
            (status, now, detail, execution_id),
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
    now = _delivery_now().isoformat()
    changed = 0
    recovered: List[Dict[str, Any]] = []
    with _transaction(immediate=True) as conn:
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
                """UPDATE executions SET status='unknown', finished_at=?, error=?
                   WHERE id=? AND status IN ('claimed','running')""",
                (now,
                 "Scheduler restarted after this execution's owner exited before a durable "
                 "terminal state; whether side effects ran is unknown.",
                 row["id"]),
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


def enqueue_delivery(
    execution_id: str,
    *,
    job: Dict[str, Any],
    content: str,
    targets: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Persist an exact post-execution delivery before any network send.

    The execution id is the idempotency key. A repeated prepare returns the
    existing record instead of replacing an in-flight or terminal outcome.
    """
    now = _delivery_now().isoformat()
    job_id = str(job.get("id") or "")
    if not job_id:
        raise ValueError("delivery job snapshot requires an id")
    if not targets:
        raise ValueError("delivery requires at least one concrete target")
    with _transaction(immediate=True) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO deliveries
               (execution_id, job_id, job_json, content, targets_json, status,
                attempt_count, next_attempt_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)""",
            (
                str(execution_id),
                job_id,
                json.dumps(job, separators=(",", ":"), sort_keys=True),
                str(content),
                json.dumps(targets, separators=(",", ":"), sort_keys=True),
                now,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM deliveries WHERE execution_id=?", (str(execution_id),)
        ).fetchone()
    return _delivery_record(row)  # type: ignore[return-value]


def get_delivery(execution_id: str) -> Optional[Dict[str, Any]]:
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM deliveries WHERE execution_id=?", (str(execution_id),)
        ).fetchone()
    return _delivery_record(row)


def list_due_deliveries(*, limit: int = 50) -> List[Dict[str, Any]]:
    """Return delivery batches whose network attempt is safe to claim now."""
    now = _delivery_now().isoformat()
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT * FROM deliveries
               WHERE status IN ('pending','retry_wait')
                 AND next_attempt_at <= ?
               ORDER BY next_attempt_at, created_at
               LIMIT ?""",
            (now, max(1, min(int(limit), 500))),
        ).fetchall()
    return [_delivery_record(row) for row in rows]  # type: ignore[misc]


def list_pending_deliveries(*, limit: int = 500) -> List[Dict[str, Any]]:
    """Return every safely cancellable queued/backoff batch, due or future."""
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT * FROM deliveries
               WHERE status IN ('pending','retry_wait')
               ORDER BY created_at LIMIT ?""",
            (max(1, min(int(limit), 5000)),),
        ).fetchall()
    return [_delivery_record(row) for row in rows]  # type: ignore[misc]


def claim_delivery(execution_id: str) -> Optional[Dict[str, Any]]:
    """Atomically claim one due batch and begin its append-only attempt row.

    The ``BEGIN IMMEDIATE`` boundary covers due-state read, compare/update, and
    attempt insertion. The returned token is required to finish the attempt.
    """
    now = _delivery_now().isoformat()
    claim_token = uuid.uuid4().hex
    pid = os.getpid()
    process_started_at = _process_start_time(pid)
    with _transaction(immediate=True) as conn:
        row = conn.execute(
            "SELECT * FROM deliveries WHERE execution_id=?",
            (str(execution_id),),
        ).fetchone()
        if (
            row is None
            or row["status"] not in ("pending", "retry_wait")
            or not row["next_attempt_at"]
            or row["next_attempt_at"] > now
        ):
            return None
        attempt_number = int(row["attempt_count"]) + 1
        cur = conn.execute(
            """UPDATE deliveries
               SET status='delivering', attempt_count=?, updated_at=?,
                   claim_token=?, claim_process_id=?, claim_pid=?,
                   claim_process_started_at=?
               WHERE execution_id=? AND status=? AND attempt_count=?""",
            (
                attempt_number,
                now,
                claim_token,
                _PROCESS_ID,
                pid,
                process_started_at,
                str(execution_id),
                row["status"],
                row["attempt_count"],
            ),
        )
        if cur.rowcount != 1:
            return None
        conn.execute(
            """INSERT INTO delivery_attempts
               (execution_id, attempt_number, targets_json, started_at)
               VALUES (?, ?, ?, ?)""",
            (str(execution_id), attempt_number, row["targets_json"], now),
        )
        claimed = conn.execute(
            "SELECT * FROM deliveries WHERE execution_id=?", (str(execution_id),)
        ).fetchone()
    return _delivery_record(claimed)


def finish_delivery_attempt(
    execution_id: str,
    *,
    claim_token: str,
    failed_targets: List[Dict[str, Any]],
    error: Optional[str] = None,
    permanent_error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Finish the exact claimed attempt and either retire or reschedule it."""
    now_dt = _delivery_now()
    now = now_dt.isoformat()
    with _transaction(immediate=True) as conn:
        row = conn.execute(
            """SELECT * FROM deliveries
               WHERE execution_id=? AND status='delivering' AND claim_token=?""",
            (str(execution_id), str(claim_token)),
        ).fetchone()
        if row is None:
            return None
        attempt_number = int(row["attempt_count"])
        detail = str(error) if error else None
        stored_permanent = str(row["permanent_error"] or "").strip()
        new_permanent = str(permanent_error or "").strip()
        permanent_parts = [part for part in (stored_permanent, new_permanent) if part]
        combined_permanent = "; ".join(dict.fromkeys(permanent_parts)) or None
        terminal_reason = None
        if failed_targets and attempt_number < MAX_DELIVERY_ATTEMPTS:
            status = "retry_wait"
            delay_index = min(
                attempt_number - 1, len(DELIVERY_RETRY_DELAYS_SECONDS) - 1
            )
            next_attempt_at = (
                now_dt + timedelta(seconds=DELIVERY_RETRY_DELAYS_SECONDS[delay_index])
            ).isoformat()
        elif failed_targets:
            status = "exhausted"
            terminal_reason = "max_attempts"
            next_attempt_at = None
            detail = detail or "delivery attempts exhausted"
        elif combined_permanent:
            status = "exhausted"
            terminal_reason = "permanent_failure"
            next_attempt_at = None
            detail = combined_permanent
        else:
            status = "delivered"
            next_attempt_at = None

        terminal = status in ("delivered", "exhausted")
        targets_json = json.dumps(
            [] if terminal else failed_targets,
            separators=(",", ":"),
            sort_keys=True,
        )
        conn.execute(
            """UPDATE deliveries
               SET status=?, targets_json=?, next_attempt_at=?, last_error=?,
                   permanent_error=?, terminal_reason=?, job_json=?, content=?,
                   claim_token=NULL, claim_process_id=NULL, claim_pid=NULL,
                   claim_process_started_at=NULL, updated_at=?
               WHERE execution_id=? AND status='delivering' AND claim_token=?""",
            (
                status,
                targets_json,
                next_attempt_at,
                detail,
                combined_permanent,
                terminal_reason,
                None if terminal else row["job_json"],
                None if terminal else row["content"],
                now,
                str(execution_id),
                str(claim_token),
            ),
        )
        conn.execute(
            """UPDATE delivery_attempts
               SET finished_at=?, outcome=?, error=?
               WHERE execution_id=? AND attempt_number=? AND finished_at IS NULL""",
            (now, status, detail, str(execution_id), attempt_number),
        )
        finished = conn.execute(
            "SELECT * FROM deliveries WHERE execution_id=?", (str(execution_id),)
        ).fetchone()
    return _delivery_record(finished)


def cancel_delivery(
    execution_id: str,
    *,
    reason: str,
    claim_token: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Cancel an unsent batch and scrub its sensitive retry payload.

    Pending/backoff rows need no token. A worker that already claimed a row may
    cancel only with its own token, after re-checking job state before network
    I/O. Other processes cannot cancel somebody else's in-flight send.
    """
    now = _delivery_now().isoformat()
    with _transaction(immediate=True) as conn:
        if claim_token is None:
            where = "execution_id=? AND status IN ('pending','retry_wait')"
            params = (str(execution_id),)
        else:
            where = "execution_id=? AND status='delivering' AND claim_token=?"
            params = (str(execution_id), str(claim_token))
        row = conn.execute(f"SELECT * FROM deliveries WHERE {where}", params).fetchone()
        if row is None:
            return None
        conn.execute(
            f"""UPDATE deliveries
                SET status='exhausted', terminal_reason='cancelled',
                    targets_json='[]', next_attempt_at=NULL, last_error=?,
                    job_json=NULL, content=NULL, claim_token=NULL,
                    claim_process_id=NULL, claim_pid=NULL,
                    claim_process_started_at=NULL, updated_at=?
                WHERE {where}""",
            (str(reason), now, *params),
        )
        if row["status"] == "delivering":
            conn.execute(
                """UPDATE delivery_attempts
                   SET finished_at=?, outcome='exhausted', error=?
                   WHERE execution_id=? AND attempt_number=? AND finished_at IS NULL""",
                (now, str(reason), str(execution_id), row["attempt_count"]),
            )
        cancelled = conn.execute(
            "SELECT * FROM deliveries WHERE execution_id=?", (str(execution_id),)
        ).fetchone()
    return _delivery_record(cancelled)


def recover_interrupted_deliveries() -> int:
    """Mark only provably dead delivery owners unknown; never requeue them."""
    now = _delivery_now().isoformat()
    error = (
        "Scheduler restarted while delivery was in flight; whether the target "
        "received it is unknown, so Hermes will not retry automatically."
    )
    changed = 0
    with _transaction(immediate=True) as conn:
        rows = conn.execute(
            """SELECT execution_id, attempt_count, claim_process_id, claim_pid,
                      claim_process_started_at FROM deliveries """
            "WHERE status='delivering'"
        ).fetchall()
        for row in rows:
            if row["claim_process_id"] == _PROCESS_ID:
                continue
            if row["claim_pid"] is not None and _owner_is_live(
                int(row["claim_pid"]), row["claim_process_started_at"]
            ):
                continue
            cur = conn.execute(
                """UPDATE deliveries
                   SET status='unknown', targets_json='[]', next_attempt_at=NULL,
                       last_error=?, terminal_reason='ambiguous', job_json=NULL,
                       content=NULL, claim_token=NULL, claim_process_id=NULL,
                       claim_pid=NULL, claim_process_started_at=NULL, updated_at=?
                   WHERE execution_id=? AND status='delivering'""",
                (error, now, row["execution_id"]),
            )
            if cur.rowcount != 1:
                continue
            changed += 1
            conn.execute(
                """UPDATE delivery_attempts
                   SET finished_at=?, outcome='unknown', error=?
                   WHERE execution_id=? AND attempt_number=? AND finished_at IS NULL""",
                (now, error, row["execution_id"], row["attempt_count"]),
            )
    return changed


def list_delivery_attempts(execution_id: str) -> List[Dict[str, Any]]:
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT * FROM delivery_attempts WHERE execution_id=?
               ORDER BY attempt_number""",
            (str(execution_id),),
        ).fetchall()
    return [_attempt_record(row) for row in rows]


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
