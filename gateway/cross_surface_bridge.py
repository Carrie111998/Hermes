"""Local cross-process bridge for Desktop approvals and notifications.

Desktop/TUI and the messaging gateway are separate processes.  This module
provides a profile-local SQLite mailbox that lets the Desktop publish a
redacted approval request, lets the gateway render it on a configured home
channel, and carries an authenticated user's opaque-token decision back to the
original in-memory Desktop approval queue.

The model never receives a messaging capability.  Raw commands and Desktop
session keys never enter the mailbox: callers must pass the already-redacted
approval payload, and the Desktop keeps token -> session ownership in memory.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)

logger = logging.getLogger(__name__)

_DB_NAME = "cross_surface_bridge.sqlite3"
_ALLOWED_CHOICES = frozenset({"once", "session", "always", "deny"})
_DEFAULT_POLL_SECONDS = 0.25
_DEFAULT_NOTIFICATION_TTL = 3600.0
_MAX_TEXT = 4000
_SAFE_NOTIFICATION_TEXTS = frozenset(
    {
        "Desktop background process matched a notification watch.",
        "Desktop background process watch notifications were rate-limited.",
        "Desktop background process watch notifications were disabled.",
        "Desktop background task completed.",
        "Desktop background process status changed.",
    }
)
_local_lock = threading.RLock()
_local_approvals: Dict[str, tuple[str, str]] = {}  # token -> (session key, profile home)
_resolver_thread: Optional[threading.Thread] = None
_resolver_stop = threading.Event()
_resolver_terminal = False


def _settings() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        root = load_config() or {}
    except Exception:
        return {}
    approvals = root.get("approvals") or {}
    cfg = approvals.get("cross_surface") or {}
    return cfg if isinstance(cfg, dict) else {}


def enabled() -> bool:
    return bool(_settings().get("enabled", False))


def target_platform() -> str:
    value = str(_settings().get("target", "telegram") or "telegram").strip().lower()
    return value or "telegram"


def process_notifications_enabled() -> bool:
    cfg = _settings()
    return bool(cfg.get("enabled", False) and cfg.get("process_notifications", True))


def _db_path() -> Path:
    root = Path(get_hermes_home()) / "runtime"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root / _DB_NAME


def _connect() -> sqlite3.Connection:
    path = _db_path()
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="cross-surface bridge")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bridge_events (
            token TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('approval', 'notification')),
            target TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN
                ('pending', 'claimed', 'delivered', 'resolved', 'expired')),
            claim_owner TEXT,
            claim_until REAL,
            message_id TEXT,
            destination_chat_id TEXT,
            destination_thread_id TEXT,
            destination_user_id TEXT,
            decision TEXT,
            resolved_at REAL,
            dedupe_key TEXT
        )
        """
    )
    existing_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(bridge_events)").fetchall()
    }
    for column in (
        "destination_chat_id",
        "destination_thread_id",
        "destination_user_id",
    ):
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE bridge_events ADD COLUMN {column} TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS bridge_events_dedupe "
        "ON bridge_events(dedupe_key) WHERE dedupe_key IS NOT NULL"
    )
    conn.commit()
    for private_path in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            if private_path.exists():
                os.chmod(private_path, 0o600)
        except OSError:
            pass
    return conn


def _bounded_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for key in ("command", "description"):
        value = payload.get(key)
        if value is not None:
            clean[key] = str(value)[:_MAX_TEXT]
    for key in ("allow_permanent", "allow_session", "smart_denied"):
        if key in payload:
            clean[key] = bool(payload.get(key))
    choices = payload.get("choices")
    if isinstance(choices, list):
        clean["choices"] = [str(x) for x in choices if str(x) in _ALLOWED_CHOICES]
    return clean


