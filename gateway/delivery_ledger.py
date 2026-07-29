"""Durable delivery-obligation ledger for gateway final responses.

A final agent response that was generated but not yet confirmed-delivered
to the messaging platform is the one artifact the gateway can lose without
a trace: the turn already burned its tokens, the text exists only in a
Python local, and a crash / planned restart between finalize and platform
ACK drops it silently (#58818, #41696, #63695).

This module records a small durable row per outbound final response in the
shared ``state.db`` (same file and conventions as
``tools.async_delegation`` — WAL, owner pid + process-start-time liveness,
bounded retention). The gateway writes checkpoints around admission and send:

    record_accepted_route() state='accepted'    before HTTP 202
    begin_terminal_attempt() state='attempting' atomically with final content
    mark_delivered() /      state='delivered'   only on SendResult.success
    mark_failed()           state='failed'      on a definitive rejection

On startup, ``sweep_recoverable()`` claims rows whose owning process is
dead and hands them to the gateway for redelivery. Crash semantics are
explicit about ambiguity (the contract review of the earlier
delivery-outbox attempt, #61790, closed it for silently resending
ambiguous sends):

- ``accepted``    — restore the bounded route only; the resumed turn still
  owes its first terminal envelope, so nothing is sent by the sweep.
- ``pending``     — the send never started: redeliver plainly, no dup risk.
- ``attempting``  — crashed mid-await: the platform MAY already have the
  message. Redelivered WITH a visible recovered-reply marker so the
  contract is honest at-least-once, never a silent duplicate.
- ``failed``      — definitively rejected once; the restart is a natural
  retry boundary. Also carries the marker.
- ``delivered``   — nothing to do; retention prunes.
- ``conflict``    — the same provider identity produced a different durable
  route or terminal envelope. Fail closed; never replay either body.

Poison rows cannot spin: attempts are capped, stale rows expire, and both
transition to ``abandoned`` (kept briefly for inspection, then pruned).

General messaging callers use the ledger best-effort. Terminal webhook
callbacks that depend on persisted rendered routing fail closed if their
pre-egress journal write cannot complete.
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
from typing import Any, Dict, Iterator, List, Mapping, Optional

from gateway.delivery_metadata import (
    TERMINAL_DELIVERY_METADATA_KEY,
    project_delivery_ledger_context,
    project_terminal_delivery,
)
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

# Visible prefix for redeliveries that might duplicate an already-received
# message (crash mid-send / post-rejection retry). Honest at-least-once.
RECOVERED_MARKER = (
    "♻️ Recovered reply — the gateway restarted during delivery, "
    "so this may be a duplicate:\n\n"
)

ADMISSION_RECORDED = "recorded"
ADMISSION_DUPLICATE = "duplicate"
TERMINAL_ATTEMPT_STARTED = "started"
TERMINAL_ATTEMPT_IN_PROGRESS = "in_progress"
TERMINAL_ATTEMPT_ALREADY_DELIVERED = "already_delivered"


class DeliveryConflictError(RuntimeError):
    """A provider identity was reused with different durable semantics."""


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
            delivery_context_json TEXT,
            delivery_metadata_json TEXT
        )"""
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(delivery_obligations)")
    }
    for column in ("delivery_context_json", "delivery_metadata_json"):
        if column in columns:
            continue
        try:
            conn.execute(
                f"ALTER TABLE delivery_obligations ADD COLUMN {column} TEXT"
            )
        except sqlite3.OperationalError:
            # Another gateway process may have completed the additive migration
            # after our PRAGMA snapshot. Ignore only that confirmed race.
            current = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(delivery_obligations)"
                )
            }
            if column not in current:
                raise


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


def _serialize_delivery_context(context: Mapping[str, Any] | None) -> Optional[str]:
    projected = project_delivery_ledger_context(context)
    if projected is None:
        return None
    return json.dumps(projected, sort_keys=True, separators=(",", ":"))


def _serialize_delivery_metadata(metadata: Mapping[str, Any] | None) -> Optional[str]:
    terminal = project_terminal_delivery(metadata)
    if terminal is None:
        return None
    return json.dumps(
        {TERMINAL_DELIVERY_METADATA_KEY: terminal},
        sort_keys=True,
        separators=(",", ":"),
    )


