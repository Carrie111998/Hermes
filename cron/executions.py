"""Profile-local durable audit ledger for cron execution attempts.

The ledger records what is known about each attempt; it is not a retry queue.
Interrupted attempts become ``unknown`` only after their exact owner process is
proved gone. Terminal states are immutable.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

# Optional test override. Production resolves the path at transaction time so
# dashboard operations that temporarily enter another profile cannot leak that
# profile's execution records into the import-time home.
EXECUTIONS_FILE: Optional[Path] = None
# Shipped default for the terminal-history retention cap, plus the optional
# module override tests inject through. ``None`` means production should resolve
# the live cap from ``cron.max_terminal_executions`` in config.yaml;
# ``monkeypatch.setattr`` of MAX_TERMINAL_EXECUTIONS to any valid value wins over
# config (#93616), including an explicit override equal to the shipped default.
DEFAULT_MAX_TERMINAL_EXECUTIONS = 1000
MAX_TERMINAL_EXECUTIONS: Optional[int] = None
_SQLITE_MAX_INTEGER = (1 << 63) - 1
_TERMINAL_STATES = ("completed", "failed", "unknown")
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex
_last_retention_error_by_ledger: Dict[str, str] = {}


def _connect() -> sqlite3.Connection:
    from cron.jobs import _ensure_cron_dir

    path = EXECUTIONS_FILE or (get_hermes_home().resolve() / "cron" / "executions.db")
    _ensure_cron_dir(path.parent)
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


def _retention_error(value: Any) -> ValueError:
    return ValueError(
        "cron.max_terminal_executions must be a whole number between 1 and "
        f"{_SQLITE_MAX_INTEGER}, got {value!r}"
    )


def resolve_max_terminal_executions(
    value: Any, *, default: int = DEFAULT_MAX_TERMINAL_EXECUTIONS
) -> int:
    """Validate one configured terminal-history retention cap.

    Accepts integers in SQLite's signed 64-bit range, whole-number floats, and
    digit-only strings. ``None`` means "unset" and validates *default* through
    the same path. Everything else — booleans, zero, negatives, oversized or
    fractional values, empty or non-numeric strings — raises ``ValueError``.
    Pruning callers must treat that as fail-closed (skip the DELETE); silently
    coercing e.g. ``-1`` to ``0`` would wipe the entire terminal ledger, while
    binding a value above SQLite's range would roll back the terminal update.
    """
    candidate = default if value is None else value
    if isinstance(candidate, bool):
        raise _retention_error(candidate)
    if isinstance(candidate, int):
        if not 1 <= candidate <= _SQLITE_MAX_INTEGER:
            raise _retention_error(candidate)
        return candidate
    if isinstance(candidate, float):
        if not candidate.is_integer():
            raise _retention_error(candidate)
        parsed = int(candidate)
        if not 1 <= parsed <= _SQLITE_MAX_INTEGER:
            raise _retention_error(candidate)
        return parsed
    if isinstance(candidate, str):
        stripped = candidate.strip()
        # isdigit() rejects signs, decimals, underscores, and empty strings;
        # int() can still fail on exotic unicode digits isdigit() accepts.
        if not stripped.isdigit():
            raise _retention_error(candidate)
        try:
            parsed = int(stripped)
        except ValueError:
            raise _retention_error(candidate) from None
        if not 1 <= parsed <= _SQLITE_MAX_INTEGER:
            raise _retention_error(candidate)
        return parsed
    raise _retention_error(candidate)


def current_max_terminal_executions() -> int:
    """Resolve the live terminal-history cap for this process.

    Precedence:

    1. ``MAX_TERMINAL_EXECUTIONS``, when a test (or caller) set the optional
       override — preserves the existing
       ``monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", N)``
       injection point.
    2. ``cron.max_terminal_executions`` from the active profile's
       config.yaml, via the read-only config fast path.
    3. :data:`DEFAULT_MAX_TERMINAL_EXECUTIONS`.

    There is deliberately no environment-variable override — behavioral
    settings live in config.yaml. Raises ``ValueError`` when the resolved
    value is invalid or the config loader raises, so pruning callers can fail
    closed instead of deleting with a wrong cap.
    """
    if MAX_TERMINAL_EXECUTIONS is not None:
        return resolve_max_terminal_executions(MAX_TERMINAL_EXECUTIONS)
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    except Exception as exc:
        raise ValueError(
            f"could not read cron.max_terminal_executions from config.yaml: {exc}"
        ) from exc
    cron_config = config.get("cron") if isinstance(config, dict) else None
    if cron_config is None:
        return resolve_max_terminal_executions(None)
    if not isinstance(cron_config, dict):
        raise ValueError(
            "cron section in config.yaml must be a mapping, got "
            f"{type(cron_config).__name__}"
        )
    return resolve_max_terminal_executions(cron_config.get("max_terminal_executions"))


def _prune_unlocked(conn: sqlite3.Connection) -> int:
    """Prune the oldest terminal rows beyond the retention cap.

    In-flight (``claimed``/``running``) rows are never touched. On an
    invalid cap or resolution error this fails closed: it logs and deletes
    nothing rather than pruning with a wrong limit. Returns the number of rows
    deleted.
    """
    database_row = conn.execute("PRAGMA database_list").fetchone()
    ledger_key = str(database_row[2]) if database_row is not None else "<unknown>"
    try:
        limit = current_max_terminal_executions()
    except ValueError as exc:
        detail = str(exc)
        if _last_retention_error_by_ledger.get(ledger_key) != detail:
            logger.error(
                "Skipping cron execution-history prune — %s. Fix "
                "cron.max_terminal_executions in config.yaml to resume pruning.",
                exc,
            )
            _last_retention_error_by_ledger[ledger_key] = detail
        return 0
    _last_retention_error_by_ledger.pop(ledger_key, None)
    cur = conn.execute(
        """DELETE FROM executions WHERE id IN (
             SELECT id FROM executions
             WHERE status IN ('completed','failed','unknown')
             ORDER BY claimed_at DESC, id DESC LIMIT -1 OFFSET ?
           )""",
        (limit,),
    )
    return cur.rowcount


def create_execution(job_id: str, *, source: str) -> Dict[str, Any]:
    """Persist a claimed attempt before executor/provider dispatch."""
    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    with _transaction() as conn:
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
) -> Optional[Dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten."""
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    with _transaction() as conn:
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
