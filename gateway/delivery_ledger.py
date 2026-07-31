"""Durable delivery-obligation ledger for gateway final responses.

A final agent response that was generated but not yet confirmed-delivered
to the messaging platform is the one artifact the gateway can lose without
a trace: the turn already burned its tokens, the text exists only in a
Python local, and a crash / planned restart between finalize and platform
ACK drops it silently (#58818, #41696, #63695).

This module records a small durable row per outbound final response in the
shared ``state.db`` (same file and conventions as
``tools.async_delegation`` — WAL, owner pid + process-start-time liveness,
bounded retention). The gateway writes three checkpoints around the send:

    record_obligation()   state='pending'     before any send attempt
    mark_attempting()     state='attempting'  immediately before the await
    mark_delivered() /    state='delivered'   only on SendResult.success
    mark_failed()         state='failed'      on a definitive rejection

On startup, ``sweep_recoverable()`` claims rows whose owning process is
dead and hands them to the gateway for redelivery. Crash semantics are
explicit about ambiguity (the contract review of the earlier
delivery-outbox attempt, #61790, closed it for silently resending
ambiguous sends):

- ``pending``     — the send never started: redeliver plainly, no dup risk.
- ``attempting``  — crashed mid-await: the platform MAY already have the
  message. Redelivered WITH a visible recovered-reply marker so the
  contract is honest at-least-once, never a silent duplicate.
- ``failed``      — definitively rejected once; the restart is a natural
  retry boundary. Also carries the marker.
- ``delivered``   — nothing to do; retention prunes.

Poison rows cannot spin: attempts are capped, stale rows expire, and both
transition to ``abandoned`` (kept briefly for inspection, then pruned).

The generated and attempting checkpoints are a precondition for a turn-final
platform effect. Callers fail closed before send when either checkpoint cannot
be persisted; an untracked final is not an acceptable recovery strategy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DB_LOCK = threading.Lock()

# Redelivery policy knobs (module constants; deliberately not config — the
# ledger itself is gated by ``gateway.delivery_ledger`` and these bounds
# only matter in the rare recovery path).
MAX_ATTEMPTS = 3
STALE_AFTER_SECONDS = 24 * 60 * 60
_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_ROWS = 500
_RUN_RECEIPT_RETENTION_SECONDS = 30 * 24 * 60 * 60
_MAX_RUN_RECEIPTS = 2000
_TERMINAL_RUN_STATES = {"done", "blocked", "failed", "cancelled"}

# Visible prefix for redeliveries that might duplicate an already-received
# message (crash mid-send / post-rejection retry). Honest at-least-once.
RECOVERED_MARKER = (
    "♻️ Recovered reply — the gateway restarted during delivery, "
    "so this may be a duplicate:\n\n"
)


def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        _initialize_schema(conn)
    except Exception:
        # A PRAGMA/DDL failure after a successful connect() must not leak the
        # just-opened connection back to the caller.
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="state.db (delivery_ledger)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS delivery_obligations (
            obligation_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            thread_id TEXT,
            content TEXT NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            last_error TEXT,
            run_receipt_id TEXT
        )"""
    )
    delivery_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(delivery_obligations)")
    }
    if "run_receipt_id" not in delivery_columns:
        conn.execute(
            "ALTER TABLE delivery_obligations ADD COLUMN run_receipt_id TEXT"
        )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS run_terminal_receipts (
            run_receipt_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            session_key TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT '',
            run_generation INTEGER,
            message_ref TEXT,
            run_terminal_state TEXT NOT NULL,
            run_end_reason TEXT,
            run_ended_at REAL,
            final_generated INTEGER NOT NULL DEFAULT 0,
            delivery_obligation_id TEXT,
            final_delivery_status TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            owner_pid INTEGER,
            owner_started_at INTEGER
        )"""
    )
    receipt_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(run_terminal_receipts)")
    }
    if "owner_pid" not in receipt_columns:
        conn.execute(
            "ALTER TABLE run_terminal_receipts ADD COLUMN owner_pid INTEGER"
        )
    if "owner_started_at" not in receipt_columns:
        conn.execute(
            """ALTER TABLE run_terminal_receipts
               ADD COLUMN owner_started_at INTEGER"""
        )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS
           idx_delivery_obligations_run_receipt
           ON delivery_obligations(run_receipt_id)"""
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, and ALWAYS close it.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back the
    transaction; they do not close the connection. Using ``with _connect()``
    alone therefore leaks a connection — and its WAL/SHM file descriptors — on
    every call, deferring the close to the garbage collector. On a long-running
    gateway that exhausts ``RLIMIT_NOFILE`` (the cron-ledger sibling of this
    bug was #69567 / PR #69594). ``record_obligation`` runs on every outbound
    final response, so this ledger is the highest-frequency leaker.
    """
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _owner_stamp() -> tuple[int, Optional[int]]:
    pid = os.getpid()
    try:
        from gateway.status import get_process_start_time

        return pid, get_process_start_time(pid)
    except Exception:
        return pid, None


def _owner_alive(pid: Any, started_at: Any) -> bool:
    """True when the recorded owning process still exists (pid + start time)."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        from gateway.status import get_process_start_time

        current_start = get_process_start_time(pid)
    except Exception:
        current_start = None
    if current_start is None:
        # No such process (or unreadable) — treat unreadable-but-extant
        # processes as alive only if the pid exists.
        try:
            os.kill(pid, 0)  # windows-footgun: ok — EPERM counts as alive below
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    if started_at is None:
        return True
    try:
        return int(current_start) == int(started_at)
    except (TypeError, ValueError):
        return True


def compute_obligation_id(session_key: str, message_ref: str, content: str) -> str:
    """Stable id: same turn + same content re-records idempotently, while
    distinct threads/topics on the same chat can never collide (the
    session_key carries platform, chat and thread; ``message_ref`` is the
    triggering inbound message id, distinguishing turns in one session)."""
    payload = f"{session_key}|{message_ref}|{content}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:24]


def compute_run_receipt_id(
    session_key: str,
    message_ref: str,
    run_generation: Optional[int],
    *,
    run_index: int = 0,
) -> str:
    """Return a stable local identity for one logical gateway agent run."""

    payload = (
        f"{session_key}|{message_ref}|{int(run_generation or 0)}|"
        f"{int(run_index or 0)}"
    )
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:24]