def _payload_allowed_choices(payload: Dict[str, Any]) -> set[str]:
    explicit = payload.get("choices")
    if isinstance(explicit, list):
        return {str(choice) for choice in explicit} & set(_ALLOWED_CHOICES)
    if payload.get("smart_denied") or payload.get("allow_session") is False:
        return {"once", "deny"}
    if payload.get("allow_permanent") is False:
        return {"once", "session", "deny"}
    return set(_ALLOWED_CHOICES)


def publish_approval(session_key: str, payload: Dict[str, Any]) -> Optional[str]:
    """Publish a redacted Desktop approval and register local ownership.

    ``request_id`` is generated by ``tools.approval`` for the exact in-memory
    queue entry. Reusing it here makes remote resolution target-specific under
    parallel tool calls instead of falling back to FIFO session resolution.
    """
    if not session_key or not enabled():
        return None
    now = time.time()
    token = str((payload or {}).get("request_id") or "")
    if len(token) != 43 or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for ch in token):
        return None
    try:
        raw_expires_at = (payload or {}).get("expires_at")
        if raw_expires_at is None:
            return None
        expires_at = float(raw_expires_at)
    except (TypeError, ValueError):
        return None
    if expires_at <= now:
        return None
    body = _bounded_payload(payload or {})
    with _local_lock:
        if _resolver_terminal:
            return None
        with _connect() as conn:
            conn.execute(
                "INSERT INTO bridge_events "
                "(token, kind, target, payload_json, created_at, expires_at, status) "
                "VALUES (?, 'approval', ?, ?, ?, ?, 'pending')",
                (
                    token,
                    target_platform(),
                    json.dumps(body, separators=(",", ":")),
                    now,
                    expires_at,
                ),
            )
        _local_approvals[token] = (session_key, str(get_hermes_home()))
        _ensure_resolver_thread()
    logger.info("Published cross-surface approval token=%s… target=%s", token[:8], target_platform())
    return token


def publish_notification(text: str, *, dedupe_key: Optional[str] = None) -> Optional[str]:
    """Publish a one-way Desktop process notification to the home channel."""
    if not text or not process_notifications_enabled():
        return None
    text = str(text)
    if text not in _SAFE_NOTIFICATION_TEXTS and not re.fullmatch(
        r"Desktop background process completed(?: \(exit code -?\d+\))?\.", text
    ):
        logger.warning("Rejected non-generic cross-surface process notification")
        return None
    now = time.time()
    token = secrets.token_urlsafe(24)
    body = {"text": text[:_MAX_TEXT]}
    with _local_lock:
        if _resolver_terminal:
            return None
        try:
            with _connect() as conn:
                conn.execute(
                    "INSERT INTO bridge_events "
                    "(token, kind, target, payload_json, created_at, expires_at, status, dedupe_key) "
                    "VALUES (?, 'notification', ?, ?, ?, ?, 'pending', ?)",
                    (
                        token,
                        target_platform(),
                        json.dumps(body, separators=(",", ":")),
                        now,
                        now + _DEFAULT_NOTIFICATION_TTL,
                        str(dedupe_key)[:500] if dedupe_key else None,
                    ),
                )
        except sqlite3.IntegrityError:
            return None
    return token


def claim_events(target: str, owner: str, *, limit: int = 20, lease_seconds: float = 30.0) -> list[Dict[str, Any]]:
    """Atomically lease pending/abandoned events for one gateway watcher."""
    now = time.time()
    claimed: list[Dict[str, Any]] = []
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE bridge_events SET status='expired', claim_owner=NULL, claim_until=NULL "
            "WHERE expires_at <= ? AND status IN ('pending','claimed','delivered')",
            (now,),
        )
        rows = conn.execute(
            "SELECT token, kind, payload_json, expires_at FROM bridge_events "
            "WHERE target=? AND expires_at>? AND "
            "(status='pending' OR (status='claimed' AND claim_until<?)) "
            "ORDER BY created_at LIMIT ?",
            (target, now, now, max(1, min(int(limit), 100))),
        ).fetchall()
        for row in rows:
            updated = conn.execute(
                "UPDATE bridge_events SET status='claimed', claim_owner=?, claim_until=? "
                "WHERE token=? AND (status='pending' OR (status='claimed' AND claim_until<?))",
                (owner, now + lease_seconds, row["token"], now),
            ).rowcount
            if updated:
                try:
                    payload = json.loads(row["payload_json"])
                except Exception:
                    payload = {}
                claimed.append(
                    {
                        "token": row["token"],
                        "kind": row["kind"],
                        "payload": payload if isinstance(payload, dict) else {},
                        "expires_at": float(row["expires_at"]),
                    }
                )
        conn.commit()
    return claimed


