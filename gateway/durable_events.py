"""Generic durable event leases and recipient idempotency receipts.

This module is deliberately transport-agnostic.  Producers enqueue immutable
JSON events into a named stream, couriers claim them with expiring leases, and
recipients use an inbox receipt before executing a possibly side-effecting
operation.  The two halves close different crash windows:

* the stream lease makes an abandoned courier claim reclaimable; and
* the inbox receipt makes a duplicate delivery return the cached terminal
  result while an expired, ambiguous execution becomes ``indeterminate``
  instead of being run a second time.

Owner and lease-token material is never stored verbatim.  A per-database
random salt keys the hashes persisted in ``state.db``.  Public reads expose
lease timing/generation and terminal outcomes, but never those hashes.

All APIs accept an explicit database path so callers keep physical profile /
install ownership authoritative.  Writes use short ``BEGIN IMMEDIATE``
transactions; JSON serialization and validation happen before acquiring the
write lock.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


BUSY_TIMEOUT_MS = 10_000
MAX_EVENT_JSON_BYTES = 256 * 1024
MAX_OUTCOME_JSON_BYTES = 1024 * 1024
MAX_INBOX_IDENTITY_JSON_BYTES = 64 * 1024
MAX_INBOX_RESULT_JSON_BYTES = 1024 * 1024
MAX_BATCH_SIZE = 100
MAX_LEASE_SECONDS = 24 * 60 * 60
MAX_RETRY_AFTER_SECONDS = 7 * 24 * 60 * 60

_MAX_NAME_CHARS = 256
_MAX_OWNER_CHARS = 512
_MAX_ERROR_CHARS = 4096
_MAX_JSON_DEPTH = 32
_EVENT_TERMINAL_STATES = frozenset({"acked", "failed", "expired"})
_INBOX_TERMINAL_STATES = frozenset({
    "succeeded",
    "failed",
    "cancelled",
    "indeterminate",
})
_INBOX_FINISH_STATES = frozenset({"succeeded", "failed", "cancelled", "indeterminate"})

_EXPIRED_OUTCOME = {
    "status": "expired",
    "reason": "event_deadline_exceeded",
}
_INDETERMINATE_RESULT = {
    "status": "indeterminate",
    "reason": "processing_lease_expired",
    "error": (
        "recipient processing ended without a durable result; "
        "the turn may have produced side effects and was not re-run"
    ),
}


class DurableEventError(RuntimeError):
    """Base class for durable-event contract failures."""


class EventConflict(DurableEventError):
    """An event id was reused with different immutable content."""


class LeaseMismatch(DurableEventError):
    """The supplied lease is stale, expired, foreign, or otherwise invalid.

    The error is intentionally opaque: callers must not be able to probe the
    stored owner/token/generation by varying one credential at a time.
    """

    def __init__(self) -> None:
        super().__init__("lease does not match")


class InboxConflict(DurableEventError):
    """An inbox id was reused for a different identity or payload."""


class InboxMismatch(DurableEventError):
    """A recipient tried to settle a stale or foreign processing receipt."""

    def __init__(self) -> None:
        super().__init__("inbox receipt does not match")


def _now(value: Optional[float]) -> float:
    result = time.time() if value is None else float(value)
    if not math.isfinite(result):
        raise ValueError("now must be finite")
    return result


def _finite_timestamp(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a timestamp") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _positive_seconds(value: Any, name: str, maximum: float) -> float:
    result = _finite_timestamp(value, name)
    if result <= 0 or result > maximum:
        raise ValueError(f"{name} must be > 0 and <= {maximum:g}")
    return result


def _bounded_nonnegative_seconds(value: Any, name: str, maximum: float) -> float:
    result = _finite_timestamp(value, name)
    if result < 0 or result > maximum:
        raise ValueError(f"{name} must be >= 0 and <= {maximum:g}")
    return result


def _name(value: Any, field: str, *, maximum: int = _MAX_NAME_CHARS) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    result = value.strip()
    if not result or len(result) > maximum or "\x00" in result:
        raise ValueError(f"{field} is invalid")
    return result


def _digest(value: Any, field: str) -> str:
    result = _name(value, field, maximum=64).lower()
    if len(result) != 64 or any(ch not in "0123456789abcdef" for ch in result):
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return result


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("JSON nesting is too deep")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            _validate_json_value(item, depth=depth + 1)
        return
    raise ValueError(f"unsupported JSON value: {type(value).__name__}")


def _canonical_dict(
    value: Any,
    field: str,
    *,
    maximum_bytes: int,
) -> Tuple[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    _validate_json_value(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not valid JSON") from exc
    raw = encoded.encode("utf-8")
    if len(raw) > maximum_bytes:
        raise ValueError(f"{field} exceeds {maximum_bytes} bytes")
    return encoded, hashlib.sha256(raw).hexdigest()


def json_digest(value: Dict[str, Any]) -> str:
    """Return the canonical SHA-256 digest used by ACK/inbox contracts."""

    _, result = _canonical_dict(
        value,
        "value",
        maximum_bytes=MAX_OUTCOME_JSON_BYTES,
    )
    return result


def _immutable_event_digest(
    route_namespace: str,
    payload_json: str,
) -> str:
    material = json.dumps(
        {
            "payload": json.loads(payload_json),
            "route_namespace": route_namespace,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _immutable_inbox_digest(
    identity_json: str,
    payload_hash: str,
    lane_key: str,
) -> str:
    material = (
        b"hermes-durable-inbox-v2\x00"
        + lane_key.encode("utf-8")
        + b"\x00"
        + identity_json.encode("utf-8")
        + b"\x00"
        + payload_hash.encode("ascii")
    )
    return hashlib.sha256(material).hexdigest()


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS durable_event_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS durable_events (
            stream TEXT NOT NULL,
            event_id TEXT NOT NULL,
            route_namespace TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            immutable_digest TEXT NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            generation INTEGER NOT NULL DEFAULT 0,
            available_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            lease_owner_hash TEXT,
            lease_token_hash TEXT,
            lease_expires_at REAL,
            outcome_json TEXT,
            outcome_digest TEXT,
            last_error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            terminal_at REAL,
            PRIMARY KEY (stream, event_id),
            CHECK (state IN ('queued', 'leased', 'acked', 'failed', 'expired')),
            CHECK (attempts >= 0),
            CHECK (generation >= 0)
        );

        CREATE INDEX IF NOT EXISTS durable_events_claim_idx
            ON durable_events (
                stream, route_namespace, state, available_at,
                lease_expires_at, expires_at, created_at
            );
        CREATE INDEX IF NOT EXISTS durable_events_terminal_idx
            ON durable_events (terminal_at)
            WHERE state IN ('acked', 'failed', 'expired');

        CREATE TABLE IF NOT EXISTS durable_inbox_receipts (
            inbox TEXT NOT NULL,
            event_id TEXT NOT NULL,
            lane_key TEXT NOT NULL,
            identity_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            immutable_digest TEXT NOT NULL,
            state TEXT NOT NULL,
            processing_token_hash TEXT,
            processing_expires_at REAL,
            result_json TEXT,
            result_digest TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            terminal_at REAL,
            PRIMARY KEY (inbox, event_id),
            CHECK (state IN (
                'processing', 'succeeded', 'failed', 'cancelled', 'indeterminate'
            ))
        );

        CREATE INDEX IF NOT EXISTS durable_inbox_terminal_idx
            ON durable_inbox_receipts (terminal_at)
            WHERE state != 'processing';
        CREATE UNIQUE INDEX IF NOT EXISTS durable_inbox_lane_processing_idx
            ON durable_inbox_receipts (inbox, lane_key)
            WHERE state = 'processing';
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO durable_event_meta(key, value) VALUES ('hash_salt', ?)",
        (secrets.token_hex(32),),
    )


def _connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        # Windows and some mounted filesystems do not implement POSIX modes.
        pass
    try:
        target_stat = os.lstat(path)
    except FileNotFoundError:
        target_stat = None
    if target_stat is not None and not stat.S_ISREG(target_stat.st_mode):
        raise OSError(f"durable event database is not a regular file: {path}")
    conn = sqlite3.connect(
        str(path),
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    try:
        # The private parent plus the lstat checks make the usual path safe;
        # sqlite3 does not expose SQLITE_OPEN_NOFOLLOW, so repeat the check
        # immediately after opening to narrow a same-user replacement race.
        opened_stat = os.lstat(path)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise OSError(f"durable event database is not a regular file: {path}")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(
            conn,
            db_label=f"state.db durable events ({path.name})",
        )
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("PRAGMA cell_size_check=ON")
        _schema(conn)
        return conn
    except Exception:
        conn.close()
        raise


@contextmanager
def _closing_connection(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _hash_material(conn: sqlite3.Connection, kind: str, value: str) -> str:
    row = conn.execute(
        "SELECT value FROM durable_event_meta WHERE key='hash_salt'"
    ).fetchone()
    if row is None:  # schema initialization guarantees this; fail closed.
        raise DurableEventError("durable event hash key is unavailable")
    key = bytes.fromhex(str(row[0]))
    return hmac.new(
        key,
        f"{kind}\x00{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _same(left: str, right: str) -> bool:
    return hmac.compare_digest(str(left or ""), str(right or ""))


def _public_event(row: sqlite3.Row) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "stream": row["stream"],
        "event_id": row["event_id"],
        "route_namespace": row["route_namespace"],
        "payload": json.loads(row["payload_json"]),
        "payload_digest": row["payload_digest"],
        "state": row["state"],
        "attempts": int(row["attempts"]),
        "generation": int(row["generation"]),
        "available_at": float(row["available_at"]),
        "expires_at": float(row["expires_at"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }
    if row["lease_expires_at"] is not None:
        result["lease_expires_at"] = float(row["lease_expires_at"])
    if row["outcome_json"] is not None:
        result["outcome"] = json.loads(row["outcome_json"])
        result["outcome_digest"] = row["outcome_digest"]
    if row["last_error"] is not None:
        result["last_error"] = row["last_error"]
    if row["terminal_at"] is not None:
        result["terminal_at"] = float(row["terminal_at"])
    return result


def _select_event(
    conn: sqlite3.Connection,
    stream: str,
    event_id: str,
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM durable_events WHERE stream=? AND event_id=?",
        (stream, event_id),
    ).fetchone()


def _expired_outcome() -> Tuple[str, str]:
    return _canonical_dict(
        _EXPIRED_OUTCOME,
        "expired outcome",
        maximum_bytes=MAX_OUTCOME_JSON_BYTES,
    )


def _indeterminate_result() -> Tuple[str, str]:
    return _canonical_dict(
        _INDETERMINATE_RESULT,
        "indeterminate result",
        maximum_bytes=MAX_INBOX_RESULT_JSON_BYTES,
    )


def _terminalize_expired_events(
    conn: sqlite3.Connection,
    now: float,
    *,
    stream: Optional[str] = None,
    route_namespace: Optional[str] = None,
) -> int:
    outcome_json, outcome_digest = _expired_outcome()
    where = ["state IN ('queued', 'leased')", "expires_at <= ?"]
    args: List[Any] = [now]
    if stream is not None:
        where.append("stream = ?")
        args.append(stream)
    if route_namespace is not None:
        where.append("route_namespace = ?")
        args.append(route_namespace)
    cursor = conn.execute(
        f"""UPDATE durable_events
            SET state='expired', outcome_json=?, outcome_digest=?,
                last_error='event deadline exceeded',
                lease_owner_hash=NULL, lease_token_hash=NULL,
                lease_expires_at=NULL, updated_at=?, terminal_at=?
            WHERE {" AND ".join(where)}""",
        (outcome_json, outcome_digest, now, now, *args),
    )
    return max(0, cursor.rowcount)


def enqueue(
    db_path: Path | str,
    stream: str,
    event_id: str,
    payload: Dict[str, Any],
    route_namespace: str,
    expires_at: float,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Create one immutable event, or return the existing identical event.

    Reusing ``(stream, event_id)`` with any different immutable field raises
    :class:`EventConflict`; it can never reset attempts or terminal state.
    """

    current = _now(now)
    stream = _name(stream, "stream")
    event_id = _name(event_id, "event_id")
    route_namespace = _name(route_namespace, "route_namespace")
    deadline = _finite_timestamp(expires_at, "expires_at")
    payload_json, payload_digest = _canonical_dict(
        payload,
        "payload",
        maximum_bytes=MAX_EVENT_JSON_BYTES,
    )
    immutable_digest = _immutable_event_digest(route_namespace, payload_json)

    with _closing_connection(db_path) as conn, _immediate(conn):
        existing = _select_event(conn, stream, event_id)
        if existing is not None:
            if not _same(existing["immutable_digest"], immutable_digest):
                raise EventConflict("event identity already has different content")
            result = _public_event(existing)
            result["idempotent"] = True
            return result
        if deadline <= current:
            raise ValueError("expires_at must be in the future for a new event")
        conn.execute(
            """INSERT INTO durable_events (
                   stream, event_id, route_namespace, payload_json,
                   payload_digest, immutable_digest, state, attempts,
                   generation, available_at, expires_at, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, 0, ?, ?, ?, ?)""",
            (
                stream,
                event_id,
                route_namespace,
                payload_json,
                payload_digest,
                immutable_digest,
                current,
                deadline,
                current,
                current,
            ),
        )
        row = _select_event(conn, stream, event_id)
        assert row is not None
        result = _public_event(row)
        result["idempotent"] = False
        return result