def begin_run_receipt(
    *,
    run_receipt_id: str,
    session_id: str = "",
    task_id: str = "",
    session_key: str = "",
    platform: str = "",
    run_generation: Optional[int] = None,
    message_ref: Optional[str] = None,
) -> None:
    """Durably register a run before model or gateway work begins.

    ``running`` is intentionally an intermediate state. Every graceful exit
    overwrites it with one of the four terminal states; if the process is
    killed outright, the surviving row truthfully exposes an unknown outcome
    instead of inventing completion.
    """

    now = time.time()
    pid, started = _owner_stamp()
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """INSERT INTO run_terminal_receipts
               (run_receipt_id, session_id, task_id, session_key, platform,
                run_generation, message_ref, run_terminal_state,
                final_generated, created_at, updated_at,
                owner_pid, owner_started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'running', 0, ?, ?, ?, ?)
               ON CONFLICT(run_receipt_id) DO UPDATE SET
                 session_id=CASE
                   WHEN excluded.session_id != '' THEN excluded.session_id
                   ELSE run_terminal_receipts.session_id END,
                 task_id=CASE
                   WHEN excluded.task_id != '' THEN excluded.task_id
                   ELSE run_terminal_receipts.task_id END,
                 session_key=CASE
                   WHEN excluded.session_key != '' THEN excluded.session_key
                   ELSE run_terminal_receipts.session_key END,
                 platform=CASE
                   WHEN excluded.platform != '' THEN excluded.platform
                   ELSE run_terminal_receipts.platform END,
                 run_generation=COALESCE(
                   excluded.run_generation,
                   run_terminal_receipts.run_generation
                 ),
                 message_ref=COALESCE(
                   excluded.message_ref,
                   run_terminal_receipts.message_ref
                 ),
                 owner_pid=CASE
                   WHEN run_terminal_receipts.run_terminal_state='running'
                     THEN excluded.owner_pid
                   ELSE run_terminal_receipts.owner_pid END,
                 owner_started_at=CASE
                   WHEN run_terminal_receipts.run_terminal_state='running'
                     THEN excluded.owner_started_at
                   ELSE run_terminal_receipts.owner_started_at END,
                 updated_at=excluded.updated_at""",
            (
                run_receipt_id,
                str(session_id or ""),
                str(task_id or ""),
                str(session_key or ""),
                str(platform or ""),
                run_generation,
                message_ref,
                now,
                now,
                pid,
                started,
            ),
        )


