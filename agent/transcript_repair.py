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
    """True when decoded message content is None, whitespace-only, or has no visible text parts."""
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        if not content:
            return True
        texts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        return not "".join(texts).strip()
    return False


def _has_nonempty_media_payload(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (bytes, bytearray)):
        return bool(value)
    if isinstance(value, dict):
        return any(
            _has_nonempty_media_payload(value.get(key)) for key in ("url", "data")
        )
    return False


def _has_visible_repair_content(content: Any) -> bool:
    """True when repaired content has a user-visible text or media payload."""
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(_has_visible_repair_content(part) for part in content)
    if not isinstance(content, dict):
        return False

    part_type = str(content.get("type") or "").strip().lower()
    if part_type in {"text", "input_text", "output_text"}:
        return any(
            isinstance(content.get(key), str) and bool(content[key].strip())
            for key in ("text", "content")
        )
    if not part_type and isinstance(content.get("text"), str):
        return bool(content["text"].strip())
    if part_type in {"image_url", "input_image", "image"}:
        return any(
            _has_nonempty_media_payload(content.get(key))
            for key in ("image_url", "url", "data", "source")
        )
    if part_type in {"input_audio", "audio"}:
        return any(
            _has_nonempty_media_payload(content.get(key))
            for key in ("input_audio", "audio", "audio_url", "url", "data")
        )
    return False


def resolve_and_repair_transcript_batch(
    conn: sqlite3.Connection,
    session_id: str,
    messages: List[Dict[str, Any]],
    encode_content_fn: Callable[[Any], Any],
    decode_content_fn: Callable[[Any], Any],
) -> tuple[List[Dict[str, Any]], int]:
    """Partition a message batch within an active write transaction.

    For assistant messages carrying an existing integer `_row_id`:
    - Checks for an active target row or watermark compaction clone in SQLite.
    - If blank, updates the row in-place with new content.
    - If already non-blank (concurrent winner), adopts canonical content without overwrite.
    - Returns the fresh-insert rows and count of in-place visible repairs.
    """
    inserted_rows: List[Dict[str, Any]] = []
    repaired_visible_rows = 0
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
                    incoming_decoded = decode_content_fn(encoded)
                    if decoded != incoming_decoded:
                        updated = conn.execute(
                            "UPDATE messages SET content = ? "
                            "WHERE id = ? AND session_id = ? AND active = 1",
                            (encoded, target_id, session_id),
                        )
                        if (
                            updated.rowcount > 0
                            and raw_content != encoded
                            and _has_visible_repair_content(incoming_decoded)
                        ):
                            repaired_visible_rows += 1
                    if isinstance(msg, dict):
                        msg["_row_id"] = target_id
                        if decoded == incoming_decoded:
                            msg["_canonical_content"] = decoded
                else:
                    # Concurrent winner: adopt canonical content without overwrite
                    if isinstance(msg, dict):
                        msg["_row_id"] = target_id
                        msg["_canonical_content"] = decoded
                repaired = True
        if not repaired:
            inserted_rows.append(msg)
    return inserted_rows, repaired_visible_rows


def sync_flushed_message_markers(
    batch_msgs: List[Dict[str, Any]],
    batch_rows: List[Dict[str, Any]],
) -> None:
    """Stamp _DB_PERSISTED_MARKER and sync canonical row ID / content onto live dicts after commit."""
    for written, row in zip(batch_msgs, batch_rows):
        written[_DB_PERSISTED_MARKER] = True
        if isinstance(row.get("_row_id"), int):
            written["_row_id"] = row["_row_id"]
        if "_canonical_content" in row:
            written["content"] = row["_canonical_content"]
