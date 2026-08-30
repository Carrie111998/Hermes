"""Durable reply-token and action ledgers for Hermes intake callbacks.

The token is intentionally opaque to Marketing Brain.  Hermes keeps the
session and delivery correlation locally, alongside the callback's bounded
numbered-action state, so a restart cannot turn a callback retry into a new
chat action.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any


TTL_SECONDS = 24 * 60 * 60
DELIVERY_LEASE_SECONDS = 60


@dataclass(frozen=True)
class DeliveryClaim:
    """The durable state of a callback delivery claim."""

    status: str
    session_id: str = ""
    origin: dict[str, Any] | None = None


def _db_path() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS intake_reply_tokens (
        token TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        consumed_at INTEGER,
        delivery_id TEXT,
        delivery_state TEXT NOT NULL DEFAULT 'pending',
        delivery_lease_expires_at INTEGER,
        origin_json TEXT
        )"""
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(intake_reply_tokens)")
    }
    for name, declaration in (
        ("delivery_id", "TEXT"),
        ("delivery_state", "TEXT NOT NULL DEFAULT 'pending'"),
        ("delivery_lease_expires_at", "INTEGER"),
        ("origin_json", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE intake_reply_tokens ADD COLUMN {name} {declaration}")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS intake_callback_actions (
        delivery_id TEXT PRIMARY KEY,
        token TEXT NOT NULL,
        session_id TEXT NOT NULL,
        item_id INTEGER NOT NULL,
        analysis_attempt INTEGER NOT NULL,
        analysis_status TEXT NOT NULL,
        options_json TEXT NOT NULL,
        selected_option INTEGER,
        action_state TEXT NOT NULL DEFAULT 'pending',
        note_claimed_at INTEGER,
        approve_claimed_at INTEGER,
        process_claimed_at INTEGER,
        created_at INTEGER NOT NULL
        )"""
    )
    return conn


def _cleanup(conn: sqlite3.Connection, now: int) -> None:
    # Keep recent expired rows long enough to reject a stale retry explicitly,
    # but do not retain opaque capabilities indefinitely.
    conn.execute(
        "DELETE FROM intake_callback_actions WHERE created_at < ?", (now - TTL_SECONDS,)
    )
    conn.execute(
        "DELETE FROM intake_reply_tokens WHERE expires_at < ?", (now - TTL_SECONDS,)
    )


def _origin_json(origin: dict[str, Any]) -> str:
    return json.dumps(origin, sort_keys=True, separators=(",", ":"))


def _parse_origin(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def mint(session_id: str, *, now: int | None = None) -> str:
    """Mint one 24-hour opaque capability for an existing Hermes session."""
    if not session_id:
        raise ValueError("session_id required")
    now = int(time.time()) if now is None else int(now)
    token = secrets.token_urlsafe(32)
    with _connect() as conn:
        _cleanup(conn, now)
        conn.execute(
            """INSERT INTO intake_reply_tokens
            (token, session_id, expires_at, consumed_at, delivery_state)
            VALUES (?, ?, ?, NULL, 'pending')""",
            (token, session_id, now + TTL_SECONDS),
        )
    return token


def consume(token: str, *, now: int | None = None) -> str | None:
    """Compatibility helper: atomically consume a token without delivery."""
    now = int(time.time()) if now is None else int(now)
    with _connect() as conn:
        _cleanup(conn, now)
        row = conn.execute(
            """SELECT session_id FROM intake_reply_tokens
            WHERE token=? AND expires_at>? AND consumed_at IS NULL""",
            (token, now),
        ).fetchone()
        if row is None:
            return None
        result = conn.execute(
            """UPDATE intake_reply_tokens SET consumed_at=?, delivery_state='delivered'
            WHERE token=? AND consumed_at IS NULL""",
            (now, token),
        )
        return str(row["session_id"]) if result.rowcount == 1 else None


def session_id_for_token(token: str) -> str | None:
    """Return the local correlation id without consuming the capability."""
    if not token:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT session_id FROM intake_reply_tokens WHERE token=?", (token,)
        ).fetchone()
        return str(row["session_id"]) if row is not None else None


def begin_delivery(
    token: str, delivery_id: str, origin: dict[str, Any], *, now: int | None = None
) -> DeliveryClaim:
    """Atomically reserve a delivery, binding it to one source correlation."""
    now = int(time.time()) if now is None else int(now)
    if not token or not delivery_id or not origin:
        return DeliveryClaim("invalid")
    with _connect() as conn:
        _cleanup(conn, now)
        row = conn.execute(
            "SELECT * FROM intake_reply_tokens WHERE token=?", (token,)
        ).fetchone()
        if row is None:
            return DeliveryClaim("unknown")
        if int(row["expires_at"]) <= now:
            return DeliveryClaim("expired")
        existing_id = row["delivery_id"]
        state = str(row["delivery_state"] or "pending")
        if existing_id and existing_id != delivery_id:
            return DeliveryClaim("wrong_delivery")
        persisted_origin = _parse_origin(row["origin_json"]) or origin
        if state == "delivered":
            return DeliveryClaim("delivered", str(row["session_id"]), persisted_origin)
        if state == "delivering":
            lease = row["delivery_lease_expires_at"]
            if lease is not None and int(lease) > now:
                return DeliveryClaim("in_progress", str(row["session_id"]), persisted_origin)
            # A process may have died after sending but before recording the
            # receipt.  Retrying it could duplicate a chat message, so retain
            # an auditable terminal state instead of an immortal lease.
            conn.execute(
                """UPDATE intake_reply_tokens SET delivery_state='uncertain',
                   delivery_lease_expires_at=NULL
                   WHERE token=? AND delivery_id=? AND delivery_state='delivering'""",
                (token, delivery_id),
            )
            return DeliveryClaim("uncertain", str(row["session_id"]), persisted_origin)
        if state == "uncertain":
            return DeliveryClaim("uncertain", str(row["session_id"]), persisted_origin)
        updated = conn.execute(
            """UPDATE intake_reply_tokens
            SET delivery_id=?, delivery_state='delivering',
                delivery_lease_expires_at=?, origin_json=?
            WHERE token=? AND delivery_state!='delivering'""",
            (delivery_id, now + DELIVERY_LEASE_SECONDS, _origin_json(persisted_origin), token),
        )
        if updated.rowcount != 1:
            return DeliveryClaim("in_progress", str(row["session_id"]), persisted_origin)
        return DeliveryClaim("claimed", str(row["session_id"]), persisted_origin)


def finish_delivery(token: str, delivery_id: str, *, now: int | None = None) -> bool:
    """Commit a successful original-session delivery exactly once."""
    now = int(time.time()) if now is None else int(now)
    with _connect() as conn:
        result = conn.execute(
            """UPDATE intake_reply_tokens
            SET delivery_state='delivered', consumed_at=?, delivery_lease_expires_at=NULL
            WHERE token=? AND delivery_id=? AND delivery_state='delivering'""",
            (now, token, delivery_id),
        )
        return result.rowcount == 1


def release_delivery(token: str, delivery_id: str) -> None:
    """Make an unsuccessful delivery retryable with the same delivery id."""
    with _connect() as conn:
        conn.execute(
            """UPDATE intake_reply_tokens
            SET delivery_state='pending', delivery_lease_expires_at=NULL
            WHERE token=? AND delivery_id=? AND delivery_state='delivering'""",
            (token, delivery_id),
        )


def register_actions(
    *, token: str, delivery_id: str, session_id: str, payload: dict[str, Any], now: int | None = None
) -> bool:
    """Persist only the fixed callback choices which Hermes is allowed to run."""
    now = int(time.time()) if now is None else int(now)
    try:
        item_id = int(payload["item_id"])
        attempt = int(payload["analysis_attempt"])
        status = str(payload["analysis_status"])
    except (KeyError, TypeError, ValueError):
        return False
    expected = (
        {1: "approve_item", 2: "save_note_and_approve", 3: "leave_pending"}
        if status == "succeeded"
        else {1: "process_item", 2: "leave_pending"}
        if status == "failed"
        else None
    )
    raw_options = payload.get("numbered_replies")
    if not expected or not isinstance(raw_options, list) or item_id <= 0 or attempt <= 0:
        return False
    options: dict[int, dict[str, Any]] = {}
    for option in raw_options:
        if not isinstance(option, dict):
            return False
        try:
            number = int(option.get("number"))
        except (TypeError, ValueError):
            return False
        action = option.get("action")
        option_attempt = option.get("attempt")
        idempotency_key = option.get("idempotency_key")
        expected_key = f"link:{item_id}:attempt:{attempt}:{action}"
        if (
            expected.get(number) != action
            or option_attempt != attempt
            or not isinstance(idempotency_key, str)
            or idempotency_key != expected_key
        ):
            return False
        options[number] = {
            "action": action,
            "analysis_attempt": attempt,
            "idempotency_key": idempotency_key,
        }
    if {number: option["action"] for number, option in options.items()} != expected:
        return False
    options_json = json.dumps(options, sort_keys=True)
    with _connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO intake_callback_actions
            (delivery_id, token, session_id, item_id, analysis_attempt,
             analysis_status, options_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (delivery_id, token, session_id, item_id, attempt, status, options_json, now),
        )
        persisted = conn.execute(
            """SELECT token, session_id, item_id, analysis_attempt,
                      analysis_status, options_json
            FROM intake_callback_actions WHERE delivery_id=?""",
            (delivery_id,),
        ).fetchone()
        return bool(
            persisted is not None
            and persisted["token"] == token
            and persisted["session_id"] == session_id
            and int(persisted["item_id"]) == item_id
            and int(persisted["analysis_attempt"]) == attempt
            and persisted["analysis_status"] == status
            and persisted["options_json"] == options_json
        )


def discard_actions(delivery_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM intake_callback_actions WHERE delivery_id=?", (delivery_id,))


def select_action(session_id: str, choice: str, *, now: int | None = None) -> dict[str, Any] | None:
    """Record one numbered choice for a delivered callback, idempotently."""
    try:
        number = int(choice.strip())
    except (AttributeError, TypeError, ValueError):
        return None
    now = int(time.time()) if now is None else int(now)
    with _connect() as conn:
        _cleanup(conn, now)
        rows = conn.execute(
            """SELECT a.* FROM intake_callback_actions a
            JOIN intake_reply_tokens t ON t.token=a.token
            WHERE a.session_id=? AND a.action_state IN ('pending', 'selected')
              AND t.delivery_state='delivered' AND t.expires_at>?
            ORDER BY a.created_at DESC""",
            (session_id, now),
        ).fetchall()
        if len(rows) != 1:
            return None
        row = rows[0]
        options = json.loads(row["options_json"])
        selected = row["selected_option"]
        if selected is not None:
            return {"duplicate": True, "choice": int(selected), "delivery_id": row["delivery_id"]}
        option = options.get(str(number))
        if not isinstance(option, dict):
            return None
        result = conn.execute(
            """UPDATE intake_callback_actions
            SET selected_option=?, action_state='selected'
            WHERE delivery_id=? AND selected_option IS NULL AND action_state='pending'""",
            (number, row["delivery_id"]),
        )
        if result.rowcount != 1:
            return None
        return {
            "duplicate": False,
            "choice": number,
            "action": option["action"],
            "item_id": int(row["item_id"]),
            "analysis_attempt": int(option["analysis_attempt"]),
            "idempotency_key": option["idempotency_key"],
            "delivery_id": row["delivery_id"],
        }


def claim_action_tool(session_id: str, tool_name: str, args: dict[str, Any]) -> bool:
    """Allow a selected callback action once, and only for its exact item."""
    name = tool_name.removeprefix("mcp_mis_")
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM intake_callback_actions
            WHERE session_id=? AND action_state IN ('selected', 'note_claimed')
            ORDER BY created_at DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
        if row is None:
            return False
        options = json.loads(row["options_json"])
        option = options.get(str(row["selected_option"]))
        if not isinstance(option, dict):
            return False
        action = option.get("action")
        attempt = option.get("analysis_attempt")
        idempotency_key = option.get("idempotency_key")
        if (
            attempt != int(row["analysis_attempt"])
            or not isinstance(idempotency_key, str)
            or args.get("analysis_attempt") != attempt
            or args.get("idempotency_key") != idempotency_key
        ):
            return False
        if name != "ingest_note":
            try:
                item_id = int(args.get("item_id"))
            except (AttributeError, TypeError, ValueError):
                return False
            if item_id != int(row["item_id"]):
                return False
        if action == "approve_item" and name == "approve_item":
            column, state = "approve_claimed_at", "completed"
        elif action == "process_item" and name == "process_item":
            column, state = "process_claimed_at", "completed"
        elif action == "save_note_and_approve" and name == "ingest_note" and row["action_state"] == "selected":
            column, state = "note_claimed_at", "note_claimed"
        elif action == "save_note_and_approve" and name == "approve_item" and row["action_state"] == "note_claimed":
            column, state = "approve_claimed_at", "completed"
        else:
            return False
        result = conn.execute(
            f"UPDATE intake_callback_actions SET {column}=?, action_state=? "
            "WHERE delivery_id=? AND action_state=?",
            (int(time.time()), state, row["delivery_id"], row["action_state"]),
        )
        return result.rowcount == 1


def bound_action_args(session_id: str, tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Return the exact, attempt-bound arguments for a selected callback action.

    The callback option is the source of truth.  Model-provided attempts and
    keys are ignored rather than trusted; a mismatched tool or stale selection
    returns ``None`` and remains approval-gated.
    """
    name = tool_name.removeprefix("mcp_mis_")
    with _connect() as conn:
        row = conn.execute(
            """SELECT * FROM intake_callback_actions
            WHERE session_id=? AND action_state IN ('selected', 'note_claimed')
            ORDER BY created_at DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        option = json.loads(row["options_json"]).get(str(row["selected_option"]))
        if not isinstance(option, dict):
            return None
        action = option.get("action")
        if action == "approve_item" and name != "approve_item":
            return None
        if action == "process_item" and name != "process_item":
            return None
        # The callback contract has no immutable note body.  Do not let a
        # later conversational turn turn option 2 into arbitrary text intake.
        if action == "save_note_and_approve":
            return None
        if action not in {"approve_item", "process_item"}:
            return None
        return {
            "item_id": int(row["item_id"]),
            "analysis_attempt": int(option["analysis_attempt"]),
            "idempotency_key": str(option["idempotency_key"]),
        }