def record_run_terminal_receipt(
    *,
    run_receipt_id: str,
    run_terminal_state: str,
    run_end_reason: str,
    run_ended_at: float,
    final_generated: bool,
    session_id: str = "",
    task_id: str = "",
    session_key: str = "",
    platform: str = "",
    run_generation: Optional[int] = None,
    message_ref: Optional[str] = None,
) -> None:
    """Persist the terminal truth for one run, independent of telemetry."""

    if run_terminal_state not in _TERMINAL_RUN_STATES:
        raise ValueError(f"invalid terminal run state: {run_terminal_state}")
    now = time.time()
    pid, started = _owner_stamp()
    with _DB_LOCK, _transaction() as conn:
        linked = conn.execute(
            """SELECT obligation_id, state
               FROM delivery_obligations
               WHERE run_receipt_id=?
               ORDER BY updated_at DESC LIMIT 1""",
            (run_receipt_id,),
        ).fetchone()
        obligation_id = linked[0] if linked else None
        delivery_status = linked[1] if linked else None
        conn.execute(
            """INSERT INTO run_terminal_receipts
               (run_receipt_id, session_id, task_id, session_key, platform,
                run_generation, message_ref, run_terminal_state,
                run_end_reason, run_ended_at, final_generated,
                delivery_obligation_id, final_delivery_status,
                created_at, updated_at, owner_pid, owner_started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_receipt_id) DO UPDATE SET
                 session_id=CASE
                   WHEN excluded.session_id != '' THEN excluded.session_id
                   ELSE run_terminal_receipts.session_id END,
                 task_id=CASE
                   WHEN excluded.task_id != '' THEN excluded.task_id
                   ELSE run_terminal_receipts.task_id END,
                 session_key=CASE
                   WHEN excluded.session_key != '' THEN excluded.session_key
                   ELSE run_terminal_receipts.session_key END,
                 platform=CASE
                   WHEN excluded.platform != '' THEN excluded.platform
                   ELSE run_terminal_receipts.platform END,
                 run_generation=COALESCE(
                   excluded.run_generation,
                   run_terminal_receipts.run_generation
                 ),
                 message_ref=COALESCE(
                   excluded.message_ref,
                   run_terminal_receipts.message_ref
                 ),
                 run_terminal_state=excluded.run_terminal_state,
                 run_end_reason=excluded.run_end_reason,
                 run_ended_at=excluded.run_ended_at,
                 final_generated=excluded.final_generated,
                 delivery_obligation_id=COALESCE(
                   excluded.delivery_obligation_id,
                   run_terminal_receipts.delivery_obligation_id
                 ),
                 final_delivery_status=COALESCE(
                   excluded.final_delivery_status,
                   run_terminal_receipts.final_delivery_status
                 ),
                 updated_at=excluded.updated_at""",
            (
                run_receipt_id,
                str(session_id or ""),
                str(task_id or ""),
                str(session_key or ""),
                str(platform or ""),
                run_generation,
                message_ref,
                run_terminal_state,
                str(run_end_reason or run_terminal_state)[:500],
                float(run_ended_at),
                int(bool(final_generated)),
                obligation_id,
                delivery_status,
                now,
                now,
                pid,
                started,
            ),
        )
    _prune_run_receipts()


