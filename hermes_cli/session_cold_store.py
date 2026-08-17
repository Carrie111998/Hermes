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
    raw_config = row.get("model_config")
    try:
        config = json.loads(raw_config) if isinstance(raw_config, str) else raw_config or {}
    except json.JSONDecodeError:
        return True
    if not isinstance(config, dict):
        return True
    parent_id = row.get("parent_session_id")
    return bool(config.get("_branched_from") or config.get("_delegate_from")) and bool(parent_id)


def _raw_compression_lineage(conn: sqlite3.Connection, terminal_id: str) -> tuple[str, ...]:
    current = _session(conn, terminal_id)
    if current is None:
        raise ValueError(f"session not found: {terminal_id}")
    if _is_explicit_fork(current):
        return (terminal_id,)
    lineage = [str(current["id"])]
    seen = set(lineage)
    while current.get("parent_session_id"):
        parent = _session(conn, str(current["parent_session_id"]))
        if parent is None or parent.get("end_reason") != "compression" or _is_explicit_fork(current):
            break
        parent_id = str(parent["id"])
        if parent_id in seen:
            raise ValueError("cyclic compression lineage")
        lineage.append(parent_id)
        seen.add(parent_id)
        current = parent
    return tuple(reversed(lineage))


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
                "SELECT * FROM session_model_usage WHERE session_id = ? ORDER BY model, task",
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


def store_archived_lineage(db: SessionDB, terminal_id: str, archive_root: Path) -> StoredLineage:
    """Store one marked completed compression lineage without deleting DB rows.

    The terminal ID determines the logical session directory. A content-addressed
    fingerprint directory makes repeated stores idempotent and lets a later
    changed source produce a separate local revision.
    """
    conn = _connection(db)
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
    if ended_at is None:
        raise ValueError("terminal session must be ended before cold storage")

    records = _records(conn, lineage)
    fingerprint = _fingerprint(records)
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
        return StoredLineage(terminal_id, lineage, fingerprint, revision_dir)

    if os.name == "nt":
        raise OSError("cold store is not yet supported on Windows")

    revision_dir.parent.mkdir(parents=True, exist_ok=True)
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
