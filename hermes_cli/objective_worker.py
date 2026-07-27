"""Supervisable standalone worker for the governed objective event loop."""

from __future__ import annotations

import os
import signal
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli import objectives_db


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS objective_workers (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL DEFAULT '__unscoped__',
    role TEXT NOT NULL, pid INTEGER NOT NULL,
    process_nonce TEXT NOT NULL, status TEXT NOT NULL,
    started_at INTEGER NOT NULL, heartbeat_at INTEGER NOT NULL,
    stopped_at INTEGER, last_cycle_status TEXT, last_cycle_at INTEGER,
    last_error TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0,
    circuit_opened_at INTEGER, stop_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_objective_workers_health
    ON objective_workers(status, heartbeat_at);
"""

# Results that indicate a global runtime safety boundary, rather than a
# single-objective outcome.  Every supervisor host must stop on these states.
FAIL_CLOSED_STATUSES = frozenset(
    {
        "paused",
        "disabled",
        "security_blocked",
        "configuration_blocked",
        "runtime_host_blocked",
        "integrity_blocked",
        "runtime_drift_blocked",
        "recovery_blocked",
    }
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction and conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='objective_workers'"
    ).fetchone():
        return
    conn.executescript(SCHEMA_SQL)
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(objective_workers)")
    }
    if "consecutive_failures" not in columns:
        conn.execute(
            "ALTER TABLE objective_workers ADD COLUMN "
            "consecutive_failures INTEGER NOT NULL DEFAULT 0"
        )
    if "circuit_opened_at" not in columns:
        conn.execute(
            "ALTER TABLE objective_workers ADD COLUMN circuit_opened_at INTEGER"
        )
    if "organization_id" not in columns:
        conn.execute(
            "ALTER TABLE objective_workers ADD COLUMN "
            "organization_id TEXT NOT NULL DEFAULT '__unscoped__'"
        )
    if "stop_reason" not in columns:
        conn.execute("ALTER TABLE objective_workers ADD COLUMN stop_reason TEXT")


def register_worker(
    conn: sqlite3.Connection,
    *,
    role: str = "objective-runtime",
    worker_id: Optional[str] = None,
) -> str:
    ensure_schema(conn)
    now = int(time.time())
    resolved = worker_id or f"worker_{uuid.uuid4().hex}"
    from hermes_cli import organization_db

    ceo = organization_db.active_ceo(conn)
    organization_id = (
        str(ceo["organization_id"]) if ceo is not None else "__unscoped__"
    )
    with conn:
        conn.execute(
            """INSERT INTO objective_workers
               (id, organization_id, role, pid, process_nonce, status,
                started_at, heartbeat_at)
               VALUES (?, ?, ?, ?, ?, 'running', ?, ?)""",
            (
                resolved,
                organization_id,
                role,
                os.getpid(),
                uuid.uuid4().hex,
                now,
                now,
            ),
        )
    return resolved


def heartbeat(
    conn: sqlite3.Connection,
    worker_id: str,
    *,
    cycle_status: Optional[str] = None,
    error: Optional[str] = None,
) -> int:
    ensure_schema(conn)
    now = int(time.time())
    with conn:
        updated = conn.execute(
            """UPDATE objective_workers SET heartbeat_at=?,
               last_cycle_status=COALESCE(?,last_cycle_status),
               last_cycle_at=CASE WHEN ? IS NULL THEN last_cycle_at ELSE ? END,
               last_error=?,
               consecutive_failures=CASE
                 WHEN ? IS NOT NULL THEN consecutive_failures + 1
                 WHEN ? IS NOT NULL THEN 0
                 ELSE consecutive_failures END
               WHERE id=? AND status='running'""",
            (
                now,
                cycle_status,
                cycle_status,
                now,
                error,
                error,
                cycle_status,
                worker_id,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("objective worker registration is not active")
    row = conn.execute(
        "SELECT consecutive_failures FROM objective_workers WHERE id=?",
        (worker_id,),
    ).fetchone()
    return int(row["consecutive_failures"])


def touch_worker(conn: sqlite3.Connection, worker_id: str) -> bool:
    """Refresh liveness without changing cycle outcome or failure accounting."""
    ensure_schema(conn)
    with conn:
        updated = conn.execute(
            """UPDATE objective_workers SET heartbeat_at=?
                WHERE id=? AND status='running'""",
            (int(time.time()), worker_id),
        )
    return updated.rowcount == 1


class WorkerHeartbeatKeeper:
    """Keep a supervised worker visibly alive during long blocking cycles."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        worker_id: str,
        *,
        interval_seconds: float = 15,
    ):
        if interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self.worker_id = worker_id
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._failed = threading.Event()
        self._thread: Optional[threading.Thread] = None
        database = next(
            (
                str(row["file"])
                for row in conn.execute("PRAGMA database_list").fetchall()
                if str(row["name"]) == "main"
            ),
            "",
        )
        self._database_path = Path(database) if database else None

    def __enter__(self) -> "WorkerHeartbeatKeeper":
        if self._database_path is None:
            return self

        def maintain() -> None:
            try:
                with objectives_db.connect_closing(self._database_path) as health_conn:
                    while not self._stop.wait(self.interval_seconds):
                        if not touch_worker(health_conn, self.worker_id):
                            self._failed.set()
                            return
            except Exception:
                self._failed.set()

        self._thread = threading.Thread(
            target=maintain,
            name=f"objective-worker-heartbeat-{self.worker_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def assert_healthy(self) -> None:
        if self._failed.is_set():
            raise RuntimeError("objective worker heartbeat persistence failed")

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 2))
        self.assert_healthy()


