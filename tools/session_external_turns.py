#!/usr/bin/env python3
"""A durable inbox for one non-interrupting activation of a stored session.

WHAT THIS IS FOR

A local process that is not Hermes finishes some work and needs to tell the
person about it, in the conversation they already have open. Every existing
notification rail assumes the producer runs INSIDE the process that owns that
live session -- async delegations are children of the turn that dispatched
them, kanban rows are addressed to a subscription this gateway registered. An
unrelated local process had no way in at all, and its only recourse was to open
a SECOND owner of the session and write there, which is precisely what the
active-session lease refuses.

So the producer does not deliver. It enqueues, and whichever process
legitimately owns the session consumes.

WHY THAT ORDERING MATTERS

The obvious design asks "does this session have a live owner?" and picks a
transport from the answer. That answer is stale the moment it is read: an owner
can appear or die in the gap before delivery, and one of the two branches is
then wrong in a way that either loses the event or writes it twice.

Enqueueing first removes the branch. There is one durable event and a rule
about who may take it, and the active-session lease -- not a preflight guess --
decides that at the moment of consumption:

    A owns S   -> re-enters its own lease -> claims the event
    B does not -> SESSION_NOT_OWNED       -> leaves it alone

If A dies before consuming, its lease is pruned as a dead owner and a later
process may take the event. If a person opens the conversation just as a
fallback gateway starts, exactly one of them holds the lease and the other
declines. The race still happens; there is no longer an unsafe outcome of it.

WHAT THIS IS NOT

It is not a second delivery ledger. The producer's own outbox remains the
delivery authority, and canonical Hermes history remains the record of what
actually happened. This table only needs enough claim durability that two
Hermes processes cannot consume one row. ``event_id`` is the PRODUCER's
identity for the event, so re-enqueueing after an ambiguous outcome is
idempotent here by construction.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

PENDING = "PENDING"
CLAIMED = "CLAIMED"
CONSUMED = "CONSUMED"


def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        _initialize_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    # Mirrors hermes_state_common.SCHEMA_SQL. Repeated here so a producer that
    # never opens a full Hermes state handle still finds the table present.
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="state.db (session_external_turns)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS session_external_turns (
            event_id TEXT PRIMARY KEY,
            target_session_key TEXT NOT NULL,
            body TEXT NOT NULL,
            source TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'PENDING',
            owner_pid INTEGER,
            owner_started_at REAL,
            claimed_at REAL,
            created_at REAL NOT NULL,
            consumed_at REAL,
            last_error TEXT
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_session_external_turns_pending
           ON session_external_turns(target_session_key, state, created_at)"""
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Commit/rollback AND close. ``with _connect()`` alone leaks the handle."""
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _process_start_time(pid: int) -> Optional[float]:
    try:
        import psutil  # type: ignore

        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def _claimer_alive(pid: Any, started_at: Any) -> bool:
    """Is the process that claimed this row still running?

    Identity is (pid, process start time) for the same reason the active-session
    registry uses it: a pid on its own is reused, and a recycled one would keep
    an abandoned claim alive forever.
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        from gateway.status import _pid_exists

        if not _pid_exists(pid_int):
            return False
    except Exception:
        return False
    if started_at is None:
        return True
    current = _process_start_time(pid_int)
    if current is None:
        return True
    try:
        return abs(current - float(started_at)) < 0.001
    except (TypeError, ValueError):
        return True


def enqueue_external_turn(
    *,
    event_id: str,
    target_session_key: str,
    body: str,
    source: str,
) -> bool:
    """Queue one activation for a stored session. Returns False if already queued.

    Idempotent on ``event_id``: a producer that could not tell whether its last
    attempt landed may safely enqueue the same event again, and will not create
    a second turn. Nothing here delivers -- see the module docstring for why the
    producer must not also choose the transport.
    """
    key = str(target_session_key or "").strip()
    eid = str(event_id or "").strip()
    if not eid or not key:
        raise ValueError("event_id and target_session_key are both required")
    with _transaction() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO session_external_turns
               (event_id, target_session_key, body, source, state, created_at)
               VALUES (?, ?, ?, ?, 'PENDING', ?)""",
            (eid, key, str(body), str(source or "external"), time.time()),
        )
        return bool(cur.rowcount)


def pending_external_turns(target_session_key: str, limit: int = 16) -> List[Dict[str, Any]]:
    """Rows this session may consume, oldest first.

    Includes rows whose claimer has died. A process that claimed a row and was
    then killed must not take the event with it -- the person is still waiting
    to be told, and the producer will not re-send an event it believes it handed
    over.
    """
    key = str(target_session_key or "").strip()
    if not key:
        return []
    rows: List[Dict[str, Any]] = []
    with _transaction() as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            """SELECT * FROM session_external_turns
               WHERE target_session_key = ? AND state IN ('PENDING', 'CLAIMED')
               ORDER BY created_at LIMIT ?""",
            (key, int(limit)),
        ):
            record = dict(row)
            if record.get("state") == CLAIMED and _claimer_alive(
                record.get("owner_pid"), record.get("owner_started_at")
            ):
                continue
            rows.append(record)
    return rows


def claim_external_turn(event_id: str) -> bool:
    """Take ownership of one row for THIS process. False if somebody else has it.

    The UPDATE is the whole mutual exclusion: it matches only a row still in the
    state this caller just observed, so two processes racing on one event cannot
    both come away believing they own it. SQLite serialises the write; the loser
    sees rowcount 0.
    """
    eid = str(event_id or "").strip()
    if not eid:
        return False
    pid = os.getpid()
    started = _process_start_time(pid)
    now = time.time()
    with _transaction() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT state, owner_pid, owner_started_at FROM session_external_turns WHERE event_id = ?",
            (eid,),
        ).fetchone()
        if row is None or row["state"] == CONSUMED:
            return False
        if row["state"] == CLAIMED and _claimer_alive(row["owner_pid"], row["owner_started_at"]):
            return False
        cur = conn.execute(
            """UPDATE session_external_turns
               SET state = 'CLAIMED', owner_pid = ?, owner_started_at = ?, claimed_at = ?
               WHERE event_id = ? AND state = ?""",
            (pid, started, now, eid, row["state"]),
        )
        return bool(cur.rowcount)


def mark_external_turn_consumed(event_id: str) -> None:
    """A turn actually started for this event. The row is finished.

    Called once the dispatch is CONFIRMED, never before it. Whether the
    assistant then replied is decided by canonical history, not by this table --
    but whether a turn began at all is decided here, and marking optimistically
    turns a refused dispatch into a silently swallowed event.
    """
    with _transaction() as conn:
        conn.execute(
            "UPDATE session_external_turns SET state = 'CONSUMED', consumed_at = ? WHERE event_id = ?",
            (time.time(), str(event_id)),
        )


def release_external_turn(event_id: str, error: str = "") -> None:
    """Put a claimed row back, because this process could not run it after all."""
    with _transaction() as conn:
        conn.execute(
            """UPDATE session_external_turns
               SET state = 'PENDING', owner_pid = NULL, owner_started_at = NULL,
                   claimed_at = NULL, last_error = ?
               WHERE event_id = ? AND state = 'CLAIMED'""",
            (str(error)[:500] or None, str(event_id)),
        )
