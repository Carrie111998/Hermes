"""Generation/token-fenced task and run claims."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass

from .database import write_txn
from .events import append_event
from .schema import meta_value
from .types import EventRecord, FenceConflict, RunFence


@dataclass(frozen=True, slots=True)
class IssuedClaim:
    fence: RunFence
    profile: str | None
    expires_at: int


def _token_hash(conn, token: str) -> str:
    salt = bytes.fromhex(meta_value(conn, "claim_hash_salt"))
    return hashlib.sha256(salt + token.encode("utf-8")).hexdigest()


def _event(task_id: str, run_id: int, generation: int, kind: str, payload=None) -> EventRecord:
    return EventRecord(
        event_uuid=str(uuid.uuid4()),
        task_id=task_id,
        run_id=run_id,
        claim_generation=generation,
        event_type=kind,
        source="kanban.store.claims",
        severity="info",
        retention_class="lifecycle",
        payload=payload or {},
    )


def issue_claim(
    conn,
    *,
    task_id: str,
    profile: str | None,
    ttl_seconds: int,
    worker_context_digest: str | None = None,
    now: int | None = None,
) -> IssuedClaim:
    """Atomically rotate ownership and return the only plaintext token copy."""

    if ttl_seconds < 30 or ttl_seconds > 24 * 3600:
        raise ValueError("claim TTL must be between 30 seconds and 24 hours")
    current_time = int(time.time()) if now is None else int(now)
    token = secrets.token_urlsafe(48)
    token_hash = _token_hash(conn, token)
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_generation FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not row:
            raise KeyError(task_id)
        if row["status"] not in {"ready", "todo", "queued"}:
            raise FenceConflict(f"task is not claimable from {row['status']}")
        generation = int(row["claim_generation"] or 0) + 1
        expires_at = current_time + ttl_seconds
        cur = conn.execute(
            """
            UPDATE tasks
               SET status='running', claim_generation=?, claim_token_hash=?,
                   claim_expires=?, started_at=COALESCE(started_at, ?)
             WHERE id=? AND claim_generation=? AND status IN ('ready','todo','queued')
            """,
            (
                generation,
                token_hash,
                expires_at,
                current_time,
                task_id,
                generation - 1,
            ),
        )
        if cur.rowcount != 1:
            raise FenceConflict("claim lost a compare-and-swap race")
        run_cur = conn.execute(
            """
            INSERT INTO task_runs(
                task_id, profile, status, claim_lock, claim_expires,
                last_heartbeat_at, started_at, claim_generation,
                claim_token_hash, worker_context_digest
            ) VALUES (?, ?, 'running', NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                profile,
                expires_at,
                current_time,
                current_time,
                generation,
                token_hash,
                worker_context_digest,
            ),
        )
        run_id = int(run_cur.lastrowid)
        conn.execute(
            "UPDATE tasks SET current_run_id=? WHERE id=? AND claim_generation=?",
            (run_id, task_id, generation),
        )
        append_event(
            conn,
            _event(
                task_id,
                run_id,
                generation,
                "run.claimed",
                {"profile": profile, "expires_at": expires_at},
            ),
        )
    return IssuedClaim(
        fence=RunFence(task_id, run_id, generation, token),
        profile=profile,
        expires_at=expires_at,
    )


def verify_fence(conn, fence: RunFence, *, require_running: bool = True):
    row = conn.execute(
        """
        SELECT t.status AS task_status, t.current_run_id, t.claim_generation,
               t.claim_token_hash AS task_token_hash,
               r.status AS run_status, r.claim_generation AS run_generation,
               r.claim_token_hash AS run_token_hash
          FROM tasks t JOIN task_runs r ON r.id=t.current_run_id
         WHERE t.id=? AND r.id=?
        """,
        (fence.task_id, fence.run_id),
    ).fetchone()
    if not row:
        raise FenceConflict("task/run is not current")
    expected = _token_hash(conn, fence.claim_token)
    checks = (
        int(row["current_run_id"]) == fence.run_id,
        int(row["claim_generation"]) == fence.claim_generation,
        int(row["run_generation"]) == fence.claim_generation,
        hmac.compare_digest(str(row["task_token_hash"] or ""), expected),
        hmac.compare_digest(str(row["run_token_hash"] or ""), expected),
    )
    if not all(checks):
        raise FenceConflict("claim fence no longer owns the run")
    if require_running and (
        row["task_status"] != "running" or row["run_status"] != "running"
    ):
        raise FenceConflict("run is no longer active")
    return row


def heartbeat(conn, fence: RunFence, *, ttl_seconds: int, now: int | None = None) -> int:
    current_time = int(time.time()) if now is None else int(now)
    expires = current_time + ttl_seconds
    with write_txn(conn):
        verify_fence(conn, fence)
        cur = conn.execute(
            """
            UPDATE task_runs
               SET last_heartbeat_at=?, claim_expires=?
             WHERE id=? AND claim_generation=? AND status='running'
            """,
            (current_time, expires, fence.run_id, fence.claim_generation),
        )
        if cur.rowcount != 1:
            raise FenceConflict("heartbeat lost run fence")
        cur = conn.execute(
            """
            UPDATE tasks
               SET last_heartbeat_at=?, claim_expires=?
             WHERE id=? AND current_run_id=? AND claim_generation=?
               AND status='running'
            """,
            (
                current_time,
                expires,
                fence.task_id,
                fence.run_id,
                fence.claim_generation,
            ),
        )
        if cur.rowcount != 1:
            raise FenceConflict("heartbeat lost task fence")
        append_event(
            conn,
            _event(
                fence.task_id,
                fence.run_id,
                fence.claim_generation,
                "run.heartbeat",
                {"expires_at": expires},
            ),
        )
    return expires


def invalidate_claim(
    conn,
    *,
    task_id: str,
    run_id: int,
    claim_generation: int,
    reason: str,
    next_state: str = "ready",
    now: int | None = None,
) -> int:
    """Trusted requeue/reclaim invalidates old calls by advancing generation."""

    current_time = int(time.time()) if now is None else int(now)
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, current_run_id, claim_generation FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if not row:
            raise KeyError(task_id)
        if int(row["current_run_id"] or 0) != run_id or int(row["claim_generation"]) != claim_generation:
            raise FenceConflict("invalidation target is stale")
        next_generation = claim_generation + 1
        cur = conn.execute(
            """
            UPDATE tasks
               SET status=?, current_run_id=NULL, claim_generation=?,
                   claim_token_hash=NULL, claim_expires=NULL, worker_pid=NULL
             WHERE id=? AND current_run_id=? AND claim_generation=?
            """,
            (next_state, next_generation, task_id, run_id, claim_generation),
        )
        if cur.rowcount != 1:
            raise FenceConflict("invalidation lost task fence")
        conn.execute(
            """
            UPDATE task_runs
               SET status='released', outcome=?, ended_at=?, claim_token_hash=NULL,
                   claim_expires=NULL
             WHERE id=? AND claim_generation=? AND status='running'
            """,
            (reason, current_time, run_id, claim_generation),
        )
        append_event(
            conn,
            _event(
                task_id,
                run_id,
                claim_generation,
                "run.claim_invalidated",
                {"reason": reason, "next_state": next_state, "next_generation": next_generation},
            ),
        )
    return next_generation