def claim(
    db_path: Path | str,
    stream: str,
    route_namespace: str,
    owner: str,
    limit: int,
    lease_seconds: float,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Lease queued events and reclaim expired leases for one route namespace."""

    current = _now(now)
    stream = _name(stream, "stream")
    route_namespace = _name(route_namespace, "route_namespace")
    owner = _name(owner, "owner", maximum=_MAX_OWNER_CHARS)
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not (1 <= limit <= MAX_BATCH_SIZE)
    ):
        raise ValueError(f"limit must be an integer from 1 to {MAX_BATCH_SIZE}")
    lease_for = _positive_seconds(
        lease_seconds,
        "lease_seconds",
        MAX_LEASE_SECONDS,
    )
    claimed: List[Dict[str, Any]] = []

    with _closing_connection(db_path) as conn, _immediate(conn):
        _terminalize_expired_events(
            conn,
            current,
            stream=stream,
            route_namespace=route_namespace,
        )
        owner_hash = _hash_material(conn, "event-owner", owner)
        candidates = conn.execute(
            """SELECT * FROM durable_events
               WHERE stream=? AND route_namespace=? AND expires_at > ?
                 AND (
                     (state='queued' AND available_at <= ?)
                     OR
                     (state='leased' AND lease_expires_at <= ?)
                 )
               ORDER BY available_at, created_at, event_id
               LIMIT ?""",
            (stream, route_namespace, current, current, current, limit),
        ).fetchall()
        for previous in candidates:
            token = secrets.token_urlsafe(32)
            token_hash = _hash_material(conn, "event-token", token)
            generation = int(previous["generation"]) + 1
            attempts = int(previous["attempts"]) + 1
            lease_expires_at = min(
                current + lease_for,
                float(previous["expires_at"]),
            )
            if previous["state"] == "queued":
                guard = "state='queued' AND generation=? AND available_at <= ?"
                guard_args: Tuple[Any, ...] = (
                    int(previous["generation"]),
                    current,
                )
            else:
                guard = "state='leased' AND generation=? AND lease_expires_at <= ?"
                guard_args = (int(previous["generation"]), current)
            cursor = conn.execute(
                f"""UPDATE durable_events
                    SET state='leased', attempts=?, generation=?,
                        lease_owner_hash=?, lease_token_hash=?,
                        lease_expires_at=?, updated_at=?
                    WHERE stream=? AND event_id=? AND {guard}
                      AND expires_at > ?""",
                (
                    attempts,
                    generation,
                    owner_hash,
                    token_hash,
                    lease_expires_at,
                    current,
                    stream,
                    previous["event_id"],
                    *guard_args,
                    current,
                ),
            )
            if cursor.rowcount != 1:
                continue
            row = _select_event(conn, stream, previous["event_id"])
            assert row is not None
            item = _public_event(row)
            item["lease_token"] = token
            claimed.append(item)
    return claimed


def _lease_hashes(
    conn: sqlite3.Connection,
    owner: str,
    token: str,
) -> Tuple[str, str]:
    return (
        _hash_material(conn, "event-owner", owner),
        _hash_material(conn, "event-token", token),
    )


def _live_lease_matches(
    row: Optional[sqlite3.Row],
    *,
    owner_hash: str,
    token_hash: str,
    generation: int,
    now: float,
) -> bool:
    return bool(
        row is not None
        and row["state"] == "leased"
        and int(row["generation"]) == generation
        and _same(row["lease_owner_hash"], owner_hash)
        and _same(row["lease_token_hash"], token_hash)
        and row["lease_expires_at"] is not None
        and float(row["lease_expires_at"]) > now
        and float(row["expires_at"]) > now
    )


def renew(
    db_path: Path | str,
    stream: str,
    event_id: str,
    owner: str,
    lease_token: str,
    generation: int,
    lease_seconds: float,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Extend a current live lease without changing generation/attempts."""

    current = _now(now)
    stream = _name(stream, "stream")
    event_id = _name(event_id, "event_id")
    owner = _name(owner, "owner", maximum=_MAX_OWNER_CHARS)
    lease_token = _name(lease_token, "lease_token", maximum=_MAX_OWNER_CHARS)
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
    ):
        raise ValueError("generation must be a positive integer")
    lease_for = _positive_seconds(
        lease_seconds,
        "lease_seconds",
        MAX_LEASE_SECONDS,
    )

    with _closing_connection(db_path) as conn, _immediate(conn):
        owner_hash, token_hash = _lease_hashes(conn, owner, lease_token)
        row = _select_event(conn, stream, event_id)
        if not _live_lease_matches(
            row,
            owner_hash=owner_hash,
            token_hash=token_hash,
            generation=generation,
            now=current,
        ):
            raise LeaseMismatch()
        assert row is not None
        lease_expires_at = min(
            float(row["expires_at"]),
            max(float(row["lease_expires_at"]), current + lease_for),
        )
        cursor = conn.execute(
            """UPDATE durable_events
               SET lease_expires_at=?, updated_at=?
               WHERE stream=? AND event_id=? AND state='leased'
                 AND generation=? AND lease_owner_hash=? AND lease_token_hash=?
                 AND lease_expires_at > ? AND expires_at > ?""",
            (
                lease_expires_at,
                current,
                stream,
                event_id,
                generation,
                owner_hash,
                token_hash,
                current,
                current,
            ),
        )
        if cursor.rowcount != 1:
            raise LeaseMismatch()
        updated = _select_event(conn, stream, event_id)
        assert updated is not None
        return _public_event(updated)


