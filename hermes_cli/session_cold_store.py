"""Store-only local cold archive primitives.

This first slice deliberately has no CLI registration and never deletes database
rows. It validates the basic Mark → Store handoff against a caller-supplied
``SessionDB`` and local archive root.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any

from hermes_state import SessionDB


@dataclass(frozen=True)
class StoredLineage:
    """Identity and exact local revision emitted by :func:`store_archived_lineage`."""

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


def _write_text_fsync(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _valid_existing_revision(revision_dir: Path, terminal_id: str, lineage: tuple[str, ...], fingerprint: str) -> bool:
    if not revision_dir.is_dir() or revision_dir.is_symlink():
        return False
    metadata_path = revision_dir / "metadata.json"
    payload_path = revision_dir / "session.jsonl"
    if any(not path.is_file() or path.is_symlink() for path in (metadata_path, payload_path)):
        return False
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        records = [json.loads(line) for line in payload_path.read_text(encoding="utf-8").splitlines()]
    except (OSError, json.JSONDecodeError):
        return False
    return (
        metadata.get("terminal_id") == terminal_id
        and metadata.get("physical_ids") == list(lineage)
        and metadata.get("source_fingerprint") == fingerprint
        and metadata.get("record_count") == len(records)
        and _fingerprint(records) == fingerprint
        and (revision_dir / "artifacts").is_dir()
        and not (revision_dir / "artifacts").is_symlink()
    )


def _ensure_dir_tree_fsynced(path: Path) -> None:
    """Create a directory chain and fsync every new entry plus its parent."""
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir()
        _fsync_dir(directory)
        _fsync_dir(directory.parent)


def store_archived_lineage(db: SessionDB, terminal_id: str, archive_root: Path) -> StoredLineage:
    """Store one marked completed compression lineage without deleting DB rows.

    The terminal ID determines the logical session directory. A content-addressed
    fingerprint directory makes repeated stores idempotent and lets a later
    changed source produce a separate local revision.
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

            records = _records(conn, lineage)
            fingerprint = _fingerprint(records)
        finally:
            conn.execute("RELEASE SAVEPOINT cold_store_snapshot")

    terminal_date = datetime.fromtimestamp(float(ended_at), UTC)
    revision_dir = (
        archive_root
        / "sessions"
        / "ended"
        / f"{terminal_date:%Y}"
        / f"{terminal_date:%m}"
        / f"{terminal_date:%d}"
        / _safe_component(terminal_id)
        / "revisions"
        / fingerprint
    )
    if revision_dir.exists():
        if _valid_existing_revision(revision_dir, terminal_id, lineage, fingerprint):
            return StoredLineage(terminal_id, lineage, fingerprint, revision_dir)
        raise ValueError("existing cold-store revision is invalid or mismatched")

    _ensure_dir_tree_fsynced(revision_dir.parent)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=revision_dir.parent))
    try:
        metadata = {
            "format": "hermes-cold-archive-store-spike/v1",
            "terminal_id": terminal_id,
            "physical_ids": list(lineage),
            "source_fingerprint": fingerprint,
            "record_count": len(records),
        }
        _write_text_fsync(staging / "metadata.json", json.dumps(metadata, sort_keys=True, indent=2) + "\n")
        _write_text_fsync(
            staging / "session.jsonl",
            "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records),
        )
        (staging / "artifacts").mkdir()
        _fsync_dir(staging)
        os.replace(staging, revision_dir)
        _fsync_dir(revision_dir.parent)
    except BaseException:
        if staging.exists():
            for item in staging.iterdir():
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    item.rmdir()
            staging.rmdir()
        raise
    return StoredLineage(terminal_id, lineage, fingerprint, revision_dir)