def open_worker_circuit(
    conn: sqlite3.Connection,
    worker_id: str,
    *,
    role: str,
    error: str,
    failure_threshold: int,
) -> str:
    """Stop a failing worker and create one durable operator handoff."""
    if failure_threshold <= 0:
        raise ValueError("worker failure threshold must be positive")
    ensure_schema(conn)
    now = int(time.time())
    row = conn.execute(
        "SELECT consecutive_failures FROM objective_workers WHERE id=?",
        (worker_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"objective worker not found: {worker_id}")
    failures = int(row["consecutive_failures"])
    with conn:
        conn.execute(
            """UPDATE objective_workers SET status='circuit_open',
               heartbeat_at=?, stopped_at=?, circuit_opened_at=?,
               last_cycle_status='circuit_open', last_error=?
               WHERE id=? AND status='running'""",
            (now, now, now, error, worker_id),
        )
    from hermes_cli import operational_control, organization_db

    ceo = organization_db.active_ceo(conn)
    return operational_control.raise_intervention(
        conn,
        organization_id=(
            str(ceo["organization_id"]) if ceo is not None else None
        ),
        category="objective_runtime_unhealthy",
        summary="Objective runtime stopped after repeated systemic failures",
        context={
            "worker_id": worker_id,
            "role": role,
            "consecutive_failures": failures,
            "failure_threshold": failure_threshold,
            "last_error": error,
        },
        options=[
            {"id": "diagnose", "label": "Diagnose runtime configuration"},
            {"id": "restart", "label": "Restart after remediation"},
            {"id": "manual", "label": "Keep autonomous operation stopped"},
        ],
        dedupe_key=f"objective-runtime-unhealthy:{role}",
    )


def stop_worker(
    conn: sqlite3.Connection,
    worker_id: str,
    *,
    reason: str = "supervisor_exit",
) -> None:
    now = int(time.time())
    with conn:
        conn.execute(
            """UPDATE objective_workers SET status='stopped',
               heartbeat_at=?, stopped_at=?, stop_reason=?
               WHERE id=? AND status='running'""",
            (now, now, reason, worker_id),
        )


def worker_health(
    conn: sqlite3.Connection,
    *,
    stale_after_seconds: int = 60,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    now = int(time.time())
    result = []
    for row in conn.execute(
        "SELECT * FROM objective_workers ORDER BY started_at DESC,id"
    ).fetchall():
        item = dict(row)
        age = max(0, now - int(item["heartbeat_at"]))
        item["heartbeat_age_seconds"] = age
        item["healthy"] = item["status"] == "running" and age <= stale_after_seconds
        item["effective_status"] = (
            "stale"
            if item["status"] == "running" and not item["healthy"]
            else item["status"]
        )
        result.append(item)
    return result


def reconcile_stale_workers(
    conn: sqlite3.Connection,
    *,
    stale_after_seconds: int = 60,
) -> list[str]:
    """Persistently stop workers whose heartbeat lease has expired."""
    if stale_after_seconds <= 0:
        raise ValueError("stale worker threshold must be positive")
    ensure_schema(conn)
    cutoff = int(time.time()) - stale_after_seconds
    rows = conn.execute(
        "SELECT id,organization_id,role FROM objective_workers "
        "WHERE status='running' AND heartbeat_at<? ORDER BY id",
        (cutoff,),
    ).fetchall()
    if not rows:
        return []
    now = int(time.time())
    worker_ids = [str(row["id"]) for row in rows]
    with conn:
        conn.executemany(
            "UPDATE objective_workers SET status='stale', heartbeat_at=?, "
            "stopped_at=?, stop_reason='heartbeat_stale' "
            "WHERE id=? AND status='running'",
            [(now, now, worker_id) for worker_id in worker_ids],
        )
    from hermes_cli import operational_control

    for row in rows:
        operational_control.raise_intervention(
            conn,
            organization_id=str(row["organization_id"]),
            category="objective_worker_stale",
            summary="Supervised objective worker heartbeat expired",
            context={
                "worker_id": str(row["id"]),
                "role": str(row["role"]),
                "stale_after_seconds": stale_after_seconds,
                "stop_reason": "heartbeat_stale",
            },
            options=[
                {"id": "restart", "label": "Restart the supervised worker"},
                {"id": "diagnose", "label": "Diagnose worker host health"},
                {"id": "manual", "label": "Keep autonomous operation stopped"},
            ],
            dedupe_key=f"objective-worker-stale:{row['id']}",
        )
    return worker_ids


def run_forever(
    *,
    db_path: Optional[Path] = None,
    interval_seconds: float = 15,
    tick: Optional[Callable[[], Any]] = None,
    max_cycles: Optional[int] = None,
) -> int:
    """Run until SIGINT/SIGTERM; intended for a real process supervisor."""
    from hermes_cli.authority_store import validate_topology
    from hermes_cli.config import load_config

    if interval_seconds <= 0:
        raise ValueError("worker interval must be positive")
    config = load_config()
    validate_topology(config)
    agentic = config.get("agentic") or {}
    from hermes_cli import runtime_deployment

    # Callback injection is an embedding/testing seam, not an authority
    # bypass. The configured deployment role is validated before either the
    # default tick or an injected callback can run.
    runtime_deployment.validate_worker_role(agentic, "objective-runtime")
    security = agentic.get("security") or {}
    retry = agentic.get("retry_policy") or {}
    failure_threshold = max(
        1, int(security.get("circuit_breaker_failure_threshold", 3) or 3)
    )
    max_backoff_seconds = max(
        interval_seconds,
        float(retry.get("max_backoff_seconds", 900) or 900),
    )
    if tick is None:
        from hermes_cli.objective_service import tick_once

        tick = lambda: tick_once(db_path=db_path)
    stop_requested = threading.Event()
    shutdown_reason = "supervisor_exit"

    def request_stop(signum, frame):
        nonlocal shutdown_reason
        try:
            shutdown_reason = f"signal:{signal.Signals(signum).name}"
        except ValueError:
            shutdown_reason = f"signal:{signum}"
        stop_requested.set()

    previous_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signum] = signal.signal(signum, request_stop)
        except ValueError:
            pass
    cycles = 0
    exit_code = 0
    with objectives_db.connect_closing(db_path) as conn:
        from hermes_cli import operational_control

        reconcile_stale_workers(
            conn,
            stale_after_seconds=max(60, int(interval_seconds * 3)),
        )
        worker_id = register_worker(conn)
        try:
            with WorkerHeartbeatKeeper(conn, worker_id) as heartbeat_keeper:
                while (
                    not stop_requested.is_set()
                    and (max_cycles is None or cycles < max_cycles)
                ):
                    try:
                        heartbeat_keeper.assert_healthy()
                    except RuntimeError:
                        shutdown_reason = "worker_lease_lost"
                        exit_code = 1
                        break
                    consecutive_failures = 0
                    try:
                        # Keep the supervisor itself as a final authority
                        # boundary.  The default tick_once path performs the
                        # same check, but alternate/injected callbacks must not
                        # be able to begin work after an operator pause.
                        operational_control.assert_autonomous(conn)
                        # Re-read deployment authority at the last boundary;
                        # a charter change while this process is alive must
                        # not leave the worker operating under stale host
                        # permissions.
                        current_config = load_config()
                        current_agentic = current_config.get("agentic") or {}
                        runtime_deployment.validate_worker_role(
                            current_agentic, "objective-runtime"
                        )
                        runtime_deployment.assert_current_runtime(
                            conn, current_agentic
                        )
                        outcome = tick()
                        heartbeat_keeper.assert_healthy()
                        status = str(getattr(outcome, "status", "unknown"))
                        reason = str(getattr(outcome, "reason", "") or "")
                        consecutive_failures = heartbeat(
                            conn,
                            worker_id,
                            cycle_status=status,
                            error=(reason if status in FAIL_CLOSED_STATUSES else None),
                        )
                        if status in FAIL_CLOSED_STATUSES:
                            shutdown_reason = (
                                "autonomy_paused"
                                if status == "paused"
                                else f"runtime_blocked:{status}"
                            )
                            break
                    except Exception as exc:
                        try:
                            consecutive_failures = heartbeat(
                                conn,
                                worker_id,
                                cycle_status="worker_error",
                                error=str(exc),
                            )
                        except RuntimeError:
                            shutdown_reason = "worker_lease_lost"
                            exit_code = 1
                            break
                        if operational_control.autonomy_state(conn)["mode"] != "autonomous":
                            shutdown_reason = "autonomy_paused"
                            heartbeat(conn, worker_id, cycle_status="paused")
                            break
                        if consecutive_failures >= failure_threshold:
                            open_worker_circuit(
                                conn,
                                worker_id,
                                role="objective-runtime",
                                error=str(exc),
                                failure_threshold=failure_threshold,
                            )
                            exit_code = 1
                            break
                    cycles += 1
                    if (
                        not stop_requested.is_set()
                        and (max_cycles is None or cycles < max_cycles)
                    ):
                        delay = min(
                            max_backoff_seconds,
                            interval_seconds
                            * (2 ** max(0, consecutive_failures - 1)),
                        )
                        # SIGINT/SIGTERM must wake the worker immediately. A raw
                        # time.sleep here made supervised shutdown wait through
                        # the entire retry backoff (up to 15 minutes).
                        stop_requested.wait(delay)
        finally:
            stop_worker(conn, worker_id, reason=shutdown_reason)
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    return exit_code
