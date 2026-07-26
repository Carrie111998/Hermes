"""Durable signal controlling new ChatGPT Pro bridge dispatches."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Tuple

from hermes_cli.cost import turns_schema
from hermes_cli.sqlite_util import retrying_write_txn


_STATE_KEY = "bridge_fallthrough_disabled"


def _utc_now() -> str:
    from hermes_cli.cost.turns_ledger import utc_now

    return utc_now()


def set_fallthrough_disabled(
    disabled: bool,
    reason: str,
    db_path: str | Path | None = None,
) -> None:
    """Persist whether new subscription-bridge dispatches are allowed."""
    turns_schema.ensure_migrated(db_path)
    payload = json.dumps(
        {
            "disabled": bool(disabled),
            "reason": str(reason or "").strip() or None,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    conn = turns_schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            conn.execute(
                """
                INSERT INTO bridge_state (key, value, updated_ts)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_ts = excluded.updated_ts
                """,
                (_STATE_KEY, payload, _utc_now()),
            )
    finally:
        conn.close()


def is_fallthrough_disabled(
    db_path: str | Path | None = None,
) -> Tuple[bool, Optional[str]]:
    """Return the current halt flag without creating a default row."""
    turns_schema.ensure_migrated(db_path)
    conn = turns_schema.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM bridge_state WHERE key = ?",
            (_STATE_KEY,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return False, None
    try:
        payload = json.loads(str(row["value"]))
    except (TypeError, json.JSONDecodeError):
        return False, None
    if not isinstance(payload, dict):
        return False, None
    disabled = bool(payload.get("disabled"))
    reason = payload.get("reason")
    return disabled, str(reason) if disabled and reason else None


__all__ = [
    "is_fallthrough_disabled",
    "set_fallthrough_disabled",
]
