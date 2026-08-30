"""Storage seam for a single Discord notification pin per Kanban board.

The rest of Kanban should depend on the four public functions in this
module rather than on the SQLite representation.  In particular, a persisted
row is untrusted input: reads validate it before returning it, so malformed
state can never become a notification destination.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Optional, cast


_BOARD_NOTIFY_TABLE = "kanban_board_notify"
_BOARD_NOTIFY_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_BOARD_NOTIFY_TABLE} (
    board_id  TEXT PRIMARY KEY NOT NULL,
    dest_json TEXT NOT NULL
)
"""
_DEST_KEYS = frozenset(("platform", "chat_id", "thread_id"))
_SNOWFLAKE_RE = re.compile(r"[0-9]+\Z", re.ASCII)
_MAX_SNOWFLAKE = (1 << 64) - 1


def _normalize_snowflake(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    if not _SNOWFLAKE_RE.fullmatch(value):
        return None
    try:
        number = int(value, 10)
    except (TypeError, ValueError):
        return None
    if not 0 < number <= _MAX_SNOWFLAKE:
        return None
    return str(number)


def parse_discord_board_pin(dest: object) -> Optional[dict[str, str]]:
    """Return a canonical Discord destination, or ``None`` if it is invalid.

    A pin is intentionally limited to the exact three-field mapping needed for
    a Discord thread.  The platform must be the exact lowercase string
    ``"discord"`` and snowflake strings are normalized to their decimal
    representation.  Extra fields and non-mapping values are rejected so
    serialized junk cannot leak into later routing decisions.
    """
    if not isinstance(dest, Mapping):
        return None
    candidate = cast(Mapping[str, object], dest)
    try:
        if set(candidate) != _DEST_KEYS:
            return None
        platform = candidate["platform"]
        chat_id = _normalize_snowflake(candidate["chat_id"])
        thread_id = _normalize_snowflake(candidate["thread_id"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if platform != "discord":
        return None
    if chat_id is None or thread_id is None:
        return None
    return {
        "platform": "discord",
        "chat_id": chat_id,
        "thread_id": thread_id,
    }


def _normalize_board_id(board_id: object) -> str:
    if not isinstance(board_id, str) or not board_id.strip():
        raise ValueError("board_id must be a non-empty string")
    return board_id.strip()


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_BOARD_NOTIFY_SCHEMA)


def _commit_if_started(conn: sqlite3.Connection, was_in_transaction: bool) -> None:
    if not was_in_transaction:
        conn.commit()


def get_board_notify(
    conn: sqlite3.Connection, board_id: str,
) -> Optional[dict[str, str]]:
    """Read and validate the pin for ``board_id``.

    Missing rows, invalid JSON, and destinations that fail validation all map
    to ``None``.  The caller never needs to know whether a row was malformed.
    """
    board_key = _normalize_board_id(board_id)
    _ensure_table(conn)
    row = conn.execute(
        f"SELECT dest_json FROM {_BOARD_NOTIFY_TABLE} WHERE board_id = ?",
        (board_key,),
    ).fetchone()
    if row is None:
        return None
    try:
        persisted = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parse_discord_board_pin(persisted)


def set_board_notify(
    conn: sqlite3.Connection, board_id: str, dest: object,
) -> dict[str, str]:
    """Validate and persist the pin, replacing the board's prior pin.

    ``ValueError`` is raised before any write when ``dest`` is not a valid
    Discord thread destination.  The primary key makes replacement atomic and
    guarantees at most one row per board.
    """
    board_key = _normalize_board_id(board_id)
    canonical = parse_discord_board_pin(dest)
    if canonical is None:
        raise ValueError("board notification destination must be a Discord thread")

    was_in_transaction = conn.in_transaction
    try:
        _ensure_table(conn)
        conn.execute(
            f"""
            INSERT INTO {_BOARD_NOTIFY_TABLE} (board_id, dest_json)
            VALUES (?, ?)
            ON CONFLICT(board_id) DO UPDATE SET dest_json = excluded.dest_json
            """,
            (board_key, json.dumps(canonical, separators=(",", ":"))),
        )
        _commit_if_started(conn, was_in_transaction)
    except Exception:
        if not was_in_transaction:
            conn.rollback()
        raise
    return canonical


def clear_board_notify(conn: sqlite3.Connection, board_id: str) -> None:
    """Remove the pin for ``board_id``; clearing an absent pin is harmless."""
    board_key = _normalize_board_id(board_id)
    was_in_transaction = conn.in_transaction
    try:
        _ensure_table(conn)
        conn.execute(
            f"DELETE FROM {_BOARD_NOTIFY_TABLE} WHERE board_id = ?",
            (board_key,),
        )
        _commit_if_started(conn, was_in_transaction)
    except Exception:
        if not was_in_transaction:
            conn.rollback()
        raise
