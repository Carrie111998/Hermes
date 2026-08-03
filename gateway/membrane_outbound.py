"""Queue for Telegram membrane (L2) outbound deliveries from L1 seats.

Membrane L1 seats speak Telegram through a shared L2 router that alone
holds the fleet bot token. Interactive replies flow L2→L1→L2, but cron /
async delivery call ``PlatformAdapter.send()`` on the L1 ``api_server``
adapter — which cannot talk to Telegram Bot API.

This module gives L1 a durable outbox. L2 polls ``GET /v1/membrane/outbound``
(via the existing reverse tunnels) and delivers with the fleet token.

Fail-open for the agent path: enqueue errors are logged, never raise into
cron/send callers unless the caller needs the bool return.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DB_LOCK = threading.Lock()
_MAX_ROWS = 200
_RETENTION_SECONDS = 3 * 24 * 60 * 60

# chat_id shapes produced by tg_router session_id / cron origin
_TELEGRAM_DM_RE = re.compile(r"^(?:telegram:dm:)?(\d+)$", re.I)


def parse_telegram_chat_id(chat_id: Any) -> Optional[str]:
    """Return bare numeric Telegram chat/user id, or None if not a TG DM target."""
    if chat_id is None:
        return None
    s = str(chat_id).strip()
    m = _TELEGRAM_DM_RE.match(s)
    return m.group(1) if m else None


def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    try:
        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(conn, db_label="state.db (membrane_outbound)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS membrane_outbound (
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                thread_id TEXT,
                content TEXT NOT NULL,
                metadata_json TEXT,
                state TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                claimed_at REAL,
                claim_token TEXT,
                last_error TEXT
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_membrane_outbound_state "
            "ON membrane_outbound(state, created_at)"
        )
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def enqueue(
    *,
    chat_id: str,
    content: str,
    thread_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Enqueue a membrane TG delivery. Returns row id or None on failure."""
    tg_chat = parse_telegram_chat_id(chat_id)
    if not tg_chat:
        return None
    text = (content or "").strip()
    if not text:
        return None
    now = time.time()
    row_id = f"mo_{uuid.uuid4().hex[:20]}"
    meta_json = json.dumps(metadata or {}, ensure_ascii=False, default=str)
    try:
        with _DB_LOCK, _transaction() as conn:
            conn.execute(
                """INSERT INTO membrane_outbound
                   (id, chat_id, thread_id, content, metadata_json, state,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    row_id,
                    tg_chat,
                    str(thread_id) if thread_id is not None else None,
                    text,
                    meta_json,
                    now,
                    now,
                ),
            )
            _prune(conn, now)
        logger.info(
            "membrane_outbound enqueued id=%s chat_id=%s chars=%d",
            row_id,
            tg_chat,
            len(text),
        )
        return row_id
    except Exception:
        logger.warning("membrane_outbound enqueue failed", exc_info=True)
        return None


def list_pending(limit: int = 20) -> List[Dict[str, Any]]:
    """Return pending (and stale claimed) rows oldest-first without claiming."""
    limit = max(1, min(int(limit or 20), 100))
    now = time.time()
    stale_before = now - 120.0  # reclaim claims older than 2 min
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT id, chat_id, thread_id, content, metadata_json,
                      state, created_at
               FROM membrane_outbound
               WHERE state = 'pending'
                  OR (state = 'claimed' AND claimed_at IS NOT NULL AND claimed_at < ?)
               ORDER BY created_at ASC
               LIMIT ?""",
            (stale_before, limit),
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            meta = json.loads(r[4] or "{}")
        except json.JSONDecodeError:
            meta = {}
        out.append(
            {
                "id": r[0],
                "chat_id": r[1],
                "thread_id": r[2],
                "content": r[3],
                "metadata": meta,
                "state": r[5],
                "created_at": r[6],
            }
        )
    return out


def claim(ids: List[str], *, claim_token: Optional[str] = None) -> List[str]:
    """Mark rows claimed. Returns ids successfully claimed."""
    if not ids:
        return []
    token = claim_token or uuid.uuid4().hex
    now = time.time()
    claimed: List[str] = []
    with _DB_LOCK, _transaction() as conn:
        for oid in ids:
            cur = conn.execute(
                """UPDATE membrane_outbound
                   SET state='claimed', claimed_at=?, claim_token=?, updated_at=?
                   WHERE id=? AND state IN ('pending', 'claimed')""",
                (now, token, now, oid),
            )
            if cur.rowcount:
                claimed.append(oid)
    return claimed


def ack(ids: List[str], *, ok: bool = True, error: Optional[str] = None) -> int:
    """Mark claimed rows delivered or re-queue on failure."""
    if not ids:
        return 0
    now = time.time()
    n = 0
    with _DB_LOCK, _transaction() as conn:
        for oid in ids:
            if ok:
                cur = conn.execute(
                    """UPDATE membrane_outbound
                       SET state='delivered', updated_at=?, last_error=NULL
                       WHERE id=?""",
                    (now, oid),
                )
            else:
                cur = conn.execute(
                    """UPDATE membrane_outbound
                       SET state='pending', claimed_at=NULL, claim_token=NULL,
                           updated_at=?, last_error=?
                       WHERE id=?""",
                    (now, (error or "delivery_failed")[:500], oid),
                )
            n += cur.rowcount
        _prune(conn, now)
    return n


def _prune(conn: sqlite3.Connection, now: float) -> None:
    cutoff = now - _RETENTION_SECONDS
    conn.execute(
        """DELETE FROM membrane_outbound
           WHERE state='delivered' AND updated_at < ?""",
        (cutoff,),
    )
    total = conn.execute("SELECT COUNT(*) FROM membrane_outbound").fetchone()[0]
    excess = max(0, total - _MAX_ROWS)
    if excess:
        conn.execute(
            """DELETE FROM membrane_outbound WHERE id IN (
                 SELECT id FROM membrane_outbound
                 ORDER BY CASE state
                            WHEN 'delivered' THEN 0
                            ELSE 1
                          END, updated_at ASC
                 LIMIT ?)""",
            (excess,),
        )


def is_membrane_telegram_target(chat_id: Any) -> bool:
    return parse_telegram_chat_id(chat_id) is not None
