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
import uuid

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
CREATE TABLE IF NOT EXISTS activation_wake_outbox (
    message_key    TEXT NOT NULL,
    activity_id    TEXT NOT NULL,
    outbox_token   TEXT NOT NULL UNIQUE,
    job_id         TEXT NOT NULL,
    caller         TEXT NOT NULL,
    reason         TEXT,
    requested_at   REAL NOT NULL,
    PRIMARY KEY (message_key, activity_id),
    FOREIGN KEY (message_key, activity_id)
        REFERENCES activations (message_key, activity_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS activation_wake_outbox_pending
    ON activation_wake_outbox (requested_at, message_key, activity_id);
"""

#: Longest a single cron run can legitimately take (``_DEFAULT_SCRIPT_TIMEOUT``
#: in cron/scheduler.py, plus the agent's own wall-clock bound).
CRON_WALL_CLOCK_CEILING_SECONDS = 3600

#: The lease is a CRASH-recovery device, not a run timer. Nothing calls
#: ``complete()`` in production yet — wiring a completion signal from a cron run
#: back to this ledger is Task 5 work — so lease expiry is currently the only
#: thing that releases a claim. It must therefore comfortably exceed the longest
#: legitimate run, or redelivery re-claims a message while its worker is still
#: going and wakes it a second time.
DEFAULT_LEASE_SECONDS = 2 * CRON_WALL_CLOCK_CEILING_SECONDS

#: How often the deterministic reconciler actually runs — cron
#: ``jobflow-reconcile`` (64711e6d8334), ``30 0,6,12,18 * * *``.
#:
#: This is the OTHER side of the lease, and the reason the lease is bounded
#: above as well as below. A claimed row is invisible to ``scan_actionable``
#: until its lease lapses, so the lease is not only a crash-recovery timer; it
#: is a blind spot in the safety net. Keep ``DEFAULT_LEASE_SECONDS`` comfortably
#: under this or a claim can span an entire recovery window and genuinely
#: stranded work waits for the tick after next. The relationship is asserted in
#: tests/jobflow_dispatch/test_store.py — raising the lease multiplier above 3x
#: is meant to fail there rather than quietly blind the reconciler, which is
#: what the 900 -> 7200 bump did.
RECONCILER_PERIOD_SECONDS = 6 * 3600


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


@dataclass(frozen=True)
class WakeOutboxRow:
    message_key: str
    activity_id: str
    outbox_token: str
    job_id: str
    caller: str
    reason: str | None
    requested_at: float


def _identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("now must be a numeric timestamp")
    return float(value)


def _lease(value: Any) -> int | float:
    """Reject a lease that cannot do its job, at construction.

    A bad lease does not fail where you would look for it. A string fails as a
    TypeError deep inside ``claim``'s comparison; ``True`` silently becomes a
    one-second lease and every redelivery re-dispatches. Both read as "dedupe is
    behaving oddly" rather than as a config error.

    The upper bound is the load-bearing one. A claim is invisible to
    ``scan_actionable`` until its lease lapses, so a lease that reaches
    ``RECONCILER_PERIOD_SECONDS`` can hide genuinely stranded work across an
    entire recovery window — the safety net stops being a safety net, silently.

    This guard is 1x the window (reject only the provably broken); the 2x margin
    asserted in tests applies to the SHIPPED DEFAULT, a stricter question than
    what any ad-hoc store may be built with. The mismatch is deliberate.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("lease_seconds must be a positive number")
    if value <= 0:
        raise ValueError(f"lease_seconds must be positive, got {value}")
    if value >= RECONCILER_PERIOD_SECONDS:
        raise ValueError(
            f"lease_seconds {value} >= the reconciler period "
            f"{RECONCILER_PERIOD_SECONDS}s: a claim could hide stranded work "
            "across a full recovery window"
        )
    return value


class ActivationStore:
    def __init__(self, db_path: Path, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = _lease(lease_seconds)
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
            conn.execute("PRAGMA foreign_keys=ON")
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

    def claim_for_wake(
        self,
        message_key: Any,
        activity_id: Any,
        *,
        job_id: Any,
        caller: Any,
        reason: Any = None,
        now: Any,
        correlation_id: str | None = None,
    ) -> WakeOutboxRow | None:
        """Atomically claim work and record the wake that must be delivered."""
        key = _identity(message_key, "message_key")
        activity = _identity(activity_id, "activity_id")
        job = _identity(job_id, "job_id")
        owner = _identity(caller, "caller")
        detail = None if reason is None else str(reason)
        stamp = _timestamp(now)
        token = uuid.uuid4().hex

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
                        return None
                    held_for = stamp - (row["claimed_at"] or 0.0)
                    if held_for <= self.lease_seconds:
                        conn.commit()
                        return None

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
                conn.execute(
                    """
                    INSERT INTO activation_wake_outbox
                        (message_key, activity_id, outbox_token, job_id,
                         caller, reason, requested_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_key, activity_id) DO UPDATE SET
                        outbox_token = excluded.outbox_token,
                        job_id = excluded.job_id,
                        caller = excluded.caller,
                        reason = excluded.reason,
                        requested_at = excluded.requested_at
                    """,
                    (key, activity, token, job, owner, detail, stamp),
                )
                conn.commit()
                return WakeOutboxRow(
                    message_key=key,
                    activity_id=activity,
                    outbox_token=token,
                    job_id=job,
                    caller=owner,
                    reason=detail,
                    requested_at=stamp,
                )
            except Exception:
                conn.rollback()
                raise

    def pending_wake_outbox(self) -> list[WakeOutboxRow]:
        rows = self._get_conn().execute(
            "SELECT * FROM activation_wake_outbox "
            "ORDER BY requested_at, message_key, activity_id"
        ).fetchall()
        return [_wake_outbox_row(row) for row in rows]

    def ack_wake_outbox(self, outbox: WakeOutboxRow) -> bool:
        if not isinstance(outbox, WakeOutboxRow):
            raise ValueError("exact WakeOutboxRow capability is required")
        conn = self._get_conn()
        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "DELETE FROM activation_wake_outbox "
                    "WHERE message_key = ? AND activity_id = ? AND outbox_token = ?",
                    (outbox.message_key, outbox.activity_id, outbox.outbox_token),
                )
                conn.commit()
                return cursor.rowcount == 1
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
                conn.execute(
                    "DELETE FROM activation_wake_outbox "
                    "WHERE message_key = ? AND activity_id = ?",
                    (key, activity),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def release(self, message_key: Any, activity_id: Any) -> None:
        """Hand a claim back so it can be retried immediately.

        For the case where the claim committed but the activation could not be
        delivered (wake channel full, job unresolvable). Without this the
        message is invisible to BOTH paths until the lease expires — claimed so
        the reconciler skips it, but never actually woken.

        Completed work is never resurrected.
        """
        key = _identity(message_key, "message_key")
        activity = _identity(activity_id, "activity_id")

        conn = self._get_conn()
        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "DELETE FROM activations "
                    "WHERE message_key = ? AND activity_id = ? AND state != 'completed'",
                    (key, activity),
                )
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

    def claim_census(self) -> list[ActivationRow]:
        """Return every durable claim in deterministic order, without pagination."""
        rows = self._get_conn().execute(
            "SELECT * FROM activations WHERE state = 'claimed' "
            "ORDER BY activity_id, message_key"
        ).fetchall()
        return [_row(row) for row in rows]


def is_available(
    store: ActivationStore, message_key: str, activity_id: str, now: float
) -> bool:
    """True when no live claim and no completion covers this work.

    The ONE availability predicate. The reconciler decides what to resurface
    with it; the dispatcher's shadow mode decides what it *would* have woken
    with it. A second copy would eventually drift, and the drift would surface
    as shadow under- or over-reporting recall — i.e. as a dispatcher fault that
    isn't one. Keep both callers on this function.

    Read-only on purpose: deciding is not consuming. A predicate that also
    claimed would mean a scan that merely *looks* takes the work.
    """
    row = store.get(message_key, activity_id)
    if row is None:
        return True
    if row.state == "completed":
        return False
    return (now - (row.claimed_at or 0.0)) > store.lease_seconds


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


def _wake_outbox_row(row: sqlite3.Row) -> WakeOutboxRow:
    return WakeOutboxRow(
        message_key=row["message_key"],
        activity_id=row["activity_id"],
        outbox_token=row["outbox_token"],
        job_id=row["job_id"],
        caller=row["caller"],
        reason=row["reason"],
        requested_at=float(row["requested_at"]),
    )