def mark_delivered(
    token: str,
    owner: str,
    message_id: Optional[str] = None,
    *,
    chat_id: str,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> bool:
    with _connect() as conn:
        return bool(
            conn.execute(
                "UPDATE bridge_events SET status='delivered', message_id=?, "
                "destination_chat_id=?, destination_thread_id=?, destination_user_id=?, "
                "claim_owner=NULL, claim_until=NULL "
                "WHERE token=? AND status='claimed' AND claim_owner=?",
                (
                    str(message_id or ""),
                    str(chat_id),
                    str(thread_id) if thread_id is not None else None,
                    str(user_id) if user_id is not None else None,
                    token,
                    owner,
                ),
            ).rowcount
        )


def release_claim(token: str, owner: str) -> bool:
    with _connect() as conn:
        return bool(
            conn.execute(
                "UPDATE bridge_events SET status='pending', claim_owner=NULL, claim_until=NULL "
                "WHERE token=? AND status='claimed' AND claim_owner=?",
                (token, owner),
            ).rowcount
        )


def resolve_request(
    token: str,
    choice: str,
    *,
    chat_id: str,
    thread_id: Optional[str],
    user_id: str,
    message_id: Optional[str] = None,
) -> bool:
    """Record one authorized, unexpired, single-use Telegram decision."""
    if choice not in _ALLOWED_CHOICES or not token:
        return False
    now = time.time()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT kind, status, expires_at, decision, payload_json, "
            "destination_chat_id, destination_thread_id, destination_user_id, message_id "
            "FROM bridge_events WHERE token=?",
            (token,),
        ).fetchone()
        try:
            stored_payload = json.loads(row["payload_json"]) if row else None
        except Exception:
            stored_payload = None
        if (
            not row
            or row["kind"] != "approval"
            or row["decision"] is not None
            or row["status"] not in {"claimed", "delivered"}
            or float(row["expires_at"]) <= now
            or not isinstance(stored_payload, dict)
            or choice not in _payload_allowed_choices(stored_payload)
            or not row["destination_chat_id"]
            or str(row["destination_chat_id"]) != str(chat_id)
            or (
                (str(row["destination_thread_id"]) if row["destination_thread_id"] is not None else None)
                != (str(thread_id) if thread_id is not None else None)
            )
            or (
                row["destination_user_id"] is not None
                and str(row["destination_user_id"]) != str(user_id)
            )
            or (
                str(row["message_id"] or "")
                and str(row["message_id"]) != str(message_id or "")
            )
        ):
            if row and float(row["expires_at"]) <= now and row["status"] != "resolved":
                conn.execute("UPDATE bridge_events SET status='expired' WHERE token=?", (token,))
            conn.commit()
            return False
        updated = conn.execute(
            "UPDATE bridge_events SET decision=?, resolved_at=? "
            "WHERE token=? AND decision IS NULL",
            (choice, now, token),
        ).rowcount
        conn.commit()
        return bool(updated)


def _decision(token: str) -> tuple[Optional[str], Optional[str]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT status, decision FROM bridge_events WHERE token=?", (token,)
        ).fetchone()
    if not row:
        return None, None
    return str(row["status"]), (str(row["decision"]) if row["decision"] else None)


