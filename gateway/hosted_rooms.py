"""Durable state for gateway-hosted Bot Mode rooms.

This module owns only hosted-room identity and its append-only event log. It
does not deliver events, lease relay work, or run agent turns; those concerns
belong to the relay and the future hosted-room driver. Keeping that boundary
explicit lets the room log compose with a durable relay without creating a
second transport queue.

The caller supplies the database path so tests and alternate gateway layouts
can isolate state. Production handlers use the gateway's root ``state.db``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping


PROTOCOL_VERSION = 2
MAX_ROOM_ID_CHARS = 128
MAX_EVENT_ID_CHARS = 128
MAX_ROOM_NAME_CHARS = 200
MAX_EVENT_KIND_CHARS = 64
MAX_ACTOR_ID_CHARS = 128
MAX_ACTOR_LABEL_CHARS = 200
MAX_MEMBERS = 128
MAX_MEMBERS_JSON_BYTES = 128 * 1024
MAX_EVENT_JSON_BYTES = 256 * 1024
MAX_LOG_LIMIT = 500

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_EVENT_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_ROOM_SCHEMA_COLUMNS = frozenset({
    "room_id",
    "name",
    "members_json",
    "authority_gateway_id",
    "authority_epoch",
    "next_seq",
    "revision",
    "created_at",
    "updated_at",
    "disbanded_at",
})
_EVENT_SCHEMA_COLUMNS = frozenset({
    "room_id",
    "seq",
    "event_id",
    "kind",
    "actor_json",
    "authority_epoch",
    "payload_json",
    "created_at",
})
_LINK_SCHEMA_COLUMNS = frozenset({
    "room_id",
    "member_id",
    "target_url",
    "target_profile",
    "grant",
    "catalog_json",
    "cancellation_scope_id",
    "trace_id",
    "transport_security",
    "status",
    "updated_at",
})
_REMOTE_RUN_SCHEMA_COLUMNS = frozenset({
    "room_id",
    "member_id",
    "task_id",
    "execution_generation",
    "target_install_id",
    "target_profile",
    "run_id",
    "session_id",
    "created_at",
    "updated_at",
})
_REVOKED_GRANT_SCHEMA_COLUMNS = frozenset({
    "scope_key",
    "expires_at",
    "revoked_before",
})

_EVENT_KINDS_BY_ACTOR = {
    "user": frozenset({"message.user"}),
    "member": frozenset({"message.member"}),
    "gateway": frozenset({
        "member.unavailable",
        "room.activity",
        "room.stop_requested",
        "turn.deferred",
        "turn.reassigned",
        "turn.cancelled",
        "turn.failed",
        "turn.settled",
        "turn.started",
    }),
    "system": frozenset({
        "authority.claimed",
        "authority.lost",
        "room.created",
        "room.disbanded",
        "room.members_changed",
        "room.renamed",
    }),
}
_ACTOR_FIELDS = frozenset({"kind", "id", "display_name", "profile", "connection_id"})


class HostedRoomError(ValueError):
    """Base class for invalid or conflicting hosted-room operations."""


class RoomNotFoundError(HostedRoomError):
    """Raised when a room does not exist or has been disbanded."""


class RoomConflictError(HostedRoomError):
    """Raised when an idempotency key is reused for different room state."""


class EventConflictError(HostedRoomError):
    """Raised when an event id is reused with different immutable content."""


class AuthorityConflictError(HostedRoomError):
    """Raised when a stale room authority attempts to mutate hosted state."""


class AuthoritySupersededError(AuthorityConflictError):
    """Raised when a successful authority claim was later superseded."""


def default_db_path() -> Path:
    """Return the gateway-wide state database for the active install."""
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    root = home.parent.parent if home.parent.name == "profiles" else home
    return root / "state.db"


def local_authority_gateway_id() -> str:
    """Return the stable server-owned identity for hosted-room authority."""
    from hermes_cli.install_identity import get_install_id

    install_id = get_install_id()
    if not install_id:
        raise HostedRoomError("stable gateway install identity is unavailable")
    return _validate_identifier(
        f"install:{install_id}",
        label="authority_gateway_id",
        max_chars=MAX_ACTOR_ID_CHARS,
    )


def _canonical_json(value: Any, *, label: str, max_bytes: int) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise HostedRoomError(f"{label} must be JSON-serializable") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise HostedRoomError(f"{label} is too large")
    return encoded


def _validate_identifier(value: Any, *, label: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise HostedRoomError(f"{label} must be a string")
    value = value.strip()
    if not value or len(value) > max_chars or not _IDENTIFIER_RE.fullmatch(value):
        raise HostedRoomError(f"invalid {label}")
    return value


def _validate_room_name(value: Any) -> str:
    if not isinstance(value, str):
        raise HostedRoomError("name must be a string")
    value = value.strip()
    if not value or len(value) > MAX_ROOM_NAME_CHARS:
        raise HostedRoomError("invalid room name")
    return value


def _validate_members(value: Any) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(value, list):
        raise HostedRoomError("members must be a list")
    if len(value) > MAX_MEMBERS:
        raise HostedRoomError("too many room members")
    members: list[dict[str, Any]] = []
    for member in value:
        if not isinstance(member, dict):
            raise HostedRoomError("each room member must be an object")
        members.append(dict(member))
    encoded = _canonical_json(
        members,
        label="members",
        max_bytes=MAX_MEMBERS_JSON_BYTES,
    )
    return members, encoded


def _validate_event_kind(value: Any) -> str:
    if not isinstance(value, str):
        raise HostedRoomError("kind must be a string")
    value = value.strip()
    if (
        not value
        or len(value) > MAX_EVENT_KIND_CHARS
        or not _EVENT_KIND_RE.fullmatch(value)
    ):
        raise HostedRoomError("invalid event kind")
    return value


def _optional_actor_field(actor: dict[str, Any], field: str, max_chars: int) -> str:
    value = actor.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HostedRoomError(f"actor.{field} must be a string")
    value = value.strip()
    if len(value) > max_chars:
        raise HostedRoomError(f"actor.{field} is too long")
    return value


def _validate_actor(value: Any, *, kind: str) -> tuple[dict[str, str], str]:
    if not isinstance(value, dict):
        raise HostedRoomError("actor must be an object")
    unknown = set(value) - _ACTOR_FIELDS
    if unknown:
        raise HostedRoomError(f"unknown actor fields: {', '.join(sorted(unknown))}")

    actor_kind = value.get("kind")
    if not isinstance(actor_kind, str) or actor_kind not in _EVENT_KINDS_BY_ACTOR:
        raise HostedRoomError("invalid actor.kind")
    if kind not in _EVENT_KINDS_BY_ACTOR[actor_kind]:
        raise HostedRoomError(f"actor kind '{actor_kind}' cannot append '{kind}'")

    actor_id = _validate_identifier(
        value.get("id"),
        label="actor.id",
        max_chars=MAX_ACTOR_ID_CHARS,
    )
    actor = {"kind": actor_kind, "id": actor_id}
    for field, max_chars in (
        ("display_name", MAX_ACTOR_LABEL_CHARS),
        ("profile", MAX_ACTOR_ID_CHARS),
        ("connection_id", MAX_ACTOR_ID_CHARS),
    ):
        field_value = _optional_actor_field(value, field, max_chars)
        if field_value:
            actor[field] = field_value
    encoded = _canonical_json(
        actor,
        label="actor",
        max_bytes=4 * 1024,
    )
    return actor, encoded


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS hosted_rooms (
            room_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            members_json TEXT NOT NULL,
            authority_gateway_id TEXT NOT NULL,
            authority_epoch INTEGER NOT NULL DEFAULT 1 CHECK (authority_epoch >= 1),
            next_seq INTEGER NOT NULL DEFAULT 1 CHECK (next_seq >= 1),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            disbanded_at REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS hosted_room_events (
            room_id TEXT NOT NULL,
            seq INTEGER NOT NULL CHECK (seq >= 1),
            event_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            actor_json TEXT NOT NULL,
            authority_epoch INTEGER CHECK (authority_epoch IS NULL OR authority_epoch >= 1),
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (room_id, seq),
            UNIQUE (room_id, event_id),
            FOREIGN KEY (room_id) REFERENCES hosted_rooms(room_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS hosted_room_links (
            room_id TEXT NOT NULL,
            member_id TEXT NOT NULL,
            target_url TEXT NOT NULL,
            target_profile TEXT NOT NULL,
            grant TEXT NOT NULL,
            catalog_json TEXT NOT NULL,
            cancellation_scope_id TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            transport_security TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ready',
            updated_at REAL NOT NULL,
            PRIMARY KEY (room_id, member_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS hosted_room_remote_runs (
            room_id TEXT NOT NULL,
            member_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            execution_generation INTEGER NOT NULL CHECK (execution_generation >= 1),
            target_install_id TEXT NOT NULL,
            target_profile TEXT NOT NULL,
            run_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (task_id, execution_generation)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS hosted_room_revoked_grants (
            scope_key TEXT PRIMARY KEY,
            expires_at REAL NOT NULL,
            revoked_before REAL NOT NULL
        )"""
    )
    room_columns = {row[1] for row in conn.execute("PRAGMA table_info(hosted_rooms)")}
    if "authority_gateway_id" not in room_columns:
        conn.execute(
            "ALTER TABLE hosted_rooms "
            "ADD COLUMN authority_gateway_id TEXT NOT NULL DEFAULT 'legacy'"
        )
    if "authority_epoch" not in room_columns:
        conn.execute(
            "ALTER TABLE hosted_rooms "
            "ADD COLUMN authority_epoch INTEGER NOT NULL DEFAULT 1"
        )

    event_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(hosted_room_events)")
    }
    if "actor_json" not in event_columns:
        # Draft builds before the actor contract carried no identity. Preserve
        # their inert replay rows explicitly as legacy system events rather
        # than guessing a user or Bot author.
        legacy_actor = _canonical_json(
            {"kind": "system", "id": "legacy"},
            label="actor",
            max_bytes=4 * 1024,
        )
        escaped_actor = legacy_actor.replace("'", "''")
        conn.execute(
            "ALTER TABLE hosted_room_events "
            f"ADD COLUMN actor_json TEXT NOT NULL DEFAULT '{escaped_actor}'"
        )
    if "authority_epoch" not in event_columns:
        conn.execute(
            "ALTER TABLE hosted_room_events ADD COLUMN authority_epoch INTEGER"
        )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_hosted_room_events_cursor
           ON hosted_room_events(room_id, seq)"""
    )
    if not _schema_is_current(conn):
        raise HostedRoomError("hosted room schema migration did not complete")


def _schema_is_current(conn: sqlite3.Connection) -> bool:
    room_columns = frozenset(
        row[1] for row in conn.execute("PRAGMA table_info(hosted_rooms)")
    )
    event_columns = frozenset(
        row[1] for row in conn.execute("PRAGMA table_info(hosted_room_events)")
    )
    link_columns = frozenset(
        row[1] for row in conn.execute("PRAGMA table_info(hosted_room_links)")
    )
    remote_run_columns = frozenset(
        row[1] for row in conn.execute("PRAGMA table_info(hosted_room_remote_runs)")
    )
    revoked_grant_columns = frozenset(
        row[1]
        for row in conn.execute("PRAGMA table_info(hosted_room_revoked_grants)")
    )
    if not _ROOM_SCHEMA_COLUMNS.issubset(room_columns):
        return False
    if not _EVENT_SCHEMA_COLUMNS.issubset(event_columns):
        return False
    if not _LINK_SCHEMA_COLUMNS.issubset(link_columns):
        return False
    if not _REMOTE_RUN_SCHEMA_COLUMNS.issubset(remote_run_columns):
        return False
    if not _REVOKED_GRANT_SCHEMA_COLUMNS.issubset(revoked_grant_columns):
        return False
    index = conn.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type='index' AND name='idx_hosted_room_events_cursor'"""
    ).fetchone()
    return index is not None


