"""Profile-local, revocable credentials for resume-scoped linked browsers."""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

DEVICE_COOKIE_TTL_SECONDS = 90 * 24 * 60 * 60


def _path() -> Path:
    home = Path(get_hermes_home())
    home.mkdir(parents=True, exist_ok=True)
    path = home / "linked_devices.sqlite3"
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    return path


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(_path())
    db.execute("""CREATE TABLE IF NOT EXISTS linked_devices (
        id TEXT PRIMARY KEY, credential_hash BLOB NOT NULL, label TEXT NOT NULL,
        session_id TEXT NOT NULL, profile TEXT NOT NULL, user_id TEXT NOT NULL,
        email TEXT NOT NULL, display_name TEXT NOT NULL, org_id TEXT NOT NULL,
        provider TEXT NOT NULL, created_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL,
        revoked_at INTEGER
    )""")
    return db


def _hash(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()


def device_label(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "iphone" in ua: return "iPhone"
    if "ipad" in ua: return "iPad"
    if "android" in ua: return "Android tablet" if "tablet" in ua else "Android phone"
    return "Browser"


def create_or_rotate(*, existing_id: str = "", label: str, session_id: str, profile: str, identity: dict[str, str]) -> tuple[str, str]:
    now = int(time.time()); device_id = existing_id or f"dev_{uuid.uuid4().hex}"
    secret = secrets.token_urlsafe(48)  # 288 bits before URL encoding
    values = (device_id, _hash(secret), label, session_id, profile, identity["user_id"], identity["email"], identity["display_name"], identity["org_id"], identity["provider"], now, now)
    with _db() as db:
        if existing_id:
            db.execute("""UPDATE linked_devices SET credential_hash=?, label=?, session_id=?, profile=?, user_id=?, email=?, display_name=?, org_id=?, provider=?, last_seen_at=?, revoked_at=NULL WHERE id=?""", (values[1], *values[2:10], now, device_id))
            if db.total_changes == 0: existing_id = ""
        if not existing_id:
            db.execute("INSERT INTO linked_devices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)", values)
    return device_id, secret


def authenticate(secret: str) -> dict[str, Any] | None:
    if not secret: return None
    now = int(time.time())
    with _db() as db:
        row = db.execute("SELECT id, credential_hash, label, session_id, profile, user_id, email, display_name, org_id, provider, created_at, last_seen_at, revoked_at FROM linked_devices WHERE revoked_at IS NULL").fetchall()
        candidate = _hash(secret)
        for item in row:
            if secrets.compare_digest(item[1], candidate):
                if now - item[11] > DEVICE_COOKIE_TTL_SECONDS: return None
                db.execute("UPDATE linked_devices SET last_seen_at=? WHERE id=?", (now, item[0]))
                keys = ("id", "credential_hash", "label", "session_id", "profile", "user_id", "email", "display_name", "org_id", "provider", "created_at", "last_seen_at", "revoked_at")
                return dict(zip(keys, item))
    return None


def list_devices() -> list[dict[str, Any]]:
    with _db() as db:
        rows = db.execute("SELECT id,label,created_at,last_seen_at FROM linked_devices WHERE revoked_at IS NULL ORDER BY last_seen_at DESC").fetchall()
    return [dict(zip(("id", "label", "created_at", "last_seen_at"), r)) for r in rows]


def revoke(device_id: str) -> bool:
    with _db() as db:
        db.execute("UPDATE linked_devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (int(time.time()), device_id))
        return db.total_changes == 1