def amend_run_terminal_receipt(
    run_receipt_id: str,
    *,
    run_terminal_state: str,
    run_end_reason: str,
    run_ended_at: Optional[float] = None,
    final_generated: Optional[bool] = None,
) -> None:
    """Amend an existing receipt after a later gateway failure/cancellation."""

    if run_terminal_state not in _TERMINAL_RUN_STATES:
        raise ValueError(f"invalid terminal run state: {run_terminal_state}")
    ended_at = float(run_ended_at if run_ended_at is not None else time.time())
    assignments = [
        "run_terminal_state=?",
        "run_end_reason=?",
        "run_ended_at=?",
        "updated_at=?",
    ]
    params: list[Any] = [
        run_terminal_state,
        str(run_end_reason or run_terminal_state)[:500],
        ended_at,
        time.time(),
    ]
    if final_generated is not None:
        assignments.append("final_generated=?")
        params.append(int(bool(final_generated)))
    params.append(run_receipt_id)
    with _DB_LOCK, _transaction() as conn:
        cursor = conn.execute(
            f"""UPDATE run_terminal_receipts
                SET {", ".join(assignments)}
                WHERE run_receipt_id=?""",
            params,
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "run terminal receipt update matched no durable row"
            )


def get_run_terminal_receipt(run_receipt_id: str) -> Optional[Dict[str, Any]]:
    """Return one receipt for proof/tests without exposing full response text."""

    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            """SELECT run_receipt_id, session_id, task_id, session_key,
                      platform, run_generation, message_ref,
                      run_terminal_state, run_end_reason, run_ended_at,
                      final_generated, delivery_obligation_id,
                      final_delivery_status, created_at, updated_at
               FROM run_terminal_receipts
               WHERE run_receipt_id=?""",
            (run_receipt_id,),
        ).fetchone()
    if row is None:
        return None
    keys = (
        "run_receipt_id",
        "session_id",
        "task_id",
        "session_key",
        "platform",
        "run_generation",
        "message_ref",
        "run_terminal_state",
        "run_end_reason",
        "run_ended_at",
        "final_generated",
        "delivery_obligation_id",
        "final_delivery_status",
        "created_at",
        "updated_at",
    )
    result = dict(zip(keys, row))
    result["final_generated"] = bool(result["final_generated"])
    return result


