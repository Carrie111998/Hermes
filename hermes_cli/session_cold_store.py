"""Local cold archive Store, read-only Verify, and explicit Purge primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import errno
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
from typing import Any

from hermes_state import SessionDB, resolved_max_export_messages


_ARCHIVE_FORMAT = "hermes-cold-archive-store-spike/v1"
_MAX_SQLITE_IN_PARAMS = 500
_STATE_META_SESSION_NAMESPACES = ("goal", "loop", "heartbeat")
_ASYNC_DELEGATION_SESSION_COLUMNS = (
    "origin_session",
    "parent_session_id",
    "origin_session_id",
)
_COORDINATION_SESSION_REFERENCES = (
    ("compression_locks", "session_id"),
    ("session_turn_leases", "conversation_id"),
)


@dataclass(frozen=True)
class StoredLineage:
    """Identity and current local snapshot emitted by :func:`store_archived_lineage`."""

    terminal_id: str
    physical_ids: tuple[str, ...]
    source_fingerprint: str
    snapshot_dir: Path


@dataclass(frozen=True)
class VerifiedLineage:
    """Identity of a current snapshot verified against its live Store plan."""

    terminal_id: str
    physical_ids: tuple[str, ...]
    source_fingerprint: str
    snapshot_dir: Path


@dataclass(frozen=True)
class PurgedLineage:
    """Identity deleted from SQLite while its verified snapshot remains local."""

    terminal_id: str
    physical_ids: tuple[str, ...]
    source_fingerprint: str
    snapshot_dir: Path


@dataclass(frozen=True)
class _StorePlan:
    terminal_id: str
    physical_ids: tuple[str, ...]
    started_at: float
    records: list[dict[str, Any]]
    source_fingerprint: str


def _connection(db: SessionDB) -> sqlite3.Connection:
    if db._conn is None:
        raise RuntimeError("SessionDB connection is closed")
    return db._conn


def _safe_component(value: str) -> str:
    raw = str(value or "")
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", raw).strip("._")[:96] or "session"
    digest = sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return f"{safe}_{digest}"


def _rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor = conn.execute(query, params)
    return [
        {column[0]: value for column, value in zip(cursor.description, row, strict=True)}
        for row in cursor.fetchall()
    ]


def _enforce_message_limit(conn: sqlite3.Connection, physical_ids: tuple[str, ...]) -> None:
    limit = resolved_max_export_messages()
    if limit <= 0:
        return
    seen = 0
    for start in range(0, len(physical_ids), _MAX_SQLITE_IN_PARAMS):
        ids = physical_ids[start : start + _MAX_SQLITE_IN_PARAMS]
        placeholders = ",".join("?" for _ in ids)
        remaining = limit + 1 - seen
        rows = conn.execute(
            f"SELECT 1 FROM messages WHERE session_id IN ({placeholders}) LIMIT ?",
            (*ids, remaining),
        ).fetchall()
        seen += len(rows)
        if seen > limit:
            raise ValueError(
                f"cold store lineage has more than the configured export limit {limit} messages"
            )


def _session(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT * FROM sessions WHERE id = ?", (session_id,))
    return rows[0] if rows else None


def _is_explicit_fork(row: dict[str, Any]) -> bool:
    if row.get("source") == "tool":
        return True
    raw_config = row.get("model_config")
    if not raw_config:
        return False
    try:
        config = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(config, dict):
        return False
    parent_id = row.get("parent_session_id")
    branched = config.get("_branched_from")
    delegated = config.get("_delegate_from")
    if parent_id:
        return branched == parent_id or delegated == parent_id
    return branched is not None or delegated is not None


def _raw_compression_lineage(conn: sqlite3.Connection, terminal_id: str) -> tuple[str, ...]:
    current = _session(conn, terminal_id)
    if current is None:
        raise ValueError(f"session not found: {terminal_id}")
    if _is_explicit_fork(current):
        return (terminal_id,)
    ancestors = [str(current["id"])]
    seen = set(ancestors)
    while current.get("parent_session_id"):
        parent = _session(conn, str(current["parent_session_id"]))
        if parent is None or parent.get("end_reason") != "compression" or _is_explicit_fork(current):
            break
        parent_id = str(parent["id"])
        if parent_id in seen:
            raise ValueError("cyclic compression lineage")
        ancestors.append(parent_id)
        seen.add(parent_id)
        current = parent

    lineage = list(reversed(ancestors))
    current = _session(conn, lineage[-1])
    assert current is not None
    while current.get("end_reason") == "compression":
        children = _rows(
            conn,
            "SELECT * FROM sessions WHERE parent_session_id = ? ORDER BY started_at ASC",
            (str(current["id"]),),
        )
        candidates = [child for child in children if not _is_explicit_fork(child)]
        if not candidates:
            break
        if len(candidates) > 1:
            raise ValueError("ambiguous compression continuation")
        current = candidates[0]
        current_id = str(current["id"])
        if current_id in seen:
            raise ValueError("cyclic compression lineage")
        lineage.append(current_id)
        seen.add(current_id)
    return tuple(lineage)


def _records(conn: sqlite3.Connection, physical_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for session_id in physical_ids:
        sessions = _rows(conn, "SELECT * FROM sessions WHERE id = ?", (session_id,))
        if len(sessions) != 1:
            raise ValueError(f"session disappeared while storing: {session_id}")
        records.append({"v": 1, "kind": "session-segment", "row": sessions[0]})
        system_prompt_hash = sessions[0].get("system_prompt_hash")
        if system_prompt_hash:
            prompts = _rows(
                conn,
                "SELECT hash, prompt FROM system_prompts WHERE hash = ?",
                (str(system_prompt_hash),),
            )
            if len(prompts) != 1:
                raise ValueError(f"missing normalized system prompt: {system_prompt_hash}")
            records.append({"v": 1, "kind": "system-prompt", "row": prompts[0]})
        records.extend(
            {"v": 1, "kind": "message", "row": row}
            for row in _rows(
                conn,
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            )
        )
        records.extend(
            {"v": 1, "kind": "model-usage", "row": row}
            for row in _rows(
                conn,
                "SELECT * FROM session_model_usage WHERE session_id = ? "
                "ORDER BY model, billing_provider, billing_base_url, billing_mode, task",
                (session_id,),
            )
        )
    return records


def _fingerprint(records: list[dict[str, Any]]) -> str:
    canonical = "\n".join(
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for record in records
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _validate_sqlite_values(records: list[dict[str, Any]]) -> None:
    """Reject SQLite values that archive format v1 cannot represent."""
    for record in records:
        kind = str(record["kind"])
        for column, value in record["row"].items():
            location = f"{kind}.{column}"
            if isinstance(value, bytes):
                raise ValueError(
                    "cold store v1 does not support SQLite BLOB/bytes values "
                    f"at {location}"
                )
            if value is None or isinstance(value, (str, int)):
                continue
            if isinstance(value, float):
                if isfinite(value):
                    continue
                raise ValueError(
                    "cold store v1 requires finite SQLite numeric values "
                    f"at {location}"
                )
            raise ValueError(
                f"cold store v1 does not support SQLite value type "
                f"{type(value).__name__} at {location}"
            )


def _directory_open_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError as exc:
        raise OSError(errno.ENOTSUP, "cold store requires no-follow directory descriptors") from exc


def _unsafe_archive_parent(path: Path, exc: OSError | None = None) -> ValueError:
    error = ValueError(f"unsafe archive parent path: {path}")
    if exc is not None:
        error.__cause__ = exc
    return error


def _open_or_create_directory(parent_fd: int, name: str, path: Path) -> int:
    flags = _directory_open_flags()
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        try:
            os.mkdir(name, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise _unsafe_archive_parent(path, exc)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise _unsafe_archive_parent(path, exc)
        os.fsync(descriptor)
        os.fsync(parent_fd)
        return descriptor
    except OSError as exc:
        raise _unsafe_archive_parent(path, exc)


def _open_snapshot_parent(
    archive_root: Path, relative_parts: tuple[str, ...]
) -> tuple[list[int], list[tuple[int, str, int, Path]]]:
    """Open/create a no-follow chain from ``/`` through the snapshot parent."""
    absolute_root = Path(os.path.abspath(os.fspath(archive_root)))
    names = (*absolute_root.parts[1:], *relative_parts)
    descriptors = [os.open(os.sep, _directory_open_flags())]
    edges: list[tuple[int, str, int, Path]] = []
    current_path = Path(os.sep)
    try:
        for name in names:
            current_path /= name
            descriptor = _open_or_create_directory(descriptors[-1], name, current_path)
            edges.append((descriptors[-1], name, descriptor, current_path))
            descriptors.append(descriptor)
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise
    return descriptors, edges


def _directory_entry_matches(parent_fd: int, name: str, descriptor: int) -> bool:
    try:
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    opened = os.fstat(descriptor)
    return (
        stat.S_ISDIR(entry.st_mode)
        and entry.st_dev == opened.st_dev
        and entry.st_ino == opened.st_ino
    )


def _validate_directory_chain(edges: list[tuple[int, str, int, Path]]) -> None:
    for parent_fd, name, descriptor, path in edges:
        if not _directory_entry_matches(parent_fd, name, descriptor):
            raise _unsafe_archive_parent(path)


def _write_text_fsync_at(directory_fd: int, name: str, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())


def _read_regular_text_at(directory_fd: int, name: str) -> str | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as source:
            try:
                return source.read()
            except UnicodeDecodeError:
                return None
    finally:
        os.close(descriptor)


def _valid_existing_snapshot_at(
    snapshot_parent_fd: int,
    snapshot_name: str,
    terminal_id: str,
    lineage: tuple[str, ...],
    fingerprint: str,
    record_count: int,
) -> bool | None:
    try:
        snapshot_fd = os.open(
            snapshot_name,
            _directory_open_flags(),
            dir_fd=snapshot_parent_fd,
        )
    except FileNotFoundError:
        return None
    except OSError:
        return False
    try:
        try:
            metadata_text = _read_regular_text_at(snapshot_fd, "metadata.json")
            payload_text = _read_regular_text_at(snapshot_fd, "session.jsonl")
            if metadata_text is None or payload_text is None:
                return False
            metadata = json.loads(metadata_text)
            if not isinstance(metadata, dict):
                return False
            records = [json.loads(line) for line in payload_text.splitlines()]
            artifacts_fd = os.open("artifacts", _directory_open_flags(), dir_fd=snapshot_fd)
            os.close(artifacts_fd)
        except (OSError, json.JSONDecodeError):
            return False
        return (
            metadata.get("format") == _ARCHIVE_FORMAT
            and metadata.get("terminal_id") == terminal_id
            and metadata.get("physical_ids") == list(lineage)
            and metadata.get("source_fingerprint") == fingerprint
            and type(metadata.get("record_count")) is int
            and metadata.get("record_count") == record_count == len(records)
            and _fingerprint(records) == fingerprint
            and _directory_entry_matches(snapshot_parent_fd, snapshot_name, snapshot_fd)
        )
    finally:
        os.close(snapshot_fd)


def _create_staging_directory(snapshot_parent_fd: int) -> tuple[str, int]:
    for _ in range(100):
        name = f".staging-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, 0o700, dir_fd=snapshot_parent_fd)
        except FileExistsError:
            continue
        try:
            descriptor = os.open(name, _directory_open_flags(), dir_fd=snapshot_parent_fd)
        except BaseException:
            try:
                os.rmdir(name, dir_fd=snapshot_parent_fd)
            except OSError:
                pass
            raise
        if not _directory_entry_matches(snapshot_parent_fd, name, descriptor):
            os.close(descriptor)
            raise ValueError("unsafe cold-store staging directory")
        return name, descriptor
    raise FileExistsError("could not allocate cold-store staging directory")


def _remove_staging_at(snapshot_parent_fd: int, name: str, staging_fd: int) -> None:
    for member in ("metadata.json", "session.jsonl"):
        try:
            os.unlink(member, dir_fd=staging_fd)
        except OSError:
            pass
    try:
        os.rmdir("artifacts", dir_fd=staging_fd)
    except OSError:
        pass
    if _directory_entry_matches(snapshot_parent_fd, name, staging_fd):
        try:
            os.rmdir(name, dir_fd=snapshot_parent_fd)
        except OSError:
            pass


def _remove_stale_snapshot_at(snapshot_parent_fd: int, name: str) -> None:
    """Best-effort cleanup for a displaced snapshot; it is never archive history."""
    try:
        shutil.rmtree(name, dir_fd=snapshot_parent_fd)
    except OSError:
        try:
            os.unlink(name, dir_fd=snapshot_parent_fd)
        except OSError:
            pass


def _move_current_snapshot_aside(snapshot_parent_fd: int, snapshot_name: str) -> str | None:
    for _ in range(100):
        stale_name = f".stale-{secrets.token_hex(8)}"
        try:
            os.rename(
                snapshot_name,
                stale_name,
                src_dir_fd=snapshot_parent_fd,
                dst_dir_fd=snapshot_parent_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                continue
            raise
        return stale_name
    raise FileExistsError("could not allocate displaced cold-store snapshot name")


def _require_supported_platform() -> None:
    if os.name == "nt":
        raise OSError("cold store is not yet supported on Windows")


def _build_store_plan(conn: sqlite3.Connection, terminal_id: str) -> _StorePlan:
    lineage = _raw_compression_lineage(conn, terminal_id)
    if lineage[-1] != terminal_id:
        raise ValueError("store requires the terminal compression session ID")

    rows = [_session(conn, session_id) for session_id in lineage]
    if any(session is None for session in rows):
        raise ValueError("compression lineage changed while resolving store candidate")
    rows = [session for session in rows if session is not None]
    if any(not row.get("archived") for row in rows):
        raise ValueError("all compression lineage rows must be marked archived")
    if any(row.get("pinned") for row in rows):
        raise ValueError("pinned sessions are not cold-store candidates")
    started_at = rows[-1].get("started_at")
    ended_at = rows[-1].get("ended_at")
    if ended_at is None or rows[-1].get("end_reason") == "compression":
        raise ValueError(
            "terminal session must be ended and non-compression before cold storage"
        )
    if started_at is None:
        raise ValueError("terminal session must have a start time before cold storage")

    _enforce_message_limit(conn, lineage)
    records = _records(conn, lineage)
    _validate_sqlite_values(records)
    try:
        validated_started_at = float(started_at)
        datetime.fromtimestamp(validated_started_at, UTC)
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise ValueError(
            "terminal session start time is outside the supported range"
        ) from exc
    return _StorePlan(
        terminal_id=terminal_id,
        physical_ids=lineage,
        started_at=validated_started_at,
        records=records,
        source_fingerprint=_fingerprint(records),
    )


def _read_store_plan(db: SessionDB, terminal_id: str) -> _StorePlan:
    """Build one transactionally consistent Store plan using SELECTs only."""
    conn = _connection(db)
    with db._lock:
        conn.execute("SAVEPOINT cold_store_snapshot")
        try:
            return _build_store_plan(conn, terminal_id)
        finally:
            conn.execute("RELEASE SAVEPOINT cold_store_snapshot")


def plan_archived_lineage(db: SessionDB, terminal_id: str) -> None:
    """Run Store eligibility and source planning without writing any state."""
    _require_supported_platform()
    _read_store_plan(db, terminal_id)


def _snapshot_location(
    archive_root: Path, plan: _StorePlan
) -> tuple[Path, str, tuple[str, ...]]:
    terminal_date = datetime.fromtimestamp(plan.started_at, UTC)
    snapshot_name = _safe_component(plan.terminal_id)
    parent_parts = (
        "sessions",
        "started",
        f"{terminal_date:%Y}",
        f"{terminal_date:%m}",
        f"{terminal_date:%d}",
    )
    return (
        archive_root.joinpath(*parent_parts, snapshot_name),
        snapshot_name,
        parent_parts,
    )


def _open_existing_snapshot_parent(
    archive_root: Path,
    relative_parts: tuple[str, ...],
    snapshot_dir: Path,
) -> tuple[list[int], list[tuple[int, str, int, Path]]]:
    """Open a no-follow parent chain without creating any filesystem entry."""
    names = (*archive_root.parts[1:], *relative_parts)
    descriptors = [os.open(os.sep, _directory_open_flags())]
    edges: list[tuple[int, str, int, Path]] = []
    current_path = Path(os.sep)
    try:
        for name in names:
            current_path /= name
            try:
                descriptor = os.open(
                    name, _directory_open_flags(), dir_fd=descriptors[-1]
                )
            except FileNotFoundError as exc:
                raise ValueError(
                    f"cold-store snapshot not found: {snapshot_dir}"
                ) from exc
            except OSError as exc:
                raise _unsafe_archive_parent(current_path, exc)
            edges.append((descriptors[-1], name, descriptor, current_path))
            descriptors.append(descriptor)
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise
    return descriptors, edges


def _verify_plan_snapshot(archive_root: Path, plan: _StorePlan) -> Path:
    """Verify the existing snapshot for an already-consistent Store plan."""
    snapshot_dir, snapshot_name, parent_parts = _snapshot_location(
        archive_root, plan
    )
    descriptors, edges = _open_existing_snapshot_parent(
        archive_root, parent_parts, snapshot_dir
    )
    try:
        _validate_directory_chain(edges)
        valid = _valid_existing_snapshot_at(
            descriptors[-1],
            snapshot_name,
            plan.terminal_id,
            plan.physical_ids,
            plan.source_fingerprint,
            len(plan.records),
        )
        if valid is None:
            raise ValueError(f"cold-store snapshot not found: {snapshot_dir}")
        if not valid:
            raise ValueError(
                "cold-store snapshot is corrupt or does not match the current "
                "Store plan (metadata/JSONL/record count/fingerprint/physical IDs): "
                f"{snapshot_dir}"
            )
        _validate_directory_chain(edges)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return snapshot_dir


def verify_archived_lineage(
    db: SessionDB, terminal_id: str, archive_root: Path
) -> VerifiedLineage:
    """Verify one existing current snapshot against the live Store plan.

    This primitive is strictly read-only: it performs no database writes and
    opens only existing archive directories and payloads with no-follow flags.
    """
    _require_supported_platform()
    archive_root = Path(os.path.abspath(os.fspath(archive_root)))
    plan = _read_store_plan(db, terminal_id)
    snapshot_dir = _verify_plan_snapshot(archive_root, plan)
    return VerifiedLineage(
        plan.terminal_id,
        plan.physical_ids,
        plan.source_fingerprint,
        snapshot_dir,
    )


def _quoted_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _id_chunks(ids: tuple[str, ...]) -> list[tuple[str, ...]]:
    return [
        ids[start : start + _MAX_SQLITE_IN_PARAMS]
        for start in range(0, len(ids), _MAX_SQLITE_IN_PARAMS)
    ]


def _reject_gateway_routing_references(
    conn: sqlite3.Connection, physical_ids: tuple[str, ...]
) -> None:
    """Fail closed on gateway routes that may name a covered session."""
    covered = set(physical_ids)
    rows = conn.execute(
        "SELECT scope, session_key, entry_json FROM gateway_routing"
    ).fetchall()
    for row in rows:
        scope = str(row[0])
        session_key = str(row[1])
        try:
            entry = json.loads(row[2])
        except Exception as exc:
            raise ValueError(
                "cold purge refuses gateway_routing row whose session reference "
                "cannot be verified: "
                f"scope={scope!r}, session_key={session_key!r}"
            ) from exc
        if not isinstance(entry, dict):
            raise ValueError(
                "cold purge refuses gateway_routing row whose session reference "
                "cannot be verified: "
                f"scope={scope!r}, session_key={session_key!r}"
            )
        session_id = entry.get("session_id")
        if isinstance(session_id, str) and session_id in covered:
            raise ValueError(
                "cold purge refuses gateway_routing soft reference to lineage "
                f"session {session_id}: scope={scope!r}, "
                f"session_key={session_key!r}"
            )


def _required_table_columns(
    conn: sqlite3.Connection, table: str, required: tuple[str, ...]
) -> None:
    """Reject stale or damaged soft-reference schemas before purge."""
    quoted_table = _quoted_identifier(table)
    rows = conn.execute(f"PRAGMA table_info({quoted_table})").fetchall()
    columns = {str(row[1]) for row in rows}
    missing = [column for column in required if column not in columns]
    if missing:
        missing_names = ", ".join(f"{table}.{column}" for column in missing)
        raise ValueError(
            "cold purge cannot verify soft references because the session "
            f"database schema is missing {missing_names}"
        )


def _reject_state_meta_references(
    conn: sqlite3.Connection, physical_ids: tuple[str, ...]
) -> None:
    """Reject goal/loop/heartbeat keys owned by any covered session."""
    _required_table_columns(conn, "state_meta", ("key",))
    for namespace in _STATE_META_SESSION_NAMESPACES:
        for ids in _id_chunks(physical_ids):
            keys = tuple(f"{namespace}:{session_id}" for session_id in ids)
            placeholders = ",".join("?" for _ in keys)
            row = conn.execute(
                f"SELECT key FROM state_meta WHERE key IN ({placeholders}) LIMIT 1",
                keys,
            ).fetchone()
            if row is None:
                continue
            key = str(row[0])
            session_id = key.split(":", 1)[1]
            raise ValueError(
                "cold purge refuses state_meta soft reference to lineage "
                f"session {session_id}: namespace={namespace!r}, key={key!r}"
            )


def _reject_async_delegation_references(
    conn: sqlite3.Connection, physical_ids: tuple[str, ...]
) -> None:
    """Reject durable delegation rows naming any covered session."""
    required_columns = ("delegation_id", *_ASYNC_DELEGATION_SESSION_COLUMNS)
    _required_table_columns(conn, "async_delegations", required_columns)
    for column in _ASYNC_DELEGATION_SESSION_COLUMNS:
        quoted_column = _quoted_identifier(column)
        for ids in _id_chunks(physical_ids):
            placeholders = ",".join("?" for _ in ids)
            row = conn.execute(
                "SELECT delegation_id, "
                f"{quoted_column} FROM async_delegations "
                f"WHERE {quoted_column} IN ({placeholders}) LIMIT 1",
                ids,
            ).fetchone()
            if row is None:
                continue
            raise ValueError(
                "cold purge refuses async_delegations soft reference to "
                f"lineage session {row[1]}: column={column!r}, "
                f"delegation_id={str(row[0])!r}"
            )


def _reject_coordination_references(
    conn: sqlite3.Connection, physical_ids: tuple[str, ...]
) -> None:
    """Reject locks or turn leases naming any covered session."""
    for table, column in _COORDINATION_SESSION_REFERENCES:
        _required_table_columns(conn, table, (column,))
        quoted_table = _quoted_identifier(table)
        quoted_column = _quoted_identifier(column)
        for ids in _id_chunks(physical_ids):
            placeholders = ",".join("?" for _ in ids)
            row = conn.execute(
                f"SELECT {quoted_column} FROM {quoted_table} "
                f"WHERE {quoted_column} IN ({placeholders}) LIMIT 1",
                ids,
            ).fetchone()
            if row is None:
                continue
            raise ValueError(
                f"cold purge refuses {table}.{column} soft reference to "
                f"lineage session {row[0]}"
            )


def _reject_uncovered_session_references(
    conn: sqlite3.Connection, physical_ids: tuple[str, ...]
) -> None:
    """Fail if deleting the covered rows would mutate any unsnapshotted row."""
    _reject_gateway_routing_references(conn, physical_ids)
    _reject_state_meta_references(conn, physical_ids)
    _reject_async_delegation_references(conn, physical_ids)
    _reject_coordination_references(conn, physical_ids)
    covered = set(physical_ids)
    chunks = _id_chunks(physical_ids)
    for ids in chunks:
        placeholders = ",".join("?" for _ in ids)
        children = conn.execute(
            f"SELECT id FROM sessions WHERE parent_session_id IN ({placeholders})",
            ids,
        ).fetchall()
        uncovered = [str(row[0]) for row in children if str(row[0]) not in covered]
        if uncovered:
            raise ValueError(
                "cold purge refuses an uncovered child session referencing the "
                f"lineage: {uncovered[0]}"
            )

    tables = conn.execute(
        "SELECT name FROM sqlite_schema "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    covered_fk_tables = {"messages", "session_model_usage", "sessions"}
    for table_row in tables:
        table = str(table_row[0])
        if table in covered_fk_tables:
            continue
        quoted_table = _quoted_identifier(table)
        foreign_keys = conn.execute(
            f"PRAGMA foreign_key_list({quoted_table})"
        ).fetchall()
        for foreign_key in foreign_keys:
            if str(foreign_key[2]) != "sessions":
                continue
            source_column = str(foreign_key[3])
            quoted_column = _quoted_identifier(source_column)
            for ids in chunks:
                placeholders = ",".join("?" for _ in ids)
                referenced = conn.execute(
                    f"SELECT 1 FROM {quoted_table} "
                    f"WHERE {quoted_column} IN ({placeholders}) LIMIT 1",
                    ids,
                ).fetchone()
                if referenced is not None:
                    raise ValueError(
                        "cold purge refuses an uncovered foreign-key row in "
                        f"{table}.{source_column}"
                    )


def _build_purge_reference_plan(
    conn: sqlite3.Connection, terminal_id: str
) -> _StorePlan:
    """Build the archived source plan and reject every deletion reference."""
    plan = _build_store_plan(conn, terminal_id)
    _reject_uncovered_session_references(conn, plan.physical_ids)
    return plan


def preflight_purge_archived_lineage(db: SessionDB, terminal_id: str) -> None:
    """Check Store planning and Purge references without a snapshot or writes."""
    _require_supported_platform()
    conn = _connection(db)
    with db._lock:
        conn.execute("SAVEPOINT cold_purge_reference_preflight")
        try:
            _build_purge_reference_plan(conn, terminal_id)
        finally:
            conn.execute("RELEASE SAVEPOINT cold_purge_reference_preflight")


def _validated_purge_plan(
    conn: sqlite3.Connection, terminal_id: str, archive_root: Path
) -> tuple[_StorePlan, Path]:
    plan = _build_purge_reference_plan(conn, terminal_id)
    snapshot_dir = _verify_plan_snapshot(archive_root, plan)
    return plan, snapshot_dir


def validate_purge_archived_lineage(
    db: SessionDB, terminal_id: str, archive_root: Path
) -> VerifiedLineage:
    """Run the final Purge eligibility gate without deleting any rows."""
    _require_supported_platform()
    archive_root = Path(os.path.abspath(os.fspath(archive_root)))
    conn = _connection(db)
    with db._lock:
        conn.execute("SAVEPOINT cold_purge_snapshot")
        try:
            plan, snapshot_dir = _validated_purge_plan(
                conn, terminal_id, archive_root
            )
        finally:
            conn.execute("RELEASE SAVEPOINT cold_purge_snapshot")
    return VerifiedLineage(
        plan.terminal_id,
        plan.physical_ids,
        plan.source_fingerprint,
        snapshot_dir,
    )


def purge_archived_lineage(
    db: SessionDB, terminal_id: str, archive_root: Path
) -> PurgedLineage:
    """Delete exactly one verified archived compression lineage from SQLite.

    Store eligibility, source records, and the existing snapshot fingerprint
    are re-read under the final ``BEGIN IMMEDIATE`` transaction. Archive files
    are read for verification only and are never removed by this operation.
    """
    _require_supported_platform()
    archive_root = Path(os.path.abspath(os.fspath(archive_root)))

    def _purge(conn: sqlite3.Connection) -> PurgedLineage:
        plan, snapshot_dir = _validated_purge_plan(
            conn, terminal_id, archive_root
        )

        prompt_hashes = tuple(
            sorted(
                {
                    str(record["row"]["hash"])
                    for record in plan.records
                    if record["kind"] == "system-prompt"
                }
            )
        )
        for ids in _id_chunks(plan.physical_ids):
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM messages WHERE session_id IN ({placeholders})", ids
            )
            conn.execute(
                "DELETE FROM session_model_usage "
                f"WHERE session_id IN ({placeholders})",
                ids,
            )
        deleted_rows = 0
        for ids in _id_chunks(tuple(reversed(plan.physical_ids))):
            placeholders = ",".join("?" for _ in ids)
            cursor = conn.execute(
                f"DELETE FROM sessions WHERE id IN ({placeholders})", ids
            )
            deleted_rows += cursor.rowcount
        if deleted_rows != len(plan.physical_ids):
            raise RuntimeError("cold purge source lineage changed during deletion")
        for hashes in _id_chunks(prompt_hashes):
            placeholders = ",".join("?" for _ in hashes)
            conn.execute(
                f"DELETE FROM system_prompts WHERE hash IN ({placeholders}) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM sessions "
                "WHERE sessions.system_prompt_hash = system_prompts.hash)",
                hashes,
            )
        return PurgedLineage(
            plan.terminal_id,
            plan.physical_ids,
            plan.source_fingerprint,
            snapshot_dir,
        )

    return db._execute_write(_purge)


def store_archived_lineage(db: SessionDB, terminal_id: str, archive_root: Path) -> StoredLineage:
    """Store one marked completed compression lineage without deleting DB rows.

    The safe terminal ID names one current snapshot. Repeated stores of the exact
    source are idempotent; a changed or damaged snapshot is staged, verified, and
    replaced. The source database is never modified by this operation.
    """
    _require_supported_platform()

    archive_root = Path(os.path.abspath(os.fspath(archive_root)))
    plan = _read_store_plan(db, terminal_id)

    lineage = plan.physical_ids
    records = plan.records
    fingerprint = plan.source_fingerprint
    snapshot_dir, snapshot_name, snapshot_parent_parts = _snapshot_location(
        archive_root, plan
    )
    descriptors, edges = _open_snapshot_parent(archive_root, snapshot_parent_parts)
    snapshot_parent_fd = descriptors[-1]
    staging_name: str | None = None
    staging_fd = -1
    published = False
    stale_name: str | None = None
    try:
        existing = _valid_existing_snapshot_at(
            snapshot_parent_fd,
            snapshot_name,
            terminal_id,
            lineage,
            fingerprint,
            len(records),
        )
        if existing is True:
            _validate_directory_chain(edges)
            return StoredLineage(terminal_id, lineage, fingerprint, snapshot_dir)

        staging_name, staging_fd = _create_staging_directory(snapshot_parent_fd)
        _validate_directory_chain(edges)
        metadata = {
            "format": _ARCHIVE_FORMAT,
            "terminal_id": terminal_id,
            "physical_ids": list(lineage),
            "source_fingerprint": fingerprint,
            "record_count": len(records),
        }
        _write_text_fsync_at(
            staging_fd,
            "metadata.json",
            json.dumps(metadata, sort_keys=True, indent=2) + "\n",
        )
        _write_text_fsync_at(
            staging_fd,
            "session.jsonl",
            "".join(
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
                for record in records
            ),
        )
        os.mkdir("artifacts", dir_fd=staging_fd)
        os.fsync(staging_fd)
        if not _valid_existing_snapshot_at(
            snapshot_parent_fd,
            staging_name,
            terminal_id,
            lineage,
            fingerprint,
            len(records),
        ):
            raise ValueError("staged cold-store snapshot failed verification")
        _validate_directory_chain(edges)
        if not _directory_entry_matches(snapshot_parent_fd, staging_name, staging_fd):
            raise ValueError("unsafe cold-store staging directory")
        stale_name = _move_current_snapshot_aside(snapshot_parent_fd, snapshot_name)
        os.rename(
            staging_name,
            snapshot_name,
            src_dir_fd=snapshot_parent_fd,
            dst_dir_fd=snapshot_parent_fd,
        )
        published = True
        os.fsync(snapshot_parent_fd)
        if stale_name is not None:
            _remove_stale_snapshot_at(snapshot_parent_fd, stale_name)
            try:
                os.fsync(snapshot_parent_fd)
            except OSError:
                pass
    finally:
        if staging_fd >= 0:
            if not published and staging_name is not None:
                _remove_staging_at(snapshot_parent_fd, staging_name, staging_fd)
            os.close(staging_fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return StoredLineage(terminal_id, lineage, fingerprint, snapshot_dir)
