"""Capture-first Telegram intake (Slice 1.1R-B).

Implements the capture-aware ``asyncio.Queue`` seam described in
``docs/hermes-program/phases/01-reliable-capture/slice-1.1r-b-capture-first-intake.md``
(hermes-control-plane repo): every message-like Telegram update is durably
recorded -- ledger row plus companion payload, committed atomically -- before
PTB's own dispatch ever sees it. Both the polling loop and the webhook
handler await ``update_queue.put(update)`` before advancing/acking, so
gating admission at ``put()`` is what makes the durable-before-ack guarantee
hold for either transport.

Row/field shapes here match the merged Slice 1.1R-A contract
(``capture/schema/ingress-ledger.schema.json`` and
``capture/schema/route-policy.schema.json`` in hermes-control-plane) exactly;
this module is the runtime that produces rows conforming to that contract,
not a redefinition of it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from telegram import Update
except ImportError:  # pragma: no cover - PTB always present alongside adapter.py
    Update = None  # type: ignore

# Matches TelegramAdapter._GENERAL_TOPIC_THREAD_ID. Telegram's implicit
# General forum topic is addressed as thread id "1" but route-policy and the
# ingress-ledger both normalize "no specific topic" (non-forum chat OR the
# General topic) to a single value: null.
GENERAL_TOPIC_SENTINEL = "1"


def normalize_thread_id(raw_thread_id: Any) -> Optional[int]:
    """Collapse "no topic" and "the General topic" to the same null value.

    ``raw_thread_id`` is whatever TelegramAdapter._effective_message_thread_id
    already returned (None, or a numeric-string thread id, or the General
    topic sentinel "1") -- this function only applies the route-policy/ledger
    normalization rule on top of that, it does not itself detect forum-ness.
    """
    if raw_thread_id is None:
        return None
    text = str(raw_thread_id).strip()
    if not text or text == GENERAL_TOPIC_SENTINEL:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def canonicalize_update(update: "Update") -> bytes:
    """Compact sorted-key UTF-8 JSON over ``Update.to_dict()`` -- frozen wire shape."""
    payload = update.to_dict()
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_payload_hash(payload_bytes: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload_bytes).hexdigest()


def compute_event_id(profile: str, account_id: int, update_id: int) -> str:
    return f"telegram:{profile}:{account_id}:{update_id}"


def classify_event_type(message: Any) -> str:
    """Content kind of a message-like update, per the ingress-ledger enum."""
    text = getattr(message, "text", None) or getattr(message, "caption", None)
    if text and str(text).startswith("/"):
        return "command"
    if getattr(message, "location", None) is not None or getattr(message, "venue", None) is not None:
        return "location"
    for attr in ("photo", "video", "audio", "voice", "document", "sticker", "video_note", "animation"):
        if getattr(message, attr, None):
            return "media"
    if text:
        return "text"
    return "other"


class RouteConflict(Exception):
    """Same event_id captured again with a different canonical payload/hash."""


class CapturePersistenceError(Exception):
    """Ledger/payload commit failed for a reason that must fail closed (never a coding bug)."""


@dataclass(frozen=True)
class RouteEntry:
    chat_id: int
    thread_id: Optional[int]
    mode: str  # "capture_only" | "agent" | "drop"
    sink: str
    policy_version: str = "1.0.0"


class RoutePolicyTable:
    """(chat_id, normalized thread_id) -> RouteEntry, from route-policy-shaped dicts.

    Exact match only: both chat_id and normalized thread_id must match. There
    is no chat-level wildcard/fallback -- a chat with no matching entry (or a
    thread number that only matches in a *different* chat) has no route, and
    an update on it is out of scope for capture, identical to today's
    behavior (see "Exact route matching" in the plan's acceptance matrix).
    """

    def __init__(self, entries: Optional[List[Dict[str, Any]]] = None):
        self._by_key: Dict[Tuple[int, Optional[int]], RouteEntry] = {}
        for raw in entries or []:
            entry = RouteEntry(
                chat_id=int(raw["chat_id"]),
                thread_id=normalize_thread_id(raw.get("thread_id")),
                mode=str(raw["mode"]),
                sink=str(raw["sink"]),
                policy_version=str(raw.get("policy_version", "1.0.0")),
            )
            self._by_key[(entry.chat_id, entry.thread_id)] = entry

    def lookup(self, chat_id: Any, thread_id: Optional[int]) -> Optional[RouteEntry]:
        if chat_id is None:
            return None
        return self._by_key.get((int(chat_id), thread_id))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingress_ledger (
    event_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    profile TEXT NOT NULL,
    update_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER,
    message_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    route_mode TEXT NOT NULL,
    sink TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    lease_expires_at TEXT,
    next_attempt_at TEXT,
    last_error TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS ingress_payload (
    event_id TEXT PRIMARY KEY REFERENCES ingress_ledger(event_id),
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""

INSERTED = "inserted"
DUPLICATE_SAME = "duplicate_same"


class CaptureIngressStore:
    """Durable ledger + companion-payload store, one atomic transaction per capture.

    A real SQLite database (never mocked persistence) keyed on ``event_id``,
    matching the merged ingress-ledger contract exactly -- 19 required
    fields, ``additionalProperties: false`` in the JSON Schema sense (the
    table simply has no other columns).
    """

    def __init__(self, db_path: "str | Path"):
        self._db_path = str(db_path)
        parent = Path(self._db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, isolation_level=None, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def commit_capture(
        self,
        *,
        event_id: str,
        platform: str,
        account_id: int,
        profile: str,
        update_id: int,
        chat_id: int,
        thread_id: Optional[int],
        message_id: int,
        sender_id: int,
        event_type: str,
        received_at: str,
        payload_hash: str,
        route_mode: str,
        sink: str,
        payload_json: str,
    ) -> str:
        """Idempotent atomic insert. Returns INSERTED or DUPLICATE_SAME.

        Raises RouteConflict if ``event_id`` already exists with a different
        ``payload_hash`` (never overwritten), or CapturePersistenceError on
        any other storage failure (disk full, lock, corruption) -- the
        caller must treat either as fail-closed: no delegation, no ack.
        """
        with self._lock:
            try:
                existing = self._conn.execute(
                    "SELECT payload_hash FROM ingress_ledger WHERE event_id = ?", (event_id,)
                ).fetchone()
            except sqlite3.Error as exc:
                raise CapturePersistenceError(str(exc)) from exc
            if existing is not None:
                if existing[0] == payload_hash:
                    return DUPLICATE_SAME
                raise RouteConflict(
                    f"event_id {event_id!r} already captured with a different payload_hash"
                )
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    """
                    INSERT INTO ingress_ledger (
                        event_id, platform, account_id, profile, update_id,
                        chat_id, thread_id, message_id, sender_id, event_type,
                        received_at, payload_hash, route_mode, sink,
                        status, attempts, lease_expires_at, next_attempt_at,
                        last_error, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, NULL)
                    """,
                    (
                        event_id, platform, account_id, profile, update_id,
                        chat_id, thread_id, message_id, sender_id, event_type,
                        received_at, payload_hash, route_mode, sink,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO ingress_payload (event_id, payload_hash, payload_json) VALUES (?, ?, ?)",
                    (event_id, payload_hash, payload_json),
                )
                self._conn.execute("COMMIT")
            except sqlite3.IntegrityError:
                self._conn.execute("ROLLBACK")
                # Lost a race against a concurrent insert of the same event_id.
                existing = self._conn.execute(
                    "SELECT payload_hash FROM ingress_ledger WHERE event_id = ?", (event_id,)
                ).fetchone()
                if existing is not None and existing[0] == payload_hash:
                    return DUPLICATE_SAME
                raise RouteConflict(
                    f"event_id {event_id!r} already captured with a different payload_hash"
                )
            except sqlite3.Error as exc:
                self._conn.execute("ROLLBACK")
                raise CapturePersistenceError(str(exc)) from exc
            return INSERTED


class CaptureAwareQueue(asyncio.Queue):
    """``asyncio.Queue`` that captures message-like updates before admission.

    Installed via ``ApplicationBuilder(...).update_queue(...)``. PTB's
    polling loop only advances ``_last_update_id`` after ``put()`` returns,
    and its webhook handler only completes the HTTP response after the same
    ``put()`` -- so raising here (persistence failure or a conflicting
    duplicate) keeps both transports from acknowledging, and returning
    without delegating to the real queue (capture-only terminal deny) keeps
    the update out of dispatch entirely.
    """

    def __init__(
        self,
        *,
        store: CaptureIngressStore,
        route_table_provider: Callable[[], RoutePolicyTable],
        account_id_provider: Callable[[], int],
        profile_provider: Callable[[], str],
        thread_id_resolver: Callable[[Any], Optional[str]],
        is_own_message: Callable[[Any], bool],
        is_authorized_sender: Callable[[Any], bool],
        alert_failure: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._route_table_provider = route_table_provider
        self._account_id_provider = account_id_provider
        self._profile_provider = profile_provider
        self._thread_id_resolver = thread_id_resolver
        self._is_own_message = is_own_message
        self._is_authorized_sender = is_authorized_sender
        self._alert_failure = alert_failure or (lambda _msg: None)

    async def put(self, item: Any) -> None:
        if Update is None or not isinstance(item, Update):
            return await super().put(item)  # non-Update sentinel (shutdown marker, etc.)

        message = getattr(item, "effective_message", None)
        if message is None:
            return await super().put(item)  # not message-like: callback_query/poll/chat_member/...

        if self._is_own_message(message):
            return await super().put(item)  # bot-authored: existing behavior preserved
        if not self._is_authorized_sender(message):
            return await super().put(item)  # unauthorized sender: existing behavior preserved

        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        thread_id = normalize_thread_id(self._thread_id_resolver(message))

        route = self._route_table_provider().lookup(chat_id, thread_id)
        if route is None or route.mode == "drop":
            return await super().put(item)  # no route, or drop: not a capture boundary

        message_id = getattr(message, "message_id", None)
        sender = getattr(message, "from_user", None)
        sender_id = getattr(sender, "id", None)
        if message_id is None or sender_id is None:
            return await super().put(item)  # no human-authored identity to key a row on

        profile = self._profile_provider()
        account_id = self._account_id_provider()
        eid = compute_event_id(profile, account_id, item.update_id)
        payload_bytes = canonicalize_update(item)
        phash = compute_payload_hash(payload_bytes)
        received_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        event_type = classify_event_type(message)

        try:
            self._store.commit_capture(
                event_id=eid,
                platform="telegram",
                account_id=account_id,
                profile=profile,
                update_id=item.update_id,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                sender_id=sender_id,
                event_type=event_type,
                received_at=received_at,
                payload_hash=phash,
                route_mode=route.mode,
                sink=route.sink,
                payload_json=payload_bytes.decode("utf-8"),
            )
        except RouteConflict:
            self._alert_failure(f"capture ingress: conflicting duplicate for {eid}")
            raise
        except CapturePersistenceError:
            self._alert_failure(f"capture ingress: persistence failure for {eid}")
            raise

        if route.mode == "capture_only":
            return  # terminal deny: consumed here, never delegated to the underlying queue

        return await super().put(item)  # route.mode == "agent": commit already succeeded
