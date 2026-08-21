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
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from hermes_constants import get_default_hermes_root, get_hermes_home
from hermes_time import now as _hermes_now

EXECUTIONS_FILE = get_hermes_home().resolve() / "cron" / "executions.db"
# Global row cap, not per-job. At ~800 attempts/day on a busy profile the old
# 1000 held barely 24h — too short to answer "when did this daily job last
# run", the exact question asked when jobs.json's last_run_at is in doubt.
# 10000 is ~10 days there and still only a few MB.
MAX_TERMINAL_EXECUTIONS = 10000
_TERMINAL_STATES = ("completed", "failed", "unknown")
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex
_MAX_PROBE_ERROR_LENGTH = 500


def _probe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:_MAX_PROBE_ERROR_LENGTH]


def _canonical_hermes_root():
    return get_default_hermes_root()


def _connect_path(path, *, create: bool) -> sqlite3.Connection:
    path = path.resolve()
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        f"file:{path.as_posix()}?mode={'rwc' if create else 'ro'}",
        uri=True,
        timeout=5,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    if create:
        conn.execute("PRAGMA journal_mode=WAL")
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
    return conn


def _connect() -> sqlite3.Connection:
    return _connect_path(EXECUTIONS_FILE, create=True)


def _record(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


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
        # Fail safe: inability to prove death must not rewrite state. Keep that
        # behaviour -- but say so. Silently returning True makes the caller
        # skip recovery, so an interrupted attempt keeps a stale 'running' row
        # forever and jobs.json never learns the run ended, with nothing
        # anywhere recording why. This is also the ONLY branch that can make
        # recover_interrupted_executions() return 0 for a genuinely dead owner
        # (a recycled PID is rejected by the process_started_at comparison
        # below, which is centisecond-resolution), so it is the first thing to
        # look for when that count is unexpectedly 0.
        logger.warning(
            "Could not probe liveness of pid %s; treating its execution as "
            "still owned and skipping recovery (fail-safe).",
            pid,
            exc_info=True,
        )
        return True
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


def create_execution(job_id: str, *, source: str) -> Dict[str, Any]:
    """Persist a claimed attempt before executor/provider dispatch."""
    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    with _lock, _connect() as conn:
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
    return _record(row)  # type: ignore[return-value]


def mark_execution_running(execution_id: str) -> Optional[Dict[str, Any]]:
    """Transition one claimed attempt to running exactly once."""
    now = _hermes_now().isoformat()
    with _lock, _connect() as conn:
        cur = conn.execute(
            """UPDATE executions SET status='running', started_at=?
               WHERE id=? AND status='claimed'""",
            (now, execution_id),
        )
        if cur.rowcount != 1:
            return None
        return _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())