def ack(
    db_path: Path | str,
    stream: str,
    event_id: str,
    owner: str,
    lease_token: str,
    generation: int,
    outcome: Dict[str, Any],
    outcome_digest: str,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Settle a live lease successfully.

    Repeating the exact same ACK with the same lease and outcome is
    idempotent even after the lease's clock expires.  Any other terminal,
    stale, conflicting, or foreign settlement raises one opaque
    :class:`LeaseMismatch`.
    """

    current = _now(now)
    stream = _name(stream, "stream")
    event_id = _name(event_id, "event_id")
    owner = _name(owner, "owner", maximum=_MAX_OWNER_CHARS)
    lease_token = _name(lease_token, "lease_token", maximum=_MAX_OWNER_CHARS)
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
    ):
        raise ValueError("generation must be a positive integer")
    outcome_json, computed_digest = _canonical_dict(
        outcome,
        "outcome",
        maximum_bytes=MAX_OUTCOME_JSON_BYTES,
    )
    supplied_digest = _digest(outcome_digest, "outcome_digest")
    if not _same(computed_digest, supplied_digest):
        raise ValueError("outcome_digest does not match outcome")

    with _closing_connection(db_path) as conn, _immediate(conn):
        owner_hash, token_hash = _lease_hashes(conn, owner, lease_token)
        row = _select_event(conn, stream, event_id)
        if row is not None and row["state"] == "acked":
            if (
                int(row["generation"]) == generation
                and _same(row["lease_owner_hash"], owner_hash)
                and _same(row["lease_token_hash"], token_hash)
                and _same(row["outcome_digest"], computed_digest)
                and row["outcome_json"] == outcome_json
            ):
                result = _public_event(row)
                result["idempotent"] = True
                return result
            raise LeaseMismatch()
        if not _live_lease_matches(
            row,
            owner_hash=owner_hash,
            token_hash=token_hash,
            generation=generation,
            now=current,
        ):
            raise LeaseMismatch()
        cursor = conn.execute(
            """UPDATE durable_events
               SET state='acked', outcome_json=?, outcome_digest=?,
                   last_error=NULL, updated_at=?, terminal_at=?
               WHERE stream=? AND event_id=? AND state='leased'
                 AND generation=? AND lease_owner_hash=? AND lease_token_hash=?
                 AND lease_expires_at > ? AND expires_at > ?""",
            (
                outcome_json,
                computed_digest,
                current,
                current,
                stream,
                event_id,
                generation,
                owner_hash,
                token_hash,
                current,
                current,
            ),
        )
        if cursor.rowcount != 1:
            raise LeaseMismatch()
        updated = _select_event(conn, stream, event_id)
        assert updated is not None
        result = _public_event(updated)
        result["idempotent"] = False
        return result


def _bounded_error(error: Any) -> str:
    value = str(error or "delivery failed")
    return value[:_MAX_ERROR_CHARS]


def nack(
    db_path: Path | str,
    stream: str,
    event_id: str,
    owner: str,
    lease_token: str,
    generation: int,
    error: str,
    retryable: bool,
    retry_after_seconds: float,
    max_attempts: int,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Release or terminalize a live lease after a failed attempt."""

    current = _now(now)
    stream = _name(stream, "stream")
    event_id = _name(event_id, "event_id")
    owner = _name(owner, "owner", maximum=_MAX_OWNER_CHARS)
    lease_token = _name(lease_token, "lease_token", maximum=_MAX_OWNER_CHARS)
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
    ):
        raise ValueError("generation must be a positive integer")
    if not isinstance(retryable, bool):
        raise ValueError("retryable must be boolean")
    retry_delay = _bounded_nonnegative_seconds(
        retry_after_seconds,
        "retry_after_seconds",
        MAX_RETRY_AFTER_SECONDS,
    )
    if (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or max_attempts <= 0
    ):
        raise ValueError("max_attempts must be a positive integer")
    error_text = _bounded_error(error)

    with _closing_connection(db_path) as conn, _immediate(conn):
        owner_hash, token_hash = _lease_hashes(conn, owner, lease_token)
        row = _select_event(conn, stream, event_id)
        if not _live_lease_matches(
            row,
            owner_hash=owner_hash,
            token_hash=token_hash,
            generation=generation,
            now=current,
        ):
            raise LeaseMismatch()
        assert row is not None
        deadline = float(row["expires_at"])
        attempts = int(row["attempts"])
        if retryable and attempts < max_attempts and current + retry_delay < deadline:
            cursor = conn.execute(
                """UPDATE durable_events
                   SET state='queued', available_at=?, last_error=?,
                       lease_owner_hash=NULL, lease_token_hash=NULL,
                       lease_expires_at=NULL, updated_at=?
                   WHERE stream=? AND event_id=? AND state='leased'
                     AND generation=? AND lease_owner_hash=? AND lease_token_hash=?
                     AND lease_expires_at > ? AND expires_at > ?""",
                (
                    current + retry_delay,
                    error_text,
                    current,
                    stream,
                    event_id,
                    generation,
                    owner_hash,
                    token_hash,
                    current,
                    current,
                ),
            )
        else:
            if retryable and current + retry_delay >= deadline:
                state = "expired"
                outcome = _EXPIRED_OUTCOME
            else:
                state = "failed"
                outcome = {
                    "status": "failed",
                    "reason": (
                        "max_attempts_exhausted" if retryable else "non_retryable"
                    ),
                    "error": error_text,
                }
            outcome_json, outcome_digest = _canonical_dict(
                outcome,
                "terminal outcome",
                maximum_bytes=MAX_OUTCOME_JSON_BYTES,
            )
            cursor = conn.execute(
                """UPDATE durable_events
                   SET state=?, outcome_json=?, outcome_digest=?, last_error=?,
                       lease_owner_hash=NULL, lease_token_hash=NULL,
                       lease_expires_at=NULL, updated_at=?, terminal_at=?
                   WHERE stream=? AND event_id=? AND state='leased'
                     AND generation=? AND lease_owner_hash=? AND lease_token_hash=?
                     AND lease_expires_at > ? AND expires_at > ?""",
                (
                    state,
                    outcome_json,
                    outcome_digest,
                    error_text,
                    current,
                    current,
                    stream,
                    event_id,
                    generation,
                    owner_hash,
                    token_hash,
                    current,
                    current,
                ),
            )
        if cursor.rowcount != 1:
            raise LeaseMismatch()
        updated = _select_event(conn, stream, event_id)
        assert updated is not None
        return _public_event(updated)


