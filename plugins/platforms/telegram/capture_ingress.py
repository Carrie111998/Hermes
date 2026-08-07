"""Capture ingress — pre-dispatch update interception queue for Telegram.

Provides a capture-aware asyncio.Queue that intercepts PTB Update objects
before they reach PTB's own dispatch loop, guaranteeing durable pre-ack
ledger recording for capture-only routes per Slice 1.1R-B.
"""

import asyncio
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

_PROFILE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

# Route key: (chat_id, thread_id or 0 for None)
RouteKey = Tuple[int, int]


def build_event_id(profile: str, account_id: int, update_id: int) -> str:
    """Build capture event identity string.

    Format: telegram:{profile}:{account_id}:{update_id}
    Must match ingress-ledger.schema.json's event_id regex.
    """
    if not _PROFILE_RE.match(profile):
        raise ValueError(f"Invalid profile name for event_id: {profile!r}")
    return f"telegram:{profile}:{account_id}:{update_id}"


def canonicalize_payload(data: dict) -> bytes:
    """Produce compact sorted-key UTF-8 JSON bytes from a dict.

    Deterministic output regardless of input key order. No trailing newline.
    """
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def payload_hash(data: dict) -> str:
    """SHA-256 hex digest of canonicalized payload bytes."""
    return f"sha256:{hashlib.sha256(canonicalize_payload(data)).hexdigest()}"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


CAPTURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingress_ledger (
    event_id TEXT PRIMARY KEY,
    profile TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    update_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    route_mode TEXT NOT NULL,
    sink TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    received_at TEXT NOT NULL,
    message_thread_id INTEGER,
    media_kind TEXT,
    command_text TEXT,
    content_preview TEXT,
    lifecycle TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capture_payload (
    event_id TEXT PRIMARY KEY REFERENCES ingress_ledger(event_id),
    payload BLOB NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


def open_capture_db(path) -> sqlite3.Connection:
    """Open (creating parents + schema) the capture ledger DB at ``path``."""
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(CAPTURE_SCHEMA)
    conn.commit()
    return conn


def atomic_insert_capture(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    profile: str,
    account_id: int,
    update_id: int,
    chat_id: int,
    message_id: int,
    sender_id: int,
    event_type: str,
    route_mode: str,
    sink: str,
    payload: bytes,
    payload_hash_str: str,
    message_thread_id: Optional[int] = None,
    media_kind: Optional[str] = None,
    command_text: Optional[str] = None,
    content_preview: Optional[str] = None,
) -> None:
    """Atomically insert a ledger row + companion payload in one transaction.

    Idempotent on identical payload_hash; raises sqlite3.IntegrityError when
    the same event_id already exists with a different payload_hash
    (conflicting replay — fail closed, no overwrite).
    """
    now = _utcnow_iso()
    with conn:
        existing = conn.execute(
            "SELECT payload_hash FROM ingress_ledger WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            if existing[0] != payload_hash_str:
                raise sqlite3.IntegrityError(
                    f"Conflicting payload for event_id={event_id}: "
                    f"existing={existing[0]} new={payload_hash_str}"
                )
            return  # Idempotent: same identity, same hash — first capture wins

        conn.execute(
            """INSERT INTO ingress_ledger
               (event_id, profile, account_id, update_id, chat_id,
                message_id, sender_id, event_type, route_mode, sink,
                payload_hash, received_at, message_thread_id, media_kind,
                command_text, content_preview, lifecycle, retry_count,
                recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)""",
            (
                event_id, profile, account_id, update_id, chat_id,
                message_id, sender_id, event_type, route_mode, sink,
                payload_hash_str, now, message_thread_id, media_kind,
                command_text, content_preview, now,
            ),
        )
        conn.execute(
            """INSERT INTO capture_payload (event_id, payload, recorded_at)
               VALUES (?, ?, ?)""",
            (event_id, payload, now),
        )


class CaptureIngressQueue(asyncio.Queue):
    """An asyncio.Queue that intercepts PTB Updates before inner dispatch.

    Installed at ``ApplicationBuilder(...).update_queue(...)``. Every ``put``
    is intercepted: message-like updates on capture-only routes are atomically
    persisted then consumed without delegating to the inner queue; agent-route
    updates persist THEN delegate; non-message-like updates pass through
    unchanged.

    Parameters
    ----------
    inner_queue: The underlying asyncio.Queue that feeds PTB's dispatcher.
    db_connection: SQLite connection for atomic ledger+payload writes.
    profile: Immutable adapter receiving-profile name.
    account_id: Telegram bot account ID.
    route_map: {(chat_id, thread_id_or_0): {"mode": ..., "sink": ...}}
    """

    def __init__(
        self,
        inner_queue: asyncio.Queue,
        db_connection,
        profile: str,
        account_id: int,
        route_map: Dict[RouteKey, Dict[str, str]],
    ):
        super().__init__()
        self._inner = inner_queue
        self._db = db_connection
        self._profile = profile
        self._account_id = account_id
        self._route_map = route_map

    async def put(self, update: Any) -> None:
        """Intercept an Update before PTB dispatch.

        Non-Update sentinels and non-message-like updates pass through
        unchanged via super().put() so PTB's own dispatcher receives them.
        Message-like updates are routed by (chat_id, thread_id); capture_only
        routes are consumed here (terminal deny); agent routes persist THEN
        delegate via super().put().
        """
        # PTB enqueues telegram.Update objects; tests and replays enqueue the
        # equivalent dict. Normalize for inspection but delegate the ORIGINAL
        # object so PTB's dispatcher gets what it expects.
        raw = update.to_dict() if hasattr(update, "to_dict") else update

        effective_msg = (
            self._get_effective_message(raw)
            if self._is_message_like(raw)
            else None
        )
        if effective_msg is None:
            await super().put(update)
            return

        chat_id = effective_msg.get("chat", {}).get("id")
        thread_id = effective_msg.get("message_thread_id")
        route = self._route_map.get((chat_id, thread_id or 0))

        if route is None or route["mode"] == "drop":
            await super().put(update)
            return

        event_type = self._classify_event_type(effective_msg)
        update_id = raw.get("update_id", 0)
        message_id = effective_msg.get("message_id", 0)
        sender_id = effective_msg.get("from", {}).get("id", 0)
        event_id = build_event_id(self._profile, self._account_id, update_id)
        text = effective_msg.get("text") or effective_msg.get("caption") or ""

        try:
            atomic_insert_capture(
                self._db,
                event_id=event_id,
                profile=self._profile,
                account_id=self._account_id,
                update_id=update_id,
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
                event_type=event_type,
                route_mode=route["mode"],
                sink=route["sink"],
                payload=canonicalize_payload(raw),
                payload_hash_str=payload_hash(raw),
                message_thread_id=thread_id,
                command_text=(text if event_type == "command" else None),
                content_preview=text[:200] or None,
            )
        except sqlite3.IntegrityError:
            # Conflicting payload for a known identity — fail closed:
            # no overwrite, no delegation, no ACK.
            import logging
            logging.getLogger(__name__).warning(
                "CaptureIngressQueue: conflicting payload for event_id=%s — "
                "dropping update (fail-closed)", event_id
            )
            return

        if route["mode"] == "capture_only":
            return  # Terminal deny: consumed without delegating

        # Agent route: persist-then-delegate via super().put()
        await super().put(update)

    # --- Internal helpers ---

    @staticmethod
    def _is_message_like(update: Any) -> bool:
        """Return True if the PTB Update carries an effective_message."""
        if not isinstance(update, dict):
            return False
        return bool(
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or update.get("edited_channel_post")
        )

    @staticmethod
    def _get_effective_message(update: dict) -> Optional[dict]:
        """Extract the effective_message dict from a PTB Update."""
        for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
            msg = update.get(key)
            if msg:
                return msg
        return None

    @staticmethod
    def _classify_event_type(msg: dict) -> str:
        """Classify a message dict into an event_type string."""
        text = msg.get("text") or msg.get("caption") or ""
        if msg.get("location") or msg.get("venue"):
            return "location"
        if (
            msg.get("photo") or msg.get("video") or msg.get("audio")
            or msg.get("voice") or msg.get("document") or msg.get("sticker")
        ):
            return "media"
        if text.startswith("/"):
            return "command"
        if text:
            return "text"
        return "other"
