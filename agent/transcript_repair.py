"""Transcript repair and in-place row reconciliation helpers for SessionDB and run_agent.

Extracted from hermes_state.py and run_agent.py to keep the godfiles narrow and bounded
under the 2K invariant (#95514 / PR #95886). Provides focused helpers to:
1. Resolve active assistant rows and watermark compaction clones in SQLite during batch appends.
2. In-place update blank assistant rows or adopt concurrent non-blank winner content without overwrite.
3. Synchronize in-memory message dicts with canonical committed content and row IDs after commit.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Dict, List, Optional

from agent.context_compressor import _DB_PERSISTED_MARKER


def is_content_blank(content: Any) -> bool:
    """True when decoded message content is None, whitespace-only, or has no visible text or media parts."""
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, dict):
        if content.get("type") == "text":
            return not str(content.get("text", "")).strip()
        return False
    if isinstance(content, list):
        if not content:
            return True
        for part in content:
            if isinstance(part, str):
                if part.strip():
                    return False
            elif isinstance(part, dict):
                part_type = part.get("type")
                if part_type == "text":
                    if str(part.get("text", "")).strip():
                        return False
                elif part_type is not None:
                    # Non-text typed part (e.g. image, image_url, input_audio, document) is non-blank
                    return False
                else:
                    # Dict without a "type" field: check if text-only or multimodal payload
                    if any(k in part for k in ("image_url", "image", "source", "data", "audio", "document")):
                        return False
                    if "text" in part:
                        if str(part.get("text", "")).strip():
                            return False
                    else:
                        return False
            else:
                return False
        return True
    return False


def resolve_and_repair_transcript_batch(
    conn: sqlite3.Connection,
    session_id: str,
    messages: List[Dict[str, Any]],
    encode_content_fn: Callable[[Any], Any],
    decode_content_fn: Callable[[Any], Any],
) -> List[Dict[str, Any]]:
    """Partition a message batch within an active write transaction.

    For assistant messages carrying an existing integer `_row_id`:
    - Checks for an active target row or watermark compaction clone in SQLite.
    - If blank, updates the row in-place with new content.
    - If already non-blank (concurrent winner), adopts canonical content without overwrite.
    - Returns the list of messages that must be inserted as fresh rows.
    """
    inserted_rows: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "unknown") if isinstance(msg, dict) else "unknown"
        existing_row_id = msg.get("_row_id") if isinstance(msg, dict) else None
        repaired = False
        if role == "assistant" and isinstance(existing_row_id, int):
            row = conn.execute(
                "SELECT id, role, active, timestamp, content FROM messages "
                "WHERE id = ? AND session_id = ?",
                (existing_row_id, session_id),
            ).fetchone()
            target_row = None
            if row is not None and row["role"] == "assistant":
                if int(row["active"] or 0) == 1:
                    target_row = row
                else:
                    # Watermark compaction soft-archived the concurrent tail
                    # and cloned it. Find the active clone.
                    clone = conn.execute(
                        "SELECT id, role, active, timestamp, content FROM messages "
                        "WHERE session_id = ? AND active = 1 AND role = 'assistant' "
                        "AND timestamp IS ? AND id != ? "
                        "ORDER BY id DESC LIMIT 1",
                        (session_id, row["timestamp"], row["id"]),
                    ).fetchone()
                    if clone is not None:
                        target_row = clone
            if target_row is not None:
                target_id = int(target_row["id"])
                raw_content = target_row["content"]
                decoded = decode_content_fn(raw_content)
                if is_content_blank(decoded):
                    encoded = encode_content_fn(msg.get("content"))
                    conn.execute(
                        "UPDATE messages SET content = ? "
                        "WHERE id = ? AND session_id = ? AND active = 1",
                        (encoded, target_id, session_id),
                    )
                    if isinstance(msg, dict):
                        msg["_row_id"] = target_id
                else:
                    # Concurrent winner: adopt canonical content without overwrite
                    if isinstance(msg, dict):
                        msg["_row_id"] = target_id
                        msg["_canonical_content"] = decoded
                repaired = True
            else:
                # Target row is not in SQLite (e.g. purged/uncommitted); strip stale in-memory row id
                if isinstance(msg, dict) and "_row_id" in msg:
                    msg.pop("_row_id", None)
        if not repaired:
            inserted_rows.append(msg)
    return inserted_rows


def sync_flushed_message_markers(
    batch_msgs: List[Dict[str, Any]],
    batch_rows: List[Dict[str, Any]],
) -> None:
    """Stamp _DB_PERSISTED_MARKER and sync canonical row ID / content onto live dicts after commit."""
    for written, row in zip(batch_msgs, batch_rows):
        if isinstance(written, dict):
            written[_DB_PERSISTED_MARKER] = True
            if isinstance(row, dict):
                if isinstance(row.get("_row_id"), int):
                    written["_row_id"] = row["_row_id"]
                if "_canonical_content" in row:
                    written["content"] = row["_canonical_content"]