def get_event(
    db_path: Path | str,
    stream: str,
    event_id: str,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Return one event, including a cached terminal outcome when present."""

    current = _now(now)
    stream = _name(stream, "stream")
    event_id = _name(event_id, "event_id")
    with _closing_connection(db_path) as conn:
        row = _select_event(conn, stream, event_id)
        if (
            row is not None
            and row["state"] in {"queued", "leased"}
            and float(row["expires_at"]) <= current
        ):
            with _immediate(conn):
                _terminalize_expired_events(conn, current, stream=stream)
                row = _select_event(conn, stream, event_id)
        return _public_event(row) if row is not None else None


def _public_inbox(row: sqlite3.Row, *, action: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "action": action,
        "inbox": row["inbox"],
        "event_id": row["event_id"],
        "lane": row["lane_key"],
        "status": row["state"],
        "payload_hash": row["payload_hash"],
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }
    if row["processing_expires_at"] is not None:
        result["processing_expires_at"] = float(row["processing_expires_at"])
    if row["result_json"] is not None:
        result["result"] = json.loads(row["result_json"])
        result["result_digest"] = row["result_digest"]
    if row["terminal_at"] is not None:
        result["terminal_at"] = float(row["terminal_at"])
    return result


def _processing_inbox(row: sqlite3.Row, now: float) -> Dict[str, Any]:
    result = _public_inbox(row, action="processing")
    result["retry_after_seconds"] = max(
        0.0,
        float(row["processing_expires_at"] or now) - now,
    )
    return result


def _select_inbox(
    conn: sqlite3.Connection,
    inbox: str,
    event_id: str,
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM durable_inbox_receipts WHERE inbox=? AND event_id=?",
        (inbox, event_id),
    ).fetchone()


def _mark_inbox_indeterminate(
    conn: sqlite3.Connection,
    inbox: str,
    event_id: str,
    now: float,
) -> sqlite3.Row:
    result_json, result_digest = _indeterminate_result()
    conn.execute(
        """UPDATE durable_inbox_receipts
           SET state='indeterminate', result_json=?, result_digest=?,
               updated_at=?, terminal_at=?
           WHERE inbox=? AND event_id=? AND state='processing'
             AND processing_expires_at <= ?""",
        (result_json, result_digest, now, now, inbox, event_id, now),
    )
    row = _select_inbox(conn, inbox, event_id)
    assert row is not None
    return row


def _terminalize_expired_inbox_lane(
    conn: sqlite3.Connection,
    inbox: str,
    lane_key: str,
    now: float,
) -> int:
    result_json, result_digest = _indeterminate_result()
    cursor = conn.execute(
        """UPDATE durable_inbox_receipts
           SET state='indeterminate', result_json=?, result_digest=?,
               updated_at=?, terminal_at=?
           WHERE inbox=? AND lane_key=? AND state='processing'
             AND processing_expires_at <= ?""",
        (
            result_json,
            result_digest,
            now,
            now,
            inbox,
            lane_key,
            now,
        ),
    )
    return max(0, cursor.rowcount)


def begin_inbox(
    db_path: Path | str,
    inbox: str,
    event_id: str,
    identity: Dict[str, Any],
    payload_hash: str,
    processing_seconds: float,
    lane: str = "default",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Claim recipient execution once, or describe the existing receipt.

    An expired ``processing`` receipt is terminalized as ``indeterminate``.
    It is deliberately never converted back to ``execute`` because the prior
    worker may have committed an external side effect before crashing.
    """

    current = _now(now)
    inbox = _name(inbox, "inbox")
    event_id = _name(event_id, "event_id")
    lane_key = _name(lane, "lane")
    payload_hash = _digest(payload_hash, "payload_hash")
    processing_for = _positive_seconds(
        processing_seconds,
        "processing_seconds",
        MAX_LEASE_SECONDS,
    )
    identity_json, _ = _canonical_dict(
        identity,
        "identity",
        maximum_bytes=MAX_INBOX_IDENTITY_JSON_BYTES,
    )
    immutable_digest = _immutable_inbox_digest(
        identity_json,
        payload_hash,
        lane_key,
    )

    with _closing_connection(db_path) as conn, _immediate(conn):
        existing = _select_inbox(conn, inbox, event_id)
        if existing is not None:
            if not _same(existing["immutable_digest"], immutable_digest):
                raise InboxConflict("inbox identity already has different content")
            if existing["state"] == "processing":
                if float(existing["processing_expires_at"] or 0) <= current:
                    existing = _mark_inbox_indeterminate(
                        conn,
                        inbox,
                        event_id,
                        current,
                    )
                    return _public_inbox(existing, action="indeterminate")
                return _processing_inbox(existing, current)
            action = (
                "indeterminate" if existing["state"] == "indeterminate" else "cached"
            )
            return _public_inbox(existing, action=action)

        # The lane is an inbox-scoped single-writer lock.  Resolve ambiguous
        # expired work before admitting a different event; it remains cached
        # as indeterminate and is never re-executed.
        _terminalize_expired_inbox_lane(conn, inbox, lane_key, current)
        lane_owner = conn.execute(
            """SELECT * FROM durable_inbox_receipts
               WHERE inbox=? AND lane_key=? AND state='processing'
                 AND processing_expires_at > ?
               ORDER BY processing_expires_at, event_id
               LIMIT 1""",
            (inbox, lane_key, current),
        ).fetchone()
        if lane_owner is not None:
            return {
                "action": "processing",
                "inbox": inbox,
                "event_id": event_id,
                "lane": lane_key,
                "status": "processing",
                "processing_expires_at": float(lane_owner["processing_expires_at"]),
                "retry_after_seconds": max(
                    0.0,
                    float(lane_owner["processing_expires_at"]) - current,
                ),
            }

        token = secrets.token_urlsafe(32)
        token_hash = _hash_material(conn, "inbox-token", token)
        processing_expires_at = current + processing_for
        conn.execute(
            """INSERT INTO durable_inbox_receipts (
                   inbox, event_id, lane_key, identity_json, payload_hash,
                   immutable_digest, state, processing_token_hash,
                   processing_expires_at, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, 'processing', ?, ?, ?, ?)""",
            (
                inbox,
                event_id,
                lane_key,
                identity_json,
                payload_hash,
                immutable_digest,
                token_hash,
                processing_expires_at,
                current,
                current,
            ),
        )
        row = _select_inbox(conn, inbox, event_id)
        assert row is not None
        result = _public_inbox(row, action="execute")
        result["token"] = token
        result["execution_token"] = token
        return result


def finish_inbox(
    db_path: Path | str,
    inbox: str,
    event_id: str,
    execution_token: str,
    status: str,
    result: Dict[str, Any],
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Immutably cache a recipient result under its live processing token."""

    current = _now(now)
    inbox = _name(inbox, "inbox")
    event_id = _name(event_id, "event_id")
    execution_token = _name(
        execution_token,
        "execution_token",
        maximum=_MAX_OWNER_CHARS,
    )
    status = _name(status, "status", maximum=32).lower()
    if status not in _INBOX_FINISH_STATES:
        raise ValueError(
            "status must be one of: " + ", ".join(sorted(_INBOX_FINISH_STATES))
        )
    result_json, result_digest = _canonical_dict(
        result,
        "result",
        maximum_bytes=MAX_INBOX_RESULT_JSON_BYTES,
    )

    expired_processing = False
    response: Optional[Dict[str, Any]] = None
    with _closing_connection(db_path) as conn, _immediate(conn):
        token_hash = _hash_material(conn, "inbox-token", execution_token)
        row = _select_inbox(conn, inbox, event_id)
        if row is not None and row["state"] in _INBOX_TERMINAL_STATES:
            if (
                row["state"] == status
                and _same(row["processing_token_hash"], token_hash)
                and _same(row["result_digest"], result_digest)
                and row["result_json"] == result_json
            ):
                response = _public_inbox(row, action="cached")
                response["idempotent"] = True
                return response
            raise InboxMismatch()
        if (
            row is None
            or row["state"] != "processing"
            or not _same(row["processing_token_hash"], token_hash)
        ):
            raise InboxMismatch()
        if float(row["processing_expires_at"] or 0) <= current:
            _mark_inbox_indeterminate(conn, inbox, event_id, current)
            expired_processing = True
        else:
            cursor = conn.execute(
                """UPDATE durable_inbox_receipts
                   SET state=?, result_json=?, result_digest=?,
                       updated_at=?, terminal_at=?
                   WHERE inbox=? AND event_id=? AND state='processing'
                     AND processing_token_hash=? AND processing_expires_at > ?""",
                (
                    status,
                    result_json,
                    result_digest,
                    current,
                    current,
                    inbox,
                    event_id,
                    token_hash,
                    current,
                ),
            )
            if cursor.rowcount != 1:
                raise InboxMismatch()
            updated = _select_inbox(conn, inbox, event_id)
            assert updated is not None
            response = _public_inbox(updated, action="cached")
            response["idempotent"] = False
    if expired_processing:
        raise InboxMismatch()
    assert response is not None
    return response


def cleanup(
    db_path: Path | str,
    *,
    terminal_before: Optional[float] = None,
    retention_seconds: Optional[float] = None,
    stream: Optional[str] = None,
    inbox: Optional[str] = None,
    now: Optional[float] = None,
    limit: int = 1000,
) -> Dict[str, int]:
    """Terminalize expired active rows, then delete old terminal rows only.

    ``queued``, live ``leased``, and live inbox ``processing`` rows are never
    deleted.  Counts distinguish state settlement from retention deletion so
    callers can audit both effects.
    """

    current = _now(now)
    if (terminal_before is None) == (retention_seconds is None):
        raise ValueError("provide exactly one of terminal_before or retention_seconds")
    if retention_seconds is not None:
        retention = _finite_timestamp(retention_seconds, "retention_seconds")
        if retention < 0:
            raise ValueError("retention_seconds must be non-negative")
        cutoff = current - retention
    else:
        assert terminal_before is not None
        cutoff = _finite_timestamp(terminal_before, "terminal_before")
    stream = _name(stream, "stream") if stream is not None else None
    inbox = _name(inbox, "inbox") if inbox is not None else None
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not (1 <= limit <= 10_000)
    ):
        raise ValueError("limit must be an integer from 1 to 10000")
    expired = 0
    inbox_indeterminate = 0
    events_deleted = 0
    inbox_deleted = 0
    indeterminate_json, indeterminate_digest = _indeterminate_result()

    with _closing_connection(db_path) as conn, _immediate(conn):
        # Supplying one namespace selector intentionally leaves the other
        # substrate untouched.  That lets one consumer enforce its retention
        # policy without pruning unrelated streams/inboxes in shared state.db.
        maintain_events = stream is not None or inbox is None
        maintain_inbox = inbox is not None or stream is None
        if maintain_events:
            expired = _terminalize_expired_events(
                conn,
                current,
                stream=stream,
            )
            event_scope = " AND stream=?" if stream is not None else ""
            event_args: Tuple[Any, ...] = (
                (cutoff, stream, limit) if stream is not None else (cutoff, limit)
            )
            cursor = conn.execute(
                f"""DELETE FROM durable_events WHERE rowid IN (
                       SELECT rowid FROM durable_events
                       WHERE state IN ('acked', 'failed', 'expired')
                         AND terminal_at <= ?{event_scope}
                       ORDER BY terminal_at, rowid
                       LIMIT ?
                   )""",
                event_args,
            )
            events_deleted = max(0, cursor.rowcount)
        if maintain_inbox:
            inbox_scope = " AND inbox=?" if inbox is not None else ""
            update_args: Tuple[Any, ...] = (
                (
                    indeterminate_json,
                    indeterminate_digest,
                    current,
                    current,
                    current,
                    inbox,
                )
                if inbox is not None
                else (
                    indeterminate_json,
                    indeterminate_digest,
                    current,
                    current,
                    current,
                )
            )
            cursor = conn.execute(
                f"""UPDATE durable_inbox_receipts
                   SET state='indeterminate', result_json=?, result_digest=?,
                       updated_at=?, terminal_at=?
                   WHERE state='processing' AND processing_expires_at <= ?
                   {inbox_scope}""",
                update_args,
            )
            inbox_indeterminate = max(0, cursor.rowcount)
            delete_args: Tuple[Any, ...] = (
                (cutoff, inbox, limit) if inbox is not None else (cutoff, limit)
            )
            cursor = conn.execute(
                f"""DELETE FROM durable_inbox_receipts WHERE rowid IN (
                       SELECT rowid FROM durable_inbox_receipts
                       WHERE state IN (
                           'succeeded', 'failed', 'cancelled', 'indeterminate'
                       )
                         AND terminal_at <= ?{inbox_scope}
                       ORDER BY terminal_at, rowid
                       LIMIT ?
                   )""",
                delete_args,
            )
            inbox_deleted = max(0, cursor.rowcount)
    return {
        "events_expired": expired,
        "inbox_indeterminate": inbox_indeterminate,
        "events_deleted": events_deleted,
        "inbox_deleted": inbox_deleted,
    }


__all__ = [
    "BUSY_TIMEOUT_MS",
    "DurableEventError",
    "EventConflict",
    "InboxConflict",
    "InboxMismatch",
    "LeaseMismatch",
    "MAX_BATCH_SIZE",
    "MAX_EVENT_JSON_BYTES",
    "MAX_INBOX_IDENTITY_JSON_BYTES",
    "MAX_INBOX_RESULT_JSON_BYTES",
    "MAX_OUTCOME_JSON_BYTES",
    "ack",
    "begin_inbox",
    "claim",
    "cleanup",
    "enqueue",
    "finish_inbox",
    "get_event",
    "json_digest",
    "nack",
    "renew",
]