def wait_for_resolution(token: str, timeout: float = 3.0) -> str:
    """Wait briefly for the Desktop authority to acknowledge a decision.

    Returns ``resolved``, ``expired``, or ``pending``. Telegram must only claim
    approval after this returns ``resolved``; accepting a DB decision alone is
    not proof that the original in-memory gate was signalled.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        status, _choice = _decision(token)
        if status in {"resolved", "expired"}:
            return status
        if time.monotonic() >= deadline:
            return "pending"
        time.sleep(0.05)


def _finish_local(token: str, status: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE bridge_events SET status=?, claim_owner=NULL, claim_until=NULL "
            "WHERE token=? AND status!='resolved'",
            (status, token),
        )
    with _local_lock:
        _local_approvals.pop(token, None)


def _resolve_local_decisions_once() -> int:
    """Apply available mailbox decisions to this process's Desktop queues."""
    from tools.approval import has_blocking_approval, resolve_gateway_approval_by_id

    resolved = 0
    with _local_lock:
        items = list(_local_approvals.items())
    for token, (session_key, profile_home) in items:
        home_scope = set_hermes_home_override(profile_home)
        try:
            status, decision = _decision(token)
            # Linearize application against shutdown. stop_local_resolver() uses
            # this same lock to set the stop flag and clear ownership, so a row
            # snapshotted before shutdown cannot be applied afterward.
            with _local_lock:
                if (
                    _resolver_stop.is_set()
                    or _local_approvals.get(token) != (session_key, profile_home)
                ):
                    continue
                if decision and status == "delivered":
                    count = resolve_gateway_approval_by_id(session_key, token, decision)
                    with _connect() as conn:
                        conn.execute(
                            "UPDATE bridge_events SET status=? WHERE token=?",
                            ("resolved" if count else "expired", token),
                        )
                    _local_approvals.pop(token, None)
                    resolved += count
                elif status in {None, "expired", "resolved"} or not has_blocking_approval(
                    session_key
                ):
                    _finish_local(token, "expired")
        except Exception:
            logger.warning("Cross-surface approval resolver iteration failed", exc_info=True)
        finally:
            reset_hermes_home_override(home_scope)
    return resolved


def _resolver_loop() -> None:
    while not _resolver_stop.wait(_DEFAULT_POLL_SECONDS):
        _resolve_local_decisions_once()


def _ensure_resolver_thread() -> None:
    global _resolver_thread
    with _local_lock:
        if _resolver_terminal:
            return
        if _resolver_thread is not None and _resolver_thread.is_alive():
            return
        _resolver_stop.clear()
        _resolver_thread = threading.Thread(
            target=_resolver_loop,
            name="hermes-cross-surface-approval-resolver",
            daemon=True,
        )
        _resolver_thread.start()


def stop_local_resolver() -> None:
    """Stop the Desktop resolver and fail closed all process-owned bridge rows."""
    global _resolver_terminal, _resolver_thread
    with _local_lock:
        _resolver_terminal = True
        _resolver_stop.set()
        tokens = list(_local_approvals)
        _local_approvals.clear()
        thread = _resolver_thread
        _resolver_thread = None
    if tokens:
        try:
            with _connect() as conn:
                placeholders = ",".join("?" for _ in tokens)
                conn.execute(
                    f"UPDATE bridge_events SET status='expired' "
                    f"WHERE token IN ({placeholders}) AND status!='resolved'",
                    tokens,
                )
        except Exception:
            logger.warning("Could not expire cross-surface approvals on shutdown", exc_info=True)
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=1.0)


def cleanup_old_events(*, older_than_seconds: float = 86400.0) -> int:
    cutoff = time.time() - max(60.0, older_than_seconds)
    with _connect() as conn:
        return conn.execute(
            "DELETE FROM bridge_events WHERE created_at<? AND status IN ('resolved','expired','delivered')",
            (cutoff,),
        ).rowcount