def finish_execution(
    execution_id: str, *, success: bool, error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten."""
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    with _lock, _connect() as conn:
        cur = conn.execute(
            """UPDATE executions SET status=?, finished_at=?, error=?
               WHERE id=? AND status IN ('claimed','running')""",
            (status, now, detail, execution_id),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        return _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())


def amend_execution_after_abandon(
    execution_id: str,
    *,
    abandon_error: str,
    success: bool,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Replace only this execution's provisional deadline verdict.

    Terminal rows are otherwise immutable. This narrow compare-and-swap
    is the exception because the soft deadline explicitly leaves its
    worker running: ``failed`` means "still running at the deadline",
    not a known terminal outcome. Exact error matching prevents a late
    worker from rewriting any later or independently reached failure.
    """
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    with _lock, _connect() as conn:
        cur = conn.execute(
            """UPDATE executions SET status=?, finished_at=?, error=?
               WHERE id=? AND status='failed' AND error=?""",
            (status, now, detail, execution_id, abandon_error),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        return _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())


def recover_interrupted_executions() -> int:
    """Mark provably abandoned attempts unknown without scheduling retries."""
    return len(recover_interrupted_execution_records())


def recover_interrupted_execution_records() -> List[Dict[str, Any]]:
    """Recover abandoned attempts and return the rows that were recovered.

    Callers need the rows, not just a count, to carry the verdict onto records
    this module must not know about — jobs.json's ``last_run_at`` /
    ``last_status``, which are otherwise written only by the owning process at
    the end of a run and are therefore lost when that process dies. The ledger
    stays storage-only; the scheduler layer does the propagating.
    """
    now = _hermes_now().isoformat()
    recovered: List[Dict[str, Any]] = []
    with _lock, _connect() as conn:
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
            if cur.rowcount:
                recovered.append(_record(conn.execute(
                    "SELECT * FROM executions WHERE id=?", (row["id"],)
                ).fetchone()))
        if recovered:
            # Materialized above, so pruning cannot strip a row from the return.
            _prune_unlocked(conn)
    return recovered


def _classify_nonterminal_rows(rows) -> List[Dict[str, Any]]:
    census: List[Dict[str, Any]] = []
    for sqlite_row in rows:
        row = dict(sqlite_row)
        pid = int(row["pid"])
        recorded_start = row["process_started_at"]
        evidence: Dict[str, Any] = {
            "process_id": row["process_id"],
            "pid": pid,
            "process_started_at": recorded_start,
        }
        try:
            from gateway.status import _pid_exists

            pid_exists = _pid_exists(pid)
        except Exception as exc:
            evidence["reason"] = "pid_probe_error"
            evidence["probe_error"] = _probe_error(exc)
            liveness = "unprovable"
        else:
            evidence["pid_exists"] = pid_exists
            if pid_exists is None:
                evidence["reason"] = "pid_probe_indeterminate"
                liveness = "unprovable"
            elif not pid_exists:
                evidence["reason"] = "pid_not_found"
                liveness = "dead"
            elif recorded_start is None:
                evidence["reason"] = "recorded_process_start_time_missing"
                liveness = "unprovable"
            else:
                try:
                    observed_start = _process_start_time(pid)
                except Exception as exc:
                    evidence["reason"] = "process_start_time_probe_error"
                    evidence["probe_error"] = _probe_error(exc)
                    liveness = "unprovable"
                else:
                    evidence["observed_process_started_at"] = observed_start
                    if observed_start is None:
                        evidence["reason"] = "observed_process_start_time_unavailable"
                        liveness = "unprovable"
                    elif observed_start == recorded_start:
                        evidence["reason"] = "process_start_time_matches"
                        liveness = "live"
                    else:
                        evidence["reason"] = "process_start_time_mismatch"
                        liveness = "dead"
        row["owner_liveness"] = liveness
        row["owner_liveness_evidence"] = evidence
        census.append(row)
    return census


def _nonterminal_execution_census_path(path, *, create: bool) -> List[Dict[str, Any]]:
    with _lock, _connect_path(path, create=create) as conn:
        rows = conn.execute(
            """SELECT * FROM executions
               WHERE status IN ('claimed','running')
               ORDER BY claimed_at, id"""
        ).fetchall()
    return _classify_nonterminal_rows(rows)


def nonterminal_execution_census() -> List[Dict[str, Any]]:
    """Return every claimed/running row in this profile's execution ledger."""
    return _nonterminal_execution_census_path(EXECUTIONS_FILE, create=True)


def _cross_profile_execution_ledgers() -> tuple:
    root = _canonical_hermes_root().resolve()
    candidates = [root / "cron" / "executions.db"]
    profiles = root / "profiles"
    if profiles.is_dir():
        candidates.extend(
            profile / "cron" / "executions.db"
            for profile in sorted(profiles.iterdir(), key=lambda path: path.name)
            if profile.is_dir()
        )
    return tuple(path.resolve() for path in candidates if path.is_file())


def cross_profile_nonterminal_execution_census() -> List[Dict[str, Any]]:
    """Return a read-only, lossless census from every existing profile ledger."""
    rows: List[Dict[str, Any]] = []
    for ledger in _cross_profile_execution_ledgers():
        for row in _nonterminal_execution_census_path(ledger, create=False):
            row["execution_ledger"] = str(ledger)
            rows.append(row)
    rows.sort(key=lambda row: (row["claimed_at"], row["id"], row["execution_ledger"]))
    return rows


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
    with _lock, _connect() as conn:
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
    with _lock, _connect() as conn:
        rows = conn.execute(
            f"""SELECT e.* FROM executions e
                WHERE e.job_id IN ({placeholders})
                  AND e.id=(SELECT e2.id FROM executions e2
                            WHERE e2.job_id=e.job_id
                            ORDER BY e2.claimed_at DESC, e2.id DESC LIMIT 1)""",
            clean,
        ).fetchall()
    return {row["job_id"]: dict(row) for row in rows}