def record_obligation(
    *,
    obligation_id: str,
    session_key: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    content: str,
    run_receipt_id: Optional[str] = None,
) -> None:
    """Record a final response as owed to the platform (state='pending')."""
    now = time.time()
    pid, started = _owner_stamp()
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, thread_id,
                content, state, attempts, created_at, updated_at,
                owner_pid, owner_started_at, run_receipt_id)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)""",
            (obligation_id, session_key, platform, str(chat_id),
             str(thread_id) if thread_id else None, content, now, now,
             pid, started, run_receipt_id),
        )
        if run_receipt_id:
            cursor = conn.execute(
                """UPDATE run_terminal_receipts
                   SET delivery_obligation_id=?,
                       final_delivery_status='pending',
                       final_generated=1,
                       updated_at=?
                   WHERE run_receipt_id=?""",
                (obligation_id, now, run_receipt_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "delivery obligation could not link its run receipt"
                )
    _prune()


def mark_attempting(obligation_id: str) -> None:
    _update_state(obligation_id, "attempting")


def mark_delivered(obligation_id: str) -> None:
    _update_state(obligation_id, "delivered")


def mark_failed(obligation_id: str, error: str = "") -> None:
    _update_state(obligation_id, "failed", error=error)


def delivery_result_is_unknown(result: Any) -> bool:
    """Return true when a final-send effect may exist without an ACK.

    Unknown outcomes are never evidence of failure and therefore must remain
    in ``attempting``. Recovery can then make any later retry visible instead
    of silently producing a duplicate.
    """

    if result is None:
        return True
    error_kind = str(getattr(result, "error_kind", "") or "").strip().lower()
    if error_kind == "unknown":
        return True
    error = str(getattr(result, "error", None) or result).lower()
    name = result.__class__.__name__.lower()
    return "timeout" in error or "timed out" in error or "timeout" in name


def _update_state(obligation_id: str, state: str, error: str = "") -> None:
    with _DB_LOCK, _transaction() as conn:
        cursor = conn.execute(
            """UPDATE delivery_obligations
               SET state=?, updated_at=?, last_error=?
               WHERE obligation_id=?""",
            (state, time.time(), error[:500] if error else None, obligation_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "delivery obligation state update matched no durable row"
            )
        row = conn.execute(
            """SELECT run_receipt_id FROM delivery_obligations
               WHERE obligation_id=?""",
            (obligation_id,),
        ).fetchone()
        run_receipt_id = row[0] if row else None
        if run_receipt_id:
            receipt_cursor = conn.execute(
                """UPDATE run_terminal_receipts
                   SET delivery_obligation_id=?,
                       final_delivery_status=?,
                       updated_at=?
                   WHERE run_receipt_id=?""",
                (obligation_id, state, time.time(), run_receipt_id),
            )
            if receipt_cursor.rowcount != 1:
                raise RuntimeError(
                    "delivery state could not update its run receipt"
                )


def sweep_dead_run_receipts(now: Optional[float] = None) -> List[str]:
    """Close ``running`` receipts whose owning process is provably dead.

    A hard process kill cannot execute a normal exception/finally path. The
    next gateway startup converts only dead-owner rows to explicit failed,
    unknown-outcome terminals; live-owner rows remain untouched.
    """

    now = float(now if now is not None else time.time())
    closed: List[str] = []
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT run_receipt_id, owner_pid, owner_started_at
               FROM run_terminal_receipts
               WHERE run_terminal_state='running'"""
        ).fetchall()
        for run_receipt_id, owner_pid, owner_started_at in rows:
            if _owner_alive(owner_pid, owner_started_at):
                continue
            cursor = conn.execute(
                """UPDATE run_terminal_receipts
                   SET run_terminal_state='failed',
                       run_end_reason='process_terminated_unknown_outcome',
                       run_ended_at=?,
                       updated_at=?
                   WHERE run_receipt_id=?
                     AND run_terminal_state='running'
                     AND (owner_pid IS ? OR owner_pid=?)
                     AND (owner_started_at IS ? OR owner_started_at=?)""",
                (
                    now,
                    now,
                    run_receipt_id,
                    owner_pid,
                    owner_pid,
                    owner_started_at,
                    owner_started_at,
                ),
            )
            if cursor.rowcount == 1:
                closed.append(run_receipt_id)
    if closed:
        _prune_run_receipts(now)
    return closed


def sweep_recoverable(
    now: Optional[float] = None,
    *,
    deliverable_platforms: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Claim undelivered rows owned by dead processes; return them for
    redelivery.

    Claiming atomically re-stamps the owner to THIS process and increments
    ``attempts``, so a second gateway racing the same sweep cannot
    double-claim (the UPDATE is guarded on the previous owner stamp).
    Rows over the attempts cap or older than the stale cutoff transition to
    'abandoned' instead of being returned.

    ``deliverable_platforms`` (platform value strings) restricts claiming to
    platforms the caller can actually send on this boot.  ``attempts`` is the
    redelivery budget, so it must only be spent on a real send: a platform
    that failed to connect would otherwise burn one attempt per boot and hit
    the cap having never been sent once.  Rows for absent platforms are left
    untouched for a later boot; the stale cutoff still bounds them.
    """
    now = now if now is not None else time.time()
    pid, started = _owner_stamp()
    claimed: List[Dict[str, Any]] = []
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id, thread_id,
                      content, state, attempts, created_at,
                      owner_pid, owner_started_at
               FROM delivery_obligations
               WHERE state IN ('pending', 'attempting', 'failed')"""
        ).fetchall()
        for (oid, session_key, platform, chat_id, thread_id, content, state,
             attempts, created_at, owner_pid, owner_started_at) in rows:
            if _owner_alive(owner_pid, owner_started_at):
                continue  # a live gateway still owns this row
            if attempts >= MAX_ATTEMPTS or (now - created_at) > STALE_AFTER_SECONDS:
                conn.execute(
                    """UPDATE delivery_obligations
                       SET state='abandoned', updated_at=? WHERE obligation_id=?""",
                    (now, oid),
                )
                continue
            if (
                deliverable_platforms is not None
                and platform not in deliverable_platforms
            ):
                # No adapter for this platform this boot — the caller cannot
                # send, so claiming would spend an attempt on a no-op.
                continue
            cursor = conn.execute(
                """UPDATE delivery_obligations
                   SET owner_pid=?, owner_started_at=?, attempts=attempts+1,
                       updated_at=?
                   WHERE obligation_id=? AND (owner_pid IS ? OR owner_pid=?)""",
                (pid, started, now, oid, owner_pid, owner_pid),
            )
            if cursor.rowcount:
                claimed.append({
                    "obligation_id": oid,
                    "session_key": session_key,
                    "platform": platform,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "content": content,
                    # pending = send never started, redeliver plainly;
                    # attempting/failed = ambiguous or rejected, carry marker.
                    "needs_marker": state != "pending",
                    "attempts": attempts + 1,
                })
    return claimed