def list_room_link_records(db_path: Path | str) -> list[dict[str, Any]]:
    """Return private RoomLink records without logging or formatting grants."""
    with _transaction(db_path) as conn:
        rows = conn.execute(
            """SELECT room_id, member_id, target_url, target_profile, grant,
                      catalog_json, cancellation_scope_id, trace_id,
                      transport_security, status, updated_at
                 FROM hosted_room_links
             ORDER BY room_id, member_id"""
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_room_link_record(
    db_path: Path | str,
    *,
    record: Mapping[str, Any],
    max_links: int,
) -> None:
    """Atomically insert or replace one private RoomLink record."""
    with _transaction(db_path, immediate=True) as conn:
        existing = conn.execute(
            "SELECT 1 FROM hosted_room_links WHERE room_id=? AND member_id=?",
            (record["room_id"], record["member_id"]),
        ).fetchone()
        if existing is None:
            count = int(
                conn.execute("SELECT COUNT(*) FROM hosted_room_links").fetchone()[0]
            )
            if count >= max_links:
                raise HostedRoomError("too many stored room links")
        conn.execute(
            """INSERT INTO hosted_room_links(
                   room_id, member_id, target_url, target_profile, grant,
                   catalog_json, cancellation_scope_id, trace_id,
                   transport_security, status, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(room_id, member_id) DO UPDATE SET
                   target_url=excluded.target_url,
                   target_profile=excluded.target_profile,
                   grant=excluded.grant,
                   catalog_json=excluded.catalog_json,
                   cancellation_scope_id=excluded.cancellation_scope_id,
                   trace_id=excluded.trace_id,
                   transport_security=excluded.transport_security,
                   status=excluded.status,
                   updated_at=excluded.updated_at""",
            (
                record["room_id"],
                record["member_id"],
                record["target_url"],
                record["target_profile"],
                record["grant"],
                record["catalog_json"],
                record["cancellation_scope_id"],
                record["trace_id"],
                record["transport_security"],
                record["status"],
                record["updated_at"],
            ),
        )


def update_room_link_status(
    db_path: Path | str,
    *,
    room_id: str,
    member_id: str,
    status: str,
    now: float | None = None,
) -> bool:
    """Persist a non-secret route health classification."""
    with _transaction(db_path, immediate=True) as conn:
        cursor = conn.execute(
            """UPDATE hosted_room_links SET status=?, updated_at=?
                 WHERE room_id=? AND member_id=?""",
            (
                status,
                float(now if now is not None else time.time()),
                room_id,
                member_id,
            ),
        )
        return cursor.rowcount == 1


def delete_room_link_records(db_path: Path | str, *, room_id: str) -> int:
    """Delete persisted peer routes after their target grants are revoked."""
    with _transaction(db_path, immediate=True) as conn:
        cursor = conn.execute(
            "DELETE FROM hosted_room_links WHERE room_id=?",
            (room_id,),
        )
        return cursor.rowcount


def _room_grant_scope_key(claims: Mapping[str, Any]) -> str:
    """Return a stable non-secret key for one room/home/target/profile scope."""
    import hashlib

    fields = {
        key: str(claims.get(key) or "")
        for key in (
            "room_id",
            "home_install_id",
            "target_install_id",
            "target_profile",
        )
    }
    if not all(fields.values()):
        raise HostedRoomError("room grant scope is incomplete")
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def revoke_room_grant_scope(
    db_path: Path | str,
    *,
    claims: Mapping[str, Any],
    expires_at: float,
    now: float | None = None,
) -> None:
    """Revoke every grant issued at or before now for one exact room scope."""
    scope_key = _room_grant_scope_key(claims)
    timestamp = float(now if now is not None else time.time())
    expiry = float(expires_at)
    if expiry <= timestamp:
        return
    with _transaction(db_path, immediate=True) as conn:
        conn.execute(
            "DELETE FROM hosted_room_revoked_grants WHERE expires_at<=?",
            (timestamp,),
        )
        conn.execute(
            """INSERT INTO hosted_room_revoked_grants(
                   scope_key, expires_at, revoked_before
               ) VALUES (?, ?, ?)
               ON CONFLICT(scope_key) DO UPDATE SET
                   expires_at=MAX(hosted_room_revoked_grants.expires_at,
                                  excluded.expires_at),
                   revoked_before=MAX(hosted_room_revoked_grants.revoked_before,
                                      excluded.revoked_before)""",
            (scope_key, expiry, timestamp),
        )


def room_grant_is_revoked(
    db_path: Path | str,
    *,
    claims: Mapping[str, Any],
    now: float | None = None,
) -> bool:
    """Return whether a grant predates its exact scope's revocation fence."""
    timestamp = float(now if now is not None else time.time())
    scope_key = _room_grant_scope_key(claims)
    issued_at = float(claims.get("issued_at") or 0)
    with _transaction(db_path) as conn:
        row = conn.execute(
            """SELECT revoked_before FROM hosted_room_revoked_grants
                 WHERE scope_key=? AND expires_at>?""",
            (scope_key, timestamp),
        ).fetchone()
    return row is not None and issued_at <= float(row["revoked_before"])


def upsert_remote_run_receipt(
    db_path: Path | str,
    *,
    record: Mapping[str, Any],
    now: float | None = None,
) -> None:
    """Durably bind one logical peer task attempt to its remote run handle."""
    timestamp = float(now if now is not None else time.time())
    with _transaction(db_path, immediate=True) as conn:
        existing = conn.execute(
            """SELECT * FROM hosted_room_remote_runs
                 WHERE task_id=? AND execution_generation=?""",
            (record["task_id"], record["execution_generation"]),
        ).fetchone()
        immutable = (
            record["room_id"],
            record["member_id"],
            record["target_install_id"],
            record["target_profile"],
            record["run_id"],
            record["session_id"],
        )
        if existing is not None:
            stored = (
                existing["room_id"],
                existing["member_id"],
                existing["target_install_id"],
                existing["target_profile"],
                existing["run_id"],
                existing["session_id"],
            )
            if stored != immutable:
                raise HostedRoomError(
                    "remote run receipt conflicts with its logical task"
                )
            conn.execute(
                """UPDATE hosted_room_remote_runs SET updated_at=?
                     WHERE task_id=? AND execution_generation=?""",
                (timestamp, record["task_id"], record["execution_generation"]),
            )
            return
        conn.execute(
            """INSERT INTO hosted_room_remote_runs(
                   room_id, member_id, task_id, execution_generation,
                   target_install_id, target_profile, run_id, session_id,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                *immutable[:2],
                record["task_id"],
                record["execution_generation"],
                *immutable[2:],
                timestamp,
                timestamp,
            ),
        )


def list_remote_run_receipts(
    db_path: Path | str,
    *,
    room_id: str | None = None,
    target_profile: str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return remote run handles in durable task order."""
    conditions: list[str] = []
    values: list[Any] = []
    for column, value in (
        ("room_id", room_id),
        ("target_profile", target_profile),
        ("session_id", session_id),
    ):
        if value is not None:
            conditions.append(f"{column}=?")
            values.append(value)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    with _transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM hosted_room_remote_runs"
            + where
            + " ORDER BY created_at, task_id, execution_generation",
            values,
        ).fetchall()
    return [dict(row) for row in rows]


def remote_run_receipt(
    db_path: Path | str,
    *,
    task_id: str,
    execution_generation: int,
) -> dict[str, Any] | None:
    """Return the exact durable remote run handle for one task attempt."""
    with _transaction(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM hosted_room_remote_runs
                 WHERE task_id=? AND execution_generation=?""",
            (task_id, execution_generation),
        ).fetchone()
    return dict(row) if row is not None else None


def _connect(db_path: Path | str) -> sqlite3.Connection:
    from hermes_state import apply_wal_with_fallback

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        apply_wal_with_fallback(conn, db_label="state.db (hosted_rooms)")
        conn.execute("PRAGMA foreign_keys=ON")
        if _schema_is_current(conn):
            return conn
        # Multiple profile gateways share this root database. Serialize every
        # draft-schema transition in SQLite itself so a crash rolls back the
        # whole DDL/data migration and another process can safely retry it.
        conn.execute("BEGIN IMMEDIATE")
        _initialize_schema(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    return conn


@contextmanager
def _transaction(
    db_path: Path | str, *, immediate: bool = False
) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _room_from_row(row: sqlite3.Row, *, idempotent: bool = False) -> dict[str, Any]:
    room = {
        "room_id": row["room_id"],
        "name": row["name"],
        "members": json.loads(row["members_json"]),
        "authority_gateway_id": row["authority_gateway_id"],
        "authority_epoch": int(row["authority_epoch"]),
        "revision": int(row["revision"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "idempotent": idempotent,
    }
    if "disbanded_at" in row.keys() and row["disbanded_at"] is not None:
        room["disbanded_at"] = float(row["disbanded_at"])
    if "next_seq" in row.keys():
        room["latest_seq"] = int(row["next_seq"]) - 1
    return room


def _event_from_row(row: sqlite3.Row, *, idempotent: bool = False) -> dict[str, Any]:
    return {
        "room_id": row["room_id"],
        "seq": int(row["seq"]),
        "event_id": row["event_id"],
        "kind": row["kind"],
        "actor": json.loads(row["actor_json"]),
        "authority_epoch": (
            int(row["authority_epoch"]) if row["authority_epoch"] is not None else None
        ),
        "payload": json.loads(row["payload_json"]),
        "created_at": float(row["created_at"]),
        "idempotent": idempotent,
    }


def create_room(
    db_path: Path | str,
    *,
    room_id: Any,
    name: Any,
    members: Any,
    authority_gateway_id: Any,
    now: float | None = None,
) -> dict[str, Any]:
    """Create a room, or return the identical existing room idempotently."""
    room_id = _validate_identifier(
        room_id,
        label="room_id",
        max_chars=MAX_ROOM_ID_CHARS,
    )
    name = _validate_room_name(name)
    normalized_members, members_json = _validate_members(members)
    authority_gateway_id = _validate_identifier(
        authority_gateway_id,
        label="authority_gateway_id",
        max_chars=MAX_ACTOR_ID_CHARS,
    )
    now = time.time() if now is None else float(now)

    with _transaction(db_path, immediate=True) as conn:
        existing = conn.execute(
            """SELECT room_id, name, members_json, authority_gateway_id,
                      authority_epoch, next_seq, revision, created_at, updated_at,
                      disbanded_at
               FROM hosted_rooms WHERE room_id=?""",
            (room_id,),
        ).fetchone()
        if existing is not None:
            if existing["disbanded_at"] is not None:
                raise RoomConflictError("room_id belongs to a disbanded room")
            if existing["name"] != name or existing["members_json"] != members_json:
                raise RoomConflictError("room_id already exists with different state")
            if (
                existing["authority_gateway_id"] == "legacy"
                and authority_gateway_id != "legacy"
            ):
                target_epoch = int(existing["authority_epoch"]) + 1
                seq = int(existing["next_seq"])
                claim_actor_json = _canonical_json(
                    {"kind": "system", "id": "authority-control"},
                    label="actor",
                    max_bytes=4 * 1024,
                )
                claim_payload_json = _canonical_json(
                    {
                        "previous_gateway_id": "legacy",
                        "authority_gateway_id": authority_gateway_id,
                        "authority_epoch": target_epoch,
                    },
                    label="payload",
                    max_bytes=MAX_EVENT_JSON_BYTES,
                )
                conn.execute(
                    """INSERT INTO hosted_room_events
                       (room_id, seq, event_id, kind, actor_json,
                        authority_epoch, payload_json, created_at)
                       VALUES (?, ?, 'system:authority-adopted',
                               'authority.claimed', ?, ?, ?, ?)""",
                    (
                        room_id,
                        seq,
                        claim_actor_json,
                        target_epoch,
                        claim_payload_json,
                        now,
                    ),
                )
                adopted = conn.execute(
                    """UPDATE hosted_rooms
                          SET authority_gateway_id=?, authority_epoch=?,
                              next_seq=next_seq+1, revision=revision+1,
                              updated_at=?
                        WHERE room_id=? AND authority_gateway_id='legacy'
                          AND authority_epoch=? AND next_seq=?
                          AND disbanded_at IS NULL""",
                    (
                        authority_gateway_id,
                        target_epoch,
                        now,
                        room_id,
                        int(existing["authority_epoch"]),
                        seq,
                    ),
                )
                if adopted.rowcount != 1:
                    raise AuthorityConflictError("legacy room adoption lost its fence")
                existing = conn.execute(
                    """SELECT room_id, name, members_json, authority_gateway_id,
                              authority_epoch, next_seq, revision, created_at,
                              updated_at, disbanded_at
                         FROM hosted_rooms WHERE room_id=?""",
                    (room_id,),
                ).fetchone()
                if existing is None:  # pragma: no cover - row updated above
                    raise RuntimeError("adopted room could not be reloaded")
                result = _room_from_row(existing, idempotent=True)
                result["adopted"] = True
                claim_event = conn.execute(
                    """SELECT room_id, seq, event_id, kind, actor_json,
                              authority_epoch, payload_json, created_at
                         FROM hosted_room_events
                        WHERE room_id=? AND event_id='system:authority-adopted'""",
                    (room_id,),
                ).fetchone()
                if claim_event is None:  # pragma: no cover - inserted above
                    raise RuntimeError("legacy adoption event could not be reloaded")
                result["claim_event"] = _event_from_row(claim_event)
                return result
            if existing["authority_gateway_id"] != authority_gateway_id:
                raise RoomConflictError(
                    "room_id already belongs to a different authority"
                )
            return _room_from_row(existing, idempotent=True)

        conn.execute(
            """INSERT INTO hosted_rooms
               (room_id, name, members_json, authority_gateway_id,
                authority_epoch, next_seq, revision,
                created_at, updated_at, disbanded_at)
               VALUES (?, ?, ?, ?, 1, 1, 1, ?, ?, NULL)""",
            (room_id, name, members_json, authority_gateway_id, now, now),
        )
        row = conn.execute(
            """SELECT room_id, name, members_json, authority_gateway_id,
                      authority_epoch, revision, created_at, updated_at
               FROM hosted_rooms WHERE room_id=?""",
            (room_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - guarded by the insert above
            raise RuntimeError("created room could not be reloaded")
    result = _room_from_row(row)
    result["members"] = normalized_members
    return result


def list_rooms(
    db_path: Path | str,
    *,
    include_disbanded: bool = False,
) -> list[dict[str, Any]]:
    """Return hosted rooms ordered by most recent change."""
    with _transaction(db_path) as conn:
        rows = conn.execute(
            """SELECT room_id, name, members_json, authority_gateway_id,
                      authority_epoch, next_seq, revision, created_at, updated_at,
                      disbanded_at
               FROM hosted_rooms
               WHERE disbanded_at IS NULL OR ?
               ORDER BY updated_at DESC, room_id ASC""",
            (int(include_disbanded),),
        ).fetchall()
    return [_room_from_row(row) for row in rows]


def append_event(
    db_path: Path | str,
    *,
    room_id: Any,
    event_id: Any,
    kind: Any,
    actor: Any,
    payload: Any,
    authority_gateway_id: Any = None,
    authority_epoch: Any = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Append one immutable event and allocate its per-room sequence atomically.

    Repeating the same ``event_id`` and immutable content returns the original
    event. Reusing the id for different content fails closed.
    """
    room_id = _validate_identifier(
        room_id,
        label="room_id",
        max_chars=MAX_ROOM_ID_CHARS,
    )
    event_id = _validate_identifier(
        event_id,
        label="event_id",
        max_chars=MAX_EVENT_ID_CHARS,
    )
    kind = _validate_event_kind(kind)
    normalized_actor, actor_json = _validate_actor(actor, kind=kind)
    authority_scoped = normalized_actor["kind"] in {"member", "gateway", "system"}
    normalized_authority_gateway_id: str | None = None
    normalized_authority_epoch: int | None = None
    if authority_scoped:
        normalized_authority_gateway_id = _validate_identifier(
            authority_gateway_id,
            label="authority_gateway_id",
            max_chars=MAX_ACTOR_ID_CHARS,
        )
        if (
            normalized_actor["kind"] == "gateway"
            and normalized_actor["id"] != normalized_authority_gateway_id
        ):
            raise HostedRoomError("gateway actor.id must match authority_gateway_id")
        if (
            isinstance(authority_epoch, bool)
            or not isinstance(authority_epoch, int)
            or authority_epoch < 1
        ):
            raise HostedRoomError("authority_epoch must be a positive integer")
        normalized_authority_epoch = authority_epoch
    elif authority_gateway_id is not None or authority_epoch is not None:
        raise HostedRoomError(
            "authority fields are only valid for member, gateway, or system events"
        )
    if not isinstance(payload, dict):
        raise HostedRoomError("payload must be an object")
    payload_json = _canonical_json(
        payload,
        label="payload",
        max_bytes=MAX_EVENT_JSON_BYTES,
    )
    now = time.time() if now is None else float(now)

    with _transaction(db_path, immediate=True) as conn:
        existing = conn.execute(
            """SELECT room_id, seq, event_id, kind, actor_json, authority_epoch,
                      payload_json, created_at
               FROM hosted_room_events WHERE room_id=? AND event_id=?""",
            (room_id, event_id),
        ).fetchone()
        if existing is not None:
            if (
                existing["kind"] != kind
                or existing["actor_json"] != actor_json
                or existing["authority_epoch"] != normalized_authority_epoch
                or existing["payload_json"] != payload_json
            ):
                raise EventConflictError(
                    "event_id already exists with different content"
                )
            return _event_from_row(existing, idempotent=True)

        room = conn.execute(
            """SELECT next_seq, authority_gateway_id, authority_epoch
                  FROM hosted_rooms
               WHERE room_id=? AND disbanded_at IS NULL""",
            (room_id,),
        ).fetchone()
        if room is None:
            raise RoomNotFoundError("hosted room not found")
        if authority_scoped and (
            room["authority_gateway_id"] != normalized_authority_gateway_id
            or int(room["authority_epoch"]) != normalized_authority_epoch
        ):
            raise AuthorityConflictError("stale hosted room authority")
        seq = int(room["next_seq"])
        conn.execute(
            """INSERT INTO hosted_room_events
               (room_id, seq, event_id, kind, actor_json, authority_epoch,
                payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                room_id,
                seq,
                event_id,
                kind,
                actor_json,
                normalized_authority_epoch,
                payload_json,
                now,
            ),
        )
        advanced = conn.execute(
            """UPDATE hosted_rooms
               SET next_seq=?, updated_at=?
               WHERE room_id=? AND next_seq=?""",
            (seq + 1, now, room_id, seq),
        )
        if advanced.rowcount != 1:
            raise RuntimeError("hosted room sequence advance lost its write fence")
        row = conn.execute(
            """SELECT room_id, seq, event_id, kind, actor_json, authority_epoch,
                      payload_json, created_at
               FROM hosted_room_events WHERE room_id=? AND seq=?""",
            (room_id, seq),
        ).fetchone()
        if row is None:  # pragma: no cover - guarded by the insert above
            raise RuntimeError("appended event could not be reloaded")
    result = _event_from_row(row)
    result["actor"] = normalized_actor
    return result


def room_state(
    db_path: Path | str,
    *,
    room_id: Any,
    include_disbanded: bool = False,
) -> dict[str, Any]:
    """Return durable replay and authority state for one room."""
    room_id = _validate_identifier(
        room_id,
        label="room_id",
        max_chars=MAX_ROOM_ID_CHARS,
    )
    with _transaction(db_path) as conn:
        row = conn.execute(
            """SELECT room_id, name, members_json, authority_gateway_id,
                      authority_epoch, next_seq, revision, created_at, updated_at,
                      disbanded_at
                 FROM hosted_rooms
                WHERE room_id=? AND (disbanded_at IS NULL OR ?)""",
            (room_id, int(include_disbanded)),
        ).fetchone()
        if row is None:
            raise RoomNotFoundError("hosted room not found")
        claim_row = conn.execute(
            """SELECT room_id, seq, event_id, kind, actor_json, authority_epoch,
                      payload_json, created_at
                 FROM hosted_room_events
                WHERE room_id=? AND kind='authority.claimed'
                  AND authority_epoch=?
                ORDER BY seq DESC LIMIT 1""",
            (room_id, int(row["authority_epoch"])),
        ).fetchone()
    state = _room_from_row(row)
    state["latest_seq"] = int(row["next_seq"]) - 1
    if claim_row is not None:
        state["authority_claim"] = _event_from_row(claim_row)
    return state


def request_room_stop(
    db_path: Path | str,
    *,
    room_id: Any,
    cancel_id: Any,
) -> dict[str, Any]:
    """Append an idempotent fence that supersedes earlier user turns."""

    cancel_id = _validate_identifier(
        cancel_id,
        label="cancel_id",
        max_chars=MAX_EVENT_ID_CHARS,
    )
    room = room_state(db_path, room_id=room_id)
    digest = hashlib.sha256(cancel_id.encode()).hexdigest()[:32]
    return append_event(
        db_path,
        room_id=room["room_id"],
        event_id=f"room-stop:{digest}",
        kind="room.stop_requested",
        actor={"kind": "gateway", "id": room["authority_gateway_id"]},
        payload={"cancel_id": cancel_id},
        authority_gateway_id=room["authority_gateway_id"],
        authority_epoch=room["authority_epoch"],
    )


def claim_authority(
    db_path: Path | str,
    *,
    room_id: Any,
    expected_gateway_id: Any,
    expected_epoch: Any,
    new_gateway_id: Any,
    event_id: Any,
    now: float | None = None,
) -> dict[str, Any]:
    """Fence a verified authority transfer with a compare-and-swap epoch.

    This storage primitive does not decide *when* takeover is safe. A future
    replicated driver must call it only after its lease/quorum policy has
    established that the previous owner can no longer commit.
    """
    room_id = _validate_identifier(
        room_id,
        label="room_id",
        max_chars=MAX_ROOM_ID_CHARS,
    )
    expected_gateway_id = _validate_identifier(
        expected_gateway_id,
        label="expected_gateway_id",
        max_chars=MAX_ACTOR_ID_CHARS,
    )
    new_gateway_id = _validate_identifier(
        new_gateway_id,
        label="new_gateway_id",
        max_chars=MAX_ACTOR_ID_CHARS,
    )
    event_id = _validate_identifier(
        event_id,
        label="event_id",
        max_chars=MAX_EVENT_ID_CHARS,
    )
    if (
        isinstance(expected_epoch, bool)
        or not isinstance(expected_epoch, int)
        or expected_epoch < 1
    ):
        raise HostedRoomError("expected_epoch must be a positive integer")
    now = time.time() if now is None else float(now)
    target_epoch = expected_epoch + 1
    claim_actor = {"kind": "system", "id": "authority-control"}
    claim_actor_json = _canonical_json(
        claim_actor,
        label="actor",
        max_bytes=4 * 1024,
    )
    claim_payload = {
        "previous_gateway_id": expected_gateway_id,
        "authority_gateway_id": new_gateway_id,
        "authority_epoch": target_epoch,
    }
    claim_payload_json = _canonical_json(
        claim_payload,
        label="payload",
        max_bytes=MAX_EVENT_JSON_BYTES,
    )

    with _transaction(db_path, immediate=True) as conn:
        row = conn.execute(
            """SELECT authority_gateway_id, authority_epoch, next_seq
                 FROM hosted_rooms
                WHERE room_id=? AND disbanded_at IS NULL""",
            (room_id,),
        ).fetchone()
        if row is None:
            raise RoomNotFoundError("hosted room not found")
        current_gateway = str(row["authority_gateway_id"])
        current_epoch = int(row["authority_epoch"])
        existing_event = conn.execute(
            """SELECT room_id, seq, event_id, kind, actor_json, authority_epoch,
                      payload_json, created_at
                 FROM hosted_room_events WHERE room_id=? AND event_id=?""",
            (room_id, event_id),
        ).fetchone()
        if existing_event is not None:
            if (
                existing_event["kind"] != "authority.claimed"
                or existing_event["actor_json"] != claim_actor_json
                or existing_event["authority_epoch"] != target_epoch
                or existing_event["payload_json"] != claim_payload_json
            ):
                raise EventConflictError(
                    "event_id already exists with different content"
                )
            if current_gateway != new_gateway_id or current_epoch != target_epoch:
                raise AuthoritySupersededError(
                    "authority claim succeeded but was later superseded"
                )
            idempotent = True
        elif current_gateway != expected_gateway_id or current_epoch != expected_epoch:
            raise AuthorityConflictError("hosted room authority changed")
        else:
            seq = int(row["next_seq"])
            conn.execute(
                """INSERT INTO hosted_room_events
                   (room_id, seq, event_id, kind, actor_json, authority_epoch,
                    payload_json, created_at)
                   VALUES (?, ?, ?, 'authority.claimed', ?, ?, ?, ?)""",
                (
                    room_id,
                    seq,
                    event_id,
                    claim_actor_json,
                    target_epoch,
                    claim_payload_json,
                    now,
                ),
            )
            updated = conn.execute(
                """UPDATE hosted_rooms
                      SET authority_gateway_id=?, authority_epoch=authority_epoch+1,
                          next_seq=next_seq+1, revision=revision+1, updated_at=?
                    WHERE room_id=? AND disbanded_at IS NULL
                      AND authority_gateway_id=? AND authority_epoch=?""",
                (
                    new_gateway_id,
                    now,
                    room_id,
                    expected_gateway_id,
                    expected_epoch,
                ),
            )
            if updated.rowcount != 1:
                raise AuthorityConflictError("hosted room authority changed")
            idempotent = False
            existing_event = conn.execute(
                """SELECT room_id, seq, event_id, kind, actor_json,
                          authority_epoch, payload_json, created_at
                     FROM hosted_room_events WHERE room_id=? AND event_id=?""",
                (room_id, event_id),
            ).fetchone()
        state_row = conn.execute(
            """SELECT room_id, name, members_json, authority_gateway_id,
                      authority_epoch, next_seq, revision, created_at, updated_at
                 FROM hosted_rooms WHERE room_id=?""",
            (room_id,),
        ).fetchone()
        if state_row is None:  # pragma: no cover - room exists in this transaction
            raise RuntimeError("claimed room could not be reloaded")
    state = _room_from_row(state_row, idempotent=idempotent)
    state["latest_seq"] = int(state_row["next_seq"]) - 1
    if existing_event is None:  # pragma: no cover - both claim paths set it
        raise RuntimeError("authority claim event could not be reloaded")
    state["claim_event"] = _event_from_row(
        existing_event,
        idempotent=idempotent,
    )
    return state


def disband_room(
    db_path: Path | str,
    *,
    room_id: Any,
    now: float | None = None,
) -> dict[str, Any]:
    """Tombstone a room id permanently and idempotently."""
    room_id = _validate_identifier(
        room_id,
        label="room_id",
        max_chars=MAX_ROOM_ID_CHARS,
    )
    now = time.time() if now is None else float(now)

    with _transaction(db_path, immediate=True) as conn:
        room = conn.execute(
            """SELECT authority_epoch, next_seq, disbanded_at
                 FROM hosted_rooms WHERE room_id=?""",
            (room_id,),
        ).fetchone()
        if room is None:
            raise RoomNotFoundError("hosted room not found")
        if room["disbanded_at"] is not None:
            event = conn.execute(
                """SELECT room_id, seq, event_id, kind, actor_json,
                          authority_epoch, payload_json, created_at
                     FROM hosted_room_events
                    WHERE room_id=? AND event_id='system:room-disbanded'""",
                (room_id,),
            ).fetchone()
            return {
                "room_id": room_id,
                "disbanded_at": float(room["disbanded_at"]),
                "idempotent": True,
                **(
                    {"event": _event_from_row(event, idempotent=True)}
                    if event is not None
                    else {}
                ),
            }
        seq = int(room["next_seq"])
        actor_json = _canonical_json(
            {"kind": "system", "id": "room-control"},
            label="actor",
            max_bytes=4 * 1024,
        )
        payload_json = _canonical_json(
            {"room_id": room_id},
            label="payload",
            max_bytes=MAX_EVENT_JSON_BYTES,
        )
        conn.execute(
            """INSERT INTO hosted_room_events
               (room_id, seq, event_id, kind, actor_json, authority_epoch,
                payload_json, created_at)
               VALUES (?, ?, 'system:room-disbanded', 'room.disbanded', ?, ?, ?, ?)""",
            (
                room_id,
                seq,
                actor_json,
                int(room["authority_epoch"]),
                payload_json,
                now,
            ),
        )
        updated = conn.execute(
            """UPDATE hosted_rooms
               SET disbanded_at=?, updated_at=?, revision=revision+1,
                   next_seq=next_seq+1
               WHERE room_id=? AND disbanded_at IS NULL""",
            (now, now, room_id),
        )
        if updated.rowcount != 1:
            raise RoomConflictError("hosted room disband lost its fence")
        event = conn.execute(
            """SELECT room_id, seq, event_id, kind, actor_json,
                      authority_epoch, payload_json, created_at
                 FROM hosted_room_events
                WHERE room_id=? AND event_id='system:room-disbanded'""",
            (room_id,),
        ).fetchone()
        if event is None:  # pragma: no cover - inserted in this transaction
            raise RuntimeError("room disband event could not be reloaded")
    return {
        "room_id": room_id,
        "disbanded_at": now,
        "idempotent": False,
        "event": _event_from_row(event),
    }


def read_events(
    db_path: Path | str,
    *,
    room_id: Any,
    since_seq: Any = 0,
    limit: Any = 100,
    include_disbanded: bool = False,
) -> dict[str, Any]:
    """Read a monotonic room-log delta after ``since_seq``."""
    room_id = _validate_identifier(
        room_id,
        label="room_id",
        max_chars=MAX_ROOM_ID_CHARS,
    )
    if isinstance(since_seq, bool) or not isinstance(since_seq, int) or since_seq < 0:
        raise HostedRoomError("since_seq must be a non-negative integer")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_LOG_LIMIT
    ):
        raise HostedRoomError(f"limit must be between 1 and {MAX_LOG_LIMIT}")

    with _transaction(db_path) as conn:
        room = conn.execute(
            """SELECT next_seq FROM hosted_rooms
               WHERE room_id=? AND (disbanded_at IS NULL OR ?)""",
            (room_id, int(include_disbanded)),
        ).fetchone()
        if room is None:
            raise RoomNotFoundError("hosted room not found")
        latest_seq = int(room["next_seq"]) - 1
        if since_seq > latest_seq:
            raise HostedRoomError("since_seq is ahead of the hosted room log")
        rows = conn.execute(
            """SELECT room_id, seq, event_id, kind, actor_json, authority_epoch,
                      payload_json, created_at
               FROM hosted_room_events
               WHERE room_id=? AND seq>?
               ORDER BY seq ASC LIMIT ?""",
            (room_id, since_seq, limit),
        ).fetchall()
    events = [_event_from_row(row) for row in rows]
    cursor = events[-1]["seq"] if events else since_seq
    return {
        "events": events,
        "cursor": cursor,
        "latest_seq": latest_seq,
        "has_more": cursor < latest_seq,
    }
