"""Durable, idempotent activation ledger.

EventBus delivery is at-least-once, so the same mailbox message arrives more
than once. A claim is what makes activation happen exactly once per
``(message_key, activity_id)``.

The ledger deliberately distinguishes two states that a naive "have I seen
this?" check conflates:

* ``claimed``   — someone took the work and may still be doing it. Reclaimable
  only after ``lease_seconds``, so a process that died mid-run does not strand
  the message forever.
* ``completed`` — the work ran. Never reclaimable, at any age. Without this a
  long-lived message would be re-dispatched every time its lease lapsed.

Writes use ``BEGIN IMMEDIATE`` so the read-decide-write of a claim is atomic
across processes; the scheduler, the dispatcher, and the reconciler all race
for the same rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS activations (
    message_key   TEXT NOT NULL,
    activity_id   TEXT NOT NULL,
    correlation_id TEXT,
    state         TEXT NOT NULL,
    claimed_at    REAL,
    completed_at  REAL,
    outcome       TEXT,
    PRIMARY KEY (message_key, activity_id)
);
CREATE INDEX IF NOT EXISTS activations_pending
    ON activations (activity_id, state);
"""

DEFAULT_LEASE_SECONDS = 900


def default_ledger_path() -> Path:
    """The one ledger the dispatcher and the reconciler must both use.

    If these ever diverge the reconciler re-dispatches work the subscriber
    already claimed — duplicate model calls with no error anywhere. Resolved
    from the canonical Hermes root (not the profile) because activation is
    cross-profile, matching the telemetry store.
    """
    try:
        from hermes_constants import get_default_hermes_root

        root = Path(get_default_hermes_root())
    except Exception:
        root = Path.home() / ".hermes"
    return root / "telemetry" / "jobflow_dispatch.db"


@dataclass(frozen=True)
class ActivationRow:
    message_key: str
    activity_id: str
    correlation_id: str | None
    state: str
    claimed_at: float | None
    completed_at: float | None
    outcome: str | None


def _identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("now must be a numeric timestamp")
    return float(value)


class ActivationStore:
    def __init__(self, db_path: Path, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds
        self._local = threading.local()
        self._write_lock = threading.Lock()
        conn = self._get_conn()
        with self._write_lock:
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA journal_size_limit=33554432")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
            self._local.conn = conn
        return conn

    def claim(
        self,
        message_key: Any,
        activity_id: Any,
        *,
        now: Any,
        correlation_id: str | None = None,
    ) -> bool:
        """Take the work if nobody holds it. True means "you own this"."""
        key = _identity(message_key, "message_key")
        activity = _identity(activity_id, "activity_id")
        stamp = _timestamp(now)

        conn = self._get_conn()
        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT state, claimed_at FROM activations "
                    "WHERE message_key = ? AND activity_id = ?",
                    (key, activity),
                ).fetchone()

                if row is not None:
                    if row["state"] == "completed":
                        conn.commit()
                        return False
                    held_for = stamp - (row["claimed_at"] or 0.0)
                    if held_for <= self.lease_seconds:
                        conn.commit()
                        return False

                conn.execute(
                    """
                    INSERT INTO activations
                        (message_key, activity_id, correlation_id, state,
                         claimed_at, completed_at, outcome)
                    VALUES (?, ?, ?, 'claimed', ?, NULL, NULL)
                    ON CONFLICT(message_key, activity_id) DO UPDATE SET
                        state = 'claimed',
                        claimed_at = excluded.claimed_at,
                        correlation_id = COALESCE(
                            excluded.correlation_id, activations.correlation_id
                        )
                    """,
                    (key, activity, correlation_id, stamp),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def complete(
        self,
        message_key: Any,
        activity_id: Any,
        *,
        outcome: str,
        now: Any,
    ) -> None:
        key = _identity(message_key, "message_key")
        activity = _identity(activity_id, "activity_id")
        stamp = _timestamp(now)

        conn = self._get_conn()
        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """
                    UPDATE activations
                    SET state = 'completed', completed_at = ?, outcome = ?
                    WHERE message_key = ? AND activity_id = ?
                    """,
                    (stamp, outcome, key, activity),
                )
                if cursor.rowcount == 0:
                    raise KeyError(f"activation missing: {key}/{activity}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def pending(self, activity_id: Any) -> list[ActivationRow]:
        activity = _identity(activity_id, "activity_id")
        rows = self._get_conn().execute(
            "SELECT * FROM activations WHERE activity_id = ? AND state = 'claimed' "
            "ORDER BY claimed_at",
            (activity,),
        ).fetchall()
        return [_row(r) for r in rows]

    def get(self, message_key: Any, activity_id: Any) -> ActivationRow | None:
        row = self._get_conn().execute(
            "SELECT * FROM activations WHERE message_key = ? AND activity_id = ?",
            (_identity(message_key, "message_key"), _identity(activity_id, "activity_id")),
        ).fetchone()
        return _row(row) if row is not None else None


def _row(row: sqlite3.Row) -> ActivationRow:
    return ActivationRow(
        message_key=row["message_key"],
        activity_id=row["activity_id"],
        correlation_id=row["correlation_id"],
        state=row["state"],
        claimed_at=row["claimed_at"],
        completed_at=row["completed_at"],
        outcome=row["outcome"],
    )