def _prune(now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    cutoff = now - _RETENTION_SECONDS
    try:
        with _transaction() as conn:
            conn.execute(
                """DELETE FROM delivery_obligations
                   WHERE state IN ('delivered', 'abandoned') AND updated_at < ?""",
                (cutoff,),
            )
            total = conn.execute(
                "SELECT COUNT(*) FROM delivery_obligations"
            ).fetchone()[0]
            excess = max(0, total - _MAX_ROWS)
            if excess:
                conn.execute(
                    """DELETE FROM delivery_obligations WHERE obligation_id IN (
                         SELECT obligation_id FROM delivery_obligations
                         ORDER BY CASE state
                                    WHEN 'delivered' THEN 0
                                    WHEN 'abandoned' THEN 1
                                    ELSE 2
                                  END, updated_at ASC
                         LIMIT ?)""",
                    (excess,),
                )
    except Exception:
        logger.debug("delivery ledger prune failed", exc_info=True)


def _prune_run_receipts(now: Optional[float] = None) -> None:
    """Bound terminal proof while retaining unresolved delivery links."""

    now = now if now is not None else time.time()
    cutoff = now - _RUN_RECEIPT_RETENTION_SECONDS
    try:
        with _transaction() as conn:
            conn.execute(
                """DELETE FROM run_terminal_receipts
                   WHERE run_terminal_state != 'running'
                     AND updated_at < ?""",
                (cutoff,),
            )
            total = conn.execute(
                "SELECT COUNT(*) FROM run_terminal_receipts"
            ).fetchone()[0]
            excess = max(0, total - _MAX_RUN_RECEIPTS)
            if excess:
                conn.execute(
                    """DELETE FROM run_terminal_receipts
                       WHERE run_receipt_id IN (
                         SELECT run_receipt_id
                         FROM run_terminal_receipts
                         WHERE run_terminal_state != 'running'
                           AND COALESCE(final_delivery_status, 'delivered')
                               NOT IN ('pending', 'attempting')
                         ORDER BY updated_at ASC
                         LIMIT ?
                       )""",
                    (excess,),
                )
    except Exception:
        logger.debug("run terminal receipt prune failed", exc_info=True)


def ledger_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """Read the ``gateway.delivery_ledger`` config gate (default on)."""
    try:
        if config is None:
            from hermes_cli.config import load_config

            config = load_config()
        gw = config.get("gateway") or {}
        value = gw.get("delivery_ledger", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no", "off"}
        return bool(value)
    except Exception:
        return True


def debug_rows(limit: int = 20) -> str:
    """Human-readable dump for ad-hoc inspection (sqlite3-free path)."""
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, state, attempts,
                      created_at, updated_at, last_error
               FROM delivery_obligations
               ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return json.dumps(
        [
            {
                "id": r[0], "session": r[1], "state": r[2], "attempts": r[3],
                "created_at": r[4], "updated_at": r[5], "last_error": r[6],
            }
            for r in rows
        ],
        indent=2,
    )