def _deserialize_json_object(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _normalized_thread_id(thread_id: Optional[str]) -> Optional[str]:
    return str(thread_id) if thread_id else None


def _row_matches_route(
    row: tuple,
    *,
    session_key: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    context_json: str,
) -> bool:
    return (
        row[0] == session_key
        and row[1] == platform
        and row[2] == str(chat_id)
        and row[3] == _normalized_thread_id(thread_id)
        and row[4] == context_json
    )


def _mark_conflict(
    conn: sqlite3.Connection,
    obligation_id: str,
    state: str,
) -> None:
    # A delivered obligation is immutable: report the conflicting replay but
    # never reopen it. Unresolved rows become non-recoverable so a later boot
    # cannot silently replay the first stale envelope.
    if state == "delivered":
        return
    conn.execute(
        """UPDATE delivery_obligations
           SET state='conflict', updated_at=?, last_error=?
           WHERE obligation_id=? AND state=?""",
        (time.time(), "provider identity conflict", obligation_id, state),
    )
    # Conflict is itself the fail-closed durable decision. Commit it before
    # the caller raises DeliveryConflictError; otherwise the transaction
    # context would roll this safety transition back with the exception.
    conn.commit()


def record_accepted_route(
    *,
    obligation_id: str,
    session_key: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    delivery_context: Mapping[str, Any],
) -> str:
    """Durably admit one privacy-bounded callback route before HTTP 202.

    The accepted row intentionally contains no prompt, request payload,
    terminal output, terminal metadata, secret, or tool data. Reusing the
    provider identity with a different rendered route fails closed.
    """
    now = time.time()
    pid, started = _owner_stamp()
    context_json = _serialize_delivery_context(delivery_context)
    if context_json is None:
        raise ValueError("Accepted callback route is not replay-safe")

    with _DB_LOCK, _transaction() as conn:
        cursor = conn.execute(
            """INSERT INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, thread_id,
                content, state, attempts, created_at, updated_at,
                owner_pid, owner_started_at, delivery_context_json,
                delivery_metadata_json)
               VALUES (?, ?, ?, ?, ?, '', 'accepted', 0, ?, ?, ?, ?, ?, NULL)
               ON CONFLICT(obligation_id) DO NOTHING""",
            (
                obligation_id,
                session_key,
                platform,
                str(chat_id),
                _normalized_thread_id(thread_id),
                now,
                now,
                pid,
                started,
                context_json,
            ),
        )
        if cursor.rowcount:
            decision = ADMISSION_RECORDED
        else:
            row = conn.execute(
                """SELECT session_key, platform, chat_id, thread_id,
                          delivery_context_json, state
                   FROM delivery_obligations WHERE obligation_id=?""",
                (obligation_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Accepted callback row disappeared")
            if not _row_matches_route(
                row,
                session_key=session_key,
                platform=platform,
                chat_id=chat_id,
                thread_id=thread_id,
                context_json=context_json,
            ):
                _mark_conflict(conn, obligation_id, row[5])
                raise DeliveryConflictError(
                    "Provider identity conflicts with the accepted callback route"
                )
            if row[5] == "conflict":
                raise DeliveryConflictError(
                    "Provider identity already has a durable conflict"
                )
            decision = ADMISSION_DUPLICATE
    _prune()
    return decision


def begin_terminal_attempt(
    *,
    obligation_id: str,
    session_key: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    content: str,
    delivery_context: Mapping[str, Any],
    delivery_metadata: Mapping[str, Any],
) -> str:
    """Atomically bind final content to an accepted provider identity.

    Same-envelope retries may resume only from ``pending``/``failed``.
    ``attempting`` fails closed as already in flight, and ``delivered`` is an
    idempotent no-op. A different route, body, or strict terminal marker is a
    conflict; unresolved rows become non-recoverable so stale content cannot
    be replayed after a crash.
    """
    now = time.time()
    pid, started = _owner_stamp()
    context_json = _serialize_delivery_context(delivery_context)
    metadata_json = _serialize_delivery_metadata(delivery_metadata)
    if context_json is None or metadata_json is None:
        raise ValueError("Terminal callback envelope is not replay-safe")

    with _DB_LOCK, _transaction() as conn:
        cursor = conn.execute(
            """INSERT INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, thread_id,
                content, state, attempts, created_at, updated_at,
                owner_pid, owner_started_at, delivery_context_json,
                delivery_metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, 'attempting', 0, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(obligation_id) DO NOTHING""",
            (
                obligation_id,
                session_key,
                platform,
                str(chat_id),
                _normalized_thread_id(thread_id),
                content,
                now,
                now,
                pid,
                started,
                context_json,
                metadata_json,
            ),
        )
        if cursor.rowcount:
            return TERMINAL_ATTEMPT_STARTED

        row = conn.execute(
            """SELECT session_key, platform, chat_id, thread_id,
                      delivery_context_json, state, content,
                      delivery_metadata_json
               FROM delivery_obligations WHERE obligation_id=?""",
            (obligation_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Terminal callback row disappeared")
        state = row[5]
        if not _row_matches_route(
            row,
            session_key=session_key,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            context_json=context_json,
        ):
            _mark_conflict(conn, obligation_id, state)
            raise DeliveryConflictError(
                "Provider identity conflicts with the durable callback route"
            )

        if state == "accepted":
            if row[6] != "" or row[7] is not None:
                _mark_conflict(conn, obligation_id, state)
                raise DeliveryConflictError(
                    "Accepted callback contains an unexpected terminal envelope"
                )
            updated = conn.execute(
                """UPDATE delivery_obligations
                   SET content=?, state='attempting', updated_at=?,
                       owner_pid=?, owner_started_at=?,
                       delivery_metadata_json=?, last_error=NULL
                   WHERE obligation_id=? AND state='accepted'
                         AND content='' AND delivery_metadata_json IS NULL""",
                (
                    content,
                    now,
                    pid,
                    started,
                    metadata_json,
                    obligation_id,
                ),
            )
            if updated.rowcount:
                return TERMINAL_ATTEMPT_STARTED
            raise RuntimeError("Accepted callback state changed concurrently")

        envelope_matches = row[6] == content and row[7] == metadata_json
        if not envelope_matches:
            _mark_conflict(conn, obligation_id, state)
            raise DeliveryConflictError(
                "Provider identity conflicts with the durable terminal envelope"
            )

        if state in {"pending", "failed"}:
            updated = conn.execute(
                """UPDATE delivery_obligations
                   SET state='attempting', updated_at=?, owner_pid=?,
                       owner_started_at=?, last_error=NULL
                   WHERE obligation_id=? AND state=?""",
                (now, pid, started, obligation_id, state),
            )
            if updated.rowcount:
                return TERMINAL_ATTEMPT_STARTED
            raise RuntimeError("Terminal callback state changed concurrently")
        if state == "attempting":
            return TERMINAL_ATTEMPT_IN_PROGRESS
        if state == "delivered":
            return TERMINAL_ATTEMPT_ALREADY_DELIVERED
        raise DeliveryConflictError(
            f"Terminal callback cannot proceed from durable state {state!r}"
        )


def record_obligation(
    *,
    obligation_id: str,
    session_key: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    content: str,
    delivery_context: Mapping[str, Any] | None = None,
    delivery_metadata: Mapping[str, Any] | None = None,
    preserve_existing: bool = False,
) -> None:
    """Record a final response as owed to the platform (state='pending')."""
    now = time.time()
    pid, started = _owner_stamp()
    context_json = _serialize_delivery_context(delivery_context)
    metadata_json = _serialize_delivery_metadata(delivery_metadata)
    with _DB_LOCK, _transaction() as conn:
        insert = (
            """INSERT INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, thread_id,
                content, state, attempts, created_at, updated_at,
                owner_pid, owner_started_at, delivery_context_json,
                delivery_metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(obligation_id) DO NOTHING"""
            if preserve_existing
            else
            """INSERT INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, thread_id,
                content, state, attempts, created_at, updated_at,
                owner_pid, owner_started_at, delivery_context_json,
                delivery_metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(obligation_id) DO UPDATE SET
                   session_key=excluded.session_key,
                   platform=excluded.platform,
                   chat_id=excluded.chat_id,
                   thread_id=excluded.thread_id,
                   content=excluded.content,
                   state='pending',
                   attempts=0,
                   created_at=excluded.created_at,
                   updated_at=excluded.updated_at,
                   owner_pid=excluded.owner_pid,
                   owner_started_at=excluded.owner_started_at,
                   last_error=NULL,
                   delivery_context_json=excluded.delivery_context_json,
                   delivery_metadata_json=excluded.delivery_metadata_json
               WHERE delivery_obligations.state != 'delivered'"""
        )
        conn.execute(
            insert,
            (obligation_id, session_key, platform, str(chat_id),
             _normalized_thread_id(thread_id), content, now, now,
             pid, started, context_json, metadata_json),
        )
    _prune()


def mark_attempting(obligation_id: str) -> None:
    _update_state(
        obligation_id,
        "attempting",
        from_states={"pending", "failed"},
    )


def mark_delivered(obligation_id: str) -> None:
    _update_state(
        obligation_id,
        "delivered",
        from_states={"pending", "attempting", "failed"},
    )


def mark_failed(obligation_id: str, error: str = "") -> None:
    _update_state(
        obligation_id,
        "failed",
        error=error,
        from_states={"pending", "attempting", "failed"},
    )


def mark_abandoned(obligation_id: str, error: str = "") -> None:
    _update_state(
        obligation_id,
        "abandoned",
        error=error,
        from_states={"accepted", "pending", "attempting", "failed", "conflict"},
    )


def _update_state(
    obligation_id: str,
    state: str,
    error: str = "",
    *,
    from_states: Optional[set[str]] = None,
) -> None:
    with _DB_LOCK, _transaction() as conn:
        params: list[Any] = [
            state,
            time.time(),
            error[:500] if error else None,
            obligation_id,
        ]
        where = "obligation_id=?"
        if from_states:
            placeholders = ",".join("?" for _ in from_states)
            where += f" AND state IN ({placeholders})"
            params.extend(sorted(from_states))
        conn.execute(
            f"""UPDATE delivery_obligations
                SET state=?, updated_at=?, last_error=?
                WHERE {where}""",
            params,
        )


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
                      owner_pid, owner_started_at, delivery_context_json,
                      delivery_metadata_json
               FROM delivery_obligations
               WHERE state IN ('accepted', 'pending', 'attempting', 'failed')"""
        ).fetchall()
        for (oid, session_key, platform, chat_id, thread_id, content, state,
             attempts, created_at, owner_pid, owner_started_at, context_json,
             metadata_json) in rows:
            if _owner_alive(owner_pid, owner_started_at):
                continue  # a live gateway still owns this row
            if (
                (state != "accepted" and attempts >= MAX_ATTEMPTS)
                or (now - created_at) > STALE_AFTER_SECONDS
            ):
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
            if state == "accepted":
                cursor = conn.execute(
                    """UPDATE delivery_obligations
                       SET owner_pid=?, owner_started_at=?, updated_at=?
                       WHERE obligation_id=? AND state='accepted'
                             AND (owner_pid IS ? OR owner_pid=?)""",
                    (pid, started, now, oid, owner_pid, owner_pid),
                )
                claimed_attempts = attempts
            else:
                cursor = conn.execute(
                    """UPDATE delivery_obligations
                       SET owner_pid=?, owner_started_at=?, attempts=attempts+1,
                           updated_at=?
                       WHERE obligation_id=? AND state=?
                             AND (owner_pid IS ? OR owner_pid=?)""",
                    (pid, started, now, oid, state, owner_pid, owner_pid),
                )
                claimed_attempts = attempts + 1
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
                    "state": state,
                    "ready_for_delivery": state != "accepted",
                    "needs_marker": state not in {"accepted", "pending"},
                    "attempts": claimed_attempts,
                    "delivery_context": project_delivery_ledger_context(
                        _deserialize_json_object(context_json)
                    ),
                    "delivery_metadata": (
                        {
                            TERMINAL_DELIVERY_METADATA_KEY: terminal
                        }
                        if (
                            terminal := project_terminal_delivery(
                                _deserialize_json_object(metadata_json)
                            )
                        )
                        is not None
                        else None
                    ),
                })
    return claimed


def _prune(now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    cutoff = now - _RETENTION_SECONDS
    try:
        with _transaction() as conn:
            conn.execute(
                """DELETE FROM delivery_obligations
                   WHERE state IN ('delivered', 'abandoned', 'conflict')
                         AND updated_at < ?""",
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
                         WHERE state IN ('delivered', 'abandoned', 'conflict')
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
