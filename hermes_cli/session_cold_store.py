"""Store-only local cold archive primitives.

This first slice deliberately has no CLI registration and never deletes database
rows. It validates the basic Mark → Store handoff against a caller-supplied
``SessionDB`` and local archive root.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from typing import Any

from hermes_state import SessionDB, resolved_max_export_messages


_ARCHIVE_FORMAT = "hermes-cold-archive-store-spike/v1"
_MAX_SQLITE_IN_PARAMS = 500


@dataclass(frozen=True)
class StoredLineage:
    """Identity and immutable local snapshot emitted by :func:`store_archived_lineage`."""

    terminal_id: str
    physical_ids: tuple[str, ...]
    source_fingerprint: str
    revision_dir: Path


def _connection(db: SessionDB) -> sqlite3.Connection:
    if db._conn is None:
        raise RuntimeError("SessionDB connection is closed")
    return db._conn


def _safe_component(value: str) -> str:
    raw = str(value or "").strip()
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", raw).strip("._")[:96] or "session"
    if raw == safe:
        return safe
    return f"{safe}_{sha256(raw.encode('utf-8', 'surrogatepass')).hexdigest()[:12]}"


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
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
    except OSError:
        return None
    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            return None
        return source.read()


def _valid_existing_snapshot_at(
    snapshot_parent_fd: int,
    snapshot_name: str,
    terminal_id: str,
    lineage: tuple[str, ...],
    fingerprint: str,
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
            and metadata.get("record_count") == len(records)
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


def store_archived_lineage(db: SessionDB, terminal_id: str, archive_root: Path) -> StoredLineage:
    """Store one marked completed compression lineage without deleting DB rows.

    The safe terminal ID names a single immutable snapshot. Repeated stores of
    the exact source are idempotent; a changed source fails at the archival
    boundary instead of creating a revision or overwriting the snapshot.
    """
    if os.name == "nt":
        raise OSError("cold store is not yet supported on Windows")

    conn = _connection(db)
    with db._lock:
        conn.execute("SAVEPOINT cold_store_snapshot")
        try:
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
            ended_at = rows[-1].get("ended_at")
            if ended_at is None or rows[-1].get("end_reason") == "compression":
                raise ValueError("terminal session must be ended and non-compression before cold storage")

            _enforce_message_limit(conn, lineage)
            records = _records(conn, lineage)
            fingerprint = _fingerprint(records)
        finally:
            conn.execute("RELEASE SAVEPOINT cold_store_snapshot")

    terminal_date = datetime.fromtimestamp(float(ended_at), UTC)
    snapshot_name = _safe_component(terminal_id)
    snapshot_dir = (
        archive_root
        / "sessions"
        / "ended"
        / f"{terminal_date:%Y}"
        / f"{terminal_date:%m}"
        / f"{terminal_date:%d}"
        / snapshot_name
    )
    snapshot_parent_parts = (
        "sessions",
        "ended",
        f"{terminal_date:%Y}",
        f"{terminal_date:%m}",
        f"{terminal_date:%d}",
    )
    descriptors, edges = _open_snapshot_parent(archive_root, snapshot_parent_parts)
    snapshot_parent_fd = descriptors[-1]
    staging_name: str | None = None
    staging_fd = -1
    published = False
    try:
        existing = _valid_existing_snapshot_at(
            snapshot_parent_fd,
            snapshot_name,
            terminal_id,
            lineage,
            fingerprint,
        )
        if existing is True:
            return StoredLineage(terminal_id, lineage, fingerprint, snapshot_dir)
        if existing is False:
            raise ValueError(
                "cold-store archival boundary violation: existing snapshot is invalid "
                "or the marked source changed after Store"
            )

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
        _validate_directory_chain(edges)
        if not _directory_entry_matches(snapshot_parent_fd, staging_name, staging_fd):
            raise ValueError("unsafe cold-store staging directory")
        try:
            os.rename(
                staging_name,
                snapshot_name,
                src_dir_fd=snapshot_parent_fd,
                dst_dir_fd=snapshot_parent_fd,
            )
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            if _valid_existing_snapshot_at(
                snapshot_parent_fd,
                snapshot_name,
                terminal_id,
                lineage,
                fingerprint,
            ):
                return StoredLineage(terminal_id, lineage, fingerprint, snapshot_dir)
            raise ValueError(
                "cold-store archival boundary violation: existing snapshot is invalid "
                "or the marked source changed after Store"
            ) from exc
        published = True
        os.fsync(snapshot_parent_fd)
    finally:
        if staging_fd >= 0:
            if not published and staging_name is not None:
                _remove_staging_at(snapshot_parent_fd, staging_name, staging_fd)
            os.close(staging_fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return StoredLineage(terminal_id, lineage, fingerprint, snapshot_dir)
