"""SQLite persistence for rooms, messages, consultations and drafts.

One connection guarded by a lock. Every public method is synchronous and
cheap; callers on the event loop wrap them in ``asyncio.to_thread`` (see
``store.py``) so a slow disk never blocks the webhook's 5-second budget.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 4

ROOM_FLAGS = frozenset({"lawyer_takeover", "muted", "intro_sent", "first_alerts_done"})

# Columns added after v1. `CREATE TABLE IF NOT EXISTS` is a no-op on an
# existing database, so a new column has to be added explicitly or a
# deployed bot breaks on its next query.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("rooms", "first_alerts_done", "INTEGER NOT NULL DEFAULT 0"),
    ("drafts", "instructions", "TEXT NOT NULL DEFAULT ''"),
    ("drafts", "transcript", "TEXT NOT NULL DEFAULT ''"),
    ("drafts", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("drafts", "last_error", "TEXT NOT NULL DEFAULT ''"),
    ("drafts", "claimed_at", "REAL"),
    ("intakes", "track", "TEXT NOT NULL DEFAULT 'civil'"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    room_id           TEXT PRIMARY KEY,
    room_name         TEXT NOT NULL DEFAULT '',
    kind              TEXT NOT NULL DEFAULT 'unknown',   -- direct | group | lawyer | unknown
    consult_id        INTEGER,
    lawyer_takeover   INTEGER NOT NULL DEFAULT 0,
    muted             INTEGER NOT NULL DEFAULT 0,
    intro_sent        INTEGER NOT NULL DEFAULT 0,
    first_alerts_done INTEGER NOT NULL DEFAULT 0,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     TEXT NOT NULL,
    sender      TEXT NOT NULL DEFAULT '',
    sender_key  TEXT NOT NULL DEFAULT '',
    role        TEXT NOT NULL,                            -- user | bot | lawyer | system
    text        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_room ON messages(room_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(created_at);

CREATE TABLE IF NOT EXISTS consultations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id       TEXT NOT NULL,
    client_alias  TEXT NOT NULL DEFAULT '',
    client_email  TEXT NOT NULL DEFAULT '',
    topic         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'open',           -- open | awaiting_lawyer | closed
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_consult_room ON consultations(room_id);

CREATE TABLE IF NOT EXISTS drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    consult_id   INTEGER,
    room_id      TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'general',
    title        TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL DEFAULT '',
    -- pending_generation | generating | pending_review | approved | sent
    -- | rejected | generation_failed
    status       TEXT NOT NULL DEFAULT 'pending_review',
    lawyer_note  TEXT NOT NULL DEFAULT '',
    client_email TEXT NOT NULL DEFAULT '',
    -- Everything the Codex worker needs to write the document. Kept on the
    -- row so a job survives a server restart between request and pickup.
    instructions TEXT NOT NULL DEFAULT '',
    transcript   TEXT NOT NULL DEFAULT '',
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT NOT NULL DEFAULT '',
    claimed_at   REAL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    sent_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status, id DESC);

CREATE TABLE IF NOT EXISTS intakes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id      TEXT NOT NULL,
    consult_id   INTEGER,
    doc_kind     TEXT NOT NULL DEFAULT '',
    case_type    TEXT NOT NULL DEFAULT '',
    -- civil | criminal — 민사는 요건사실로, 형사는 구성요건으로 묻는다
    track        TEXT NOT NULL DEFAULT 'civil',
    -- form_sent | collecting | report_review | quoted | confirmed | cancelled
    status       TEXT NOT NULL DEFAULT 'form_sent',
    report       TEXT NOT NULL DEFAULT '',
    missing      TEXT NOT NULL DEFAULT '',
    draft_id     INTEGER,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intakes_room ON intakes(room_id, id DESC);

CREATE TABLE IF NOT EXISTS answers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id      TEXT NOT NULL,
    sender_key   TEXT NOT NULL DEFAULT '',
    question     TEXT NOT NULL,
    answer       TEXT NOT NULL,
    citations    TEXT NOT NULL DEFAULT '[]',
    tools_used   TEXT NOT NULL DEFAULT '[]',
    latency_ms   INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_answers_room ON answers(room_id, id DESC);

CREATE TABLE IF NOT EXISTS outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id      TEXT NOT NULL,
    text         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued',          -- queued | claimed | delivered | failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    claimed_at   REAL,
    delivered_at REAL
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, id);

CREATE TABLE IF NOT EXISTS http_cache (
    cache_key   TEXT PRIMARY KEY,
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_events (
    event_key   TEXT PRIMARY KEY,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
"""


@dataclass(frozen=True)
class Message:
    role: str
    sender: str
    text: str
    created_at: float


@dataclass(frozen=True)
class Draft:
    id: int
    consult_id: int | None
    room_id: str
    kind: str
    title: str
    body: str
    status: str
    lawyer_note: str
    client_email: str
    created_at: float
    updated_at: float
    sent_at: float | None
    instructions: str = ""
    transcript: str = ""
    attempts: int = 0
    last_error: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Draft:
        keys = row.keys()
        return cls(
            id=row["id"],
            consult_id=row["consult_id"],
            room_id=row["room_id"],
            kind=row["kind"],
            title=row["title"],
            body=row["body"],
            status=row["status"],
            lawyer_note=row["lawyer_note"],
            client_email=row["client_email"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            sent_at=row["sent_at"],
            instructions=row["instructions"] if "instructions" in keys else "",
            transcript=row["transcript"] if "transcript" in keys else "",
            attempts=row["attempts"] if "attempts" in keys else 0,
            last_error=row["last_error"] if "last_error" in keys else "",
        )

    @property
    def awaiting_worker(self) -> bool:
        return self.status in {"pending_generation", "generating"}


def pseudonymise(value: str, salt: str) -> str:
    """Stable, non-reversible id for a Kakao sender.

    The audit log needs to correlate turns from the same person without
    keeping their display name around. Truncated to 16 hex chars — plenty
    for correlation, useless as an identifier on its own.
    """
    if not value:
        return ""
    digest = hashlib.sha256(f"{salt}\x00{value}".encode())
    return digest.hexdigest()[:16]


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._migrate_locked()
            self._conn.execute(
                "INSERT OR REPLACE INTO kv(key, value, updated_at) VALUES('schema_version', ?, ?)",
                (str(SCHEMA_VERSION), time.time()),
            )
            self._conn.commit()

    def _migrate_locked(self) -> None:
        """Add columns introduced after the database was first created."""
        for table, column, spec in _ADDED_COLUMNS:
            existing = {
                str(row["name"])
                for row in self._conn.execute(f"PRAGMA table_info({table})")  # noqa: S608
            }
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {spec}"  # noqa: S608
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── low level ────────────────────────────────────────────────────────
    def _exec(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ── rooms ────────────────────────────────────────────────────────────
    def upsert_room(self, room_id: str, room_name: str = "", kind: str = "") -> sqlite3.Row:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO rooms(room_id, room_name, kind, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(room_id) DO UPDATE SET
                    room_name = CASE WHEN excluded.room_name != '' THEN excluded.room_name
                                     ELSE rooms.room_name END,
                    kind      = CASE WHEN excluded.kind NOT IN ('', 'unknown') THEN excluded.kind
                                     ELSE rooms.kind END,
                    updated_at = excluded.updated_at
                """,
                (room_id, room_name, kind or "unknown", now, now),
            )
            self._conn.commit()
        row = self._query_one("SELECT * FROM rooms WHERE room_id = ?", (room_id,))
        assert row is not None
        return row

    def get_room(self, room_id: str) -> sqlite3.Row | None:
        return self._query_one("SELECT * FROM rooms WHERE room_id = ?", (room_id,))

    def set_room_flag(self, room_id: str, field: str, value: int) -> None:
        if field not in ROOM_FLAGS:
            raise ValueError(f"not a room flag: {field}")
        self._exec(
            f"UPDATE rooms SET {field} = ?, updated_at = ? WHERE room_id = ?",  # noqa: S608
            (int(value), time.time(), room_id),
        )

    def list_rooms(self, limit: int = 100) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM rooms ORDER BY updated_at DESC LIMIT ?", (limit,))

    # ── messages ─────────────────────────────────────────────────────────
    def add_message(
        self,
        room_id: str,
        role: str,
        text: str,
        sender: str = "",
        sender_key: str = "",
        keep_last: int = 24,
    ) -> int:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO messages(room_id, sender, sender_key, role, text, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (room_id, sender, sender_key, role, text, now),
            )
            # Keep only the most recent `keep_last` rows for this room. The
            # bot never needs older context and this is the only thing
            # stopping the volume from filling up with other people's
            # personal facts.
            if keep_last > 0:
                self._conn.execute(
                    """
                    DELETE FROM messages
                     WHERE room_id = ?
                       AND id NOT IN (
                           SELECT id FROM messages WHERE room_id = ?
                            ORDER BY id DESC LIMIT ?
                       )
                    """,
                    (room_id, room_id, keep_last),
                )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def recent_messages(self, room_id: str, limit: int = 24) -> list[Message]:
        rows = self._query(
            "SELECT role, sender, text, created_at FROM messages "
            "WHERE room_id = ? ORDER BY id DESC LIMIT ?",
            (room_id, limit),
        )
        return [
            Message(
                role=row["role"], sender=row["sender"], text=row["text"], created_at=row["created_at"]
            )
            for row in reversed(rows)
        ]

    def purge_old_messages(self, older_than_days: int) -> int:
        if older_than_days <= 0:
            return 0
        cutoff = time.time() - older_than_days * 86400
        cur = self._exec("DELETE FROM messages WHERE created_at < ?", (cutoff,))
        return cur.rowcount or 0

    # ── consultations ────────────────────────────────────────────────────
    def get_or_create_consultation(self, room_id: str, client_alias: str = "") -> sqlite3.Row:
        row = self._query_one(
            "SELECT * FROM consultations WHERE room_id = ? AND status != 'closed' "
            "ORDER BY id DESC LIMIT 1",
            (room_id,),
        )
        if row is not None:
            return row
        now = time.time()
        cur = self._exec(
            "INSERT INTO consultations(room_id, client_alias, created_at, updated_at) "
            "VALUES(?, ?, ?, ?)",
            (room_id, client_alias, now, now),
        )
        self._exec(
            "UPDATE rooms SET consult_id = ?, updated_at = ? WHERE room_id = ?",
            (cur.lastrowid, now, room_id),
        )
        found = self._query_one("SELECT * FROM consultations WHERE id = ?", (cur.lastrowid,))
        assert found is not None
        return found

    def update_consultation(self, consult_id: int, **fields: Any) -> None:
        allowed = {"client_alias", "client_email", "topic", "status"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        assignments = ", ".join(f"{k} = ?" for k in sets)
        self._exec(
            f"UPDATE consultations SET {assignments}, updated_at = ? WHERE id = ?",  # noqa: S608
            (*sets.values(), time.time(), consult_id),
        )

    def get_consultation(self, consult_id: int) -> sqlite3.Row | None:
        return self._query_one("SELECT * FROM consultations WHERE id = ?", (consult_id,))

    # ── drafts ───────────────────────────────────────────────────────────
    def create_draft(
        self,
        room_id: str,
        kind: str,
        title: str,
        body: str,
        consult_id: int | None = None,
        client_email: str = "",
        status: str = "pending_review",
        instructions: str = "",
        transcript: str = "",
    ) -> int:
        now = time.time()
        cur = self._exec(
            """
            INSERT INTO drafts(consult_id, room_id, kind, title, body, client_email,
                               status, instructions, transcript, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                consult_id,
                room_id,
                kind,
                title,
                body,
                client_email,
                status,
                instructions,
                transcript,
                now,
                now,
            ),
        )
        return int(cur.lastrowid or 0)

    # ── document intake ──────────────────────────────────────────────────
    def open_intake(
        self,
        room_id: str,
        doc_kind: str,
        case_type: str = "",
        consult_id: int | None = None,
        track: str = "",
    ) -> sqlite3.Row:
        """Start an intake, or return the one already running in this room.

        One room can only be collecting facts for one document at a time —
        two interleaved questionnaires would produce two half-filled reports.
        """
        existing = self.active_intake(room_id)
        if existing is not None:
            self._exec(
                "UPDATE intakes SET doc_kind = CASE WHEN ? != '' THEN ? ELSE doc_kind END, "
                "case_type = CASE WHEN ? != '' THEN ? ELSE case_type END, "
                "track = CASE WHEN ? != '' THEN ? ELSE track END, updated_at = ? "
                "WHERE id = ?",
                (
                    doc_kind,
                    doc_kind,
                    case_type,
                    case_type,
                    track,
                    track,
                    time.time(),
                    existing["id"],
                ),
            )
            found = self._query_one("SELECT * FROM intakes WHERE id = ?", (existing["id"],))
            assert found is not None
            return found

        now = time.time()
        cur = self._exec(
            "INSERT INTO intakes(room_id, consult_id, doc_kind, case_type, track, status, "
            "created_at, updated_at) VALUES(?, ?, ?, ?, ?, 'form_sent', ?, ?)",
            (room_id, consult_id, doc_kind, case_type, track or "civil", now, now),
        )
        found = self._query_one("SELECT * FROM intakes WHERE id = ?", (cur.lastrowid,))
        assert found is not None
        return found

    def active_intake(self, room_id: str) -> sqlite3.Row | None:
        return self._query_one(
            "SELECT * FROM intakes WHERE room_id = ? "
            "AND status IN ('form_sent','collecting','report_review','quoted') "
            "ORDER BY id DESC LIMIT 1",
            (room_id,),
        )

    def get_intake(self, intake_id: int) -> sqlite3.Row | None:
        return self._query_one("SELECT * FROM intakes WHERE id = ?", (intake_id,))

    def update_intake(self, intake_id: int, **fields: Any) -> None:
        allowed = {"doc_kind", "case_type", "status", "report", "missing", "draft_id"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        assignments = ", ".join(f"{k} = ?" for k in sets)
        self._exec(
            f"UPDATE intakes SET {assignments}, updated_at = ? WHERE id = ?",  # noqa: S608
            (*sets.values(), time.time(), intake_id),
        )

    def list_intakes(self, status: str = "", limit: int = 50) -> list[sqlite3.Row]:
        if status:
            return self._query(
                "SELECT * FROM intakes WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
            )
        return self._query("SELECT * FROM intakes ORDER BY id DESC LIMIT ?", (limit,))

    # ── draft generation queue (Codex worker on the lawyer's PC) ─────────
    def claim_draft_jobs(self, limit: int = 1) -> list[Draft]:
        """Hand pending jobs to the worker and mark them in progress.

        Claimed rather than merely listed, so two workers (or a worker that
        was restarted mid-poll) cannot generate the same document twice.
        """
        now = time.time()
        with self._lock:
            rows = list(
                self._conn.execute(
                    "SELECT * FROM drafts WHERE status = 'pending_generation' "
                    "ORDER BY id LIMIT ?",
                    (limit,),
                ).fetchall()
            )
            if rows:
                ids = [row["id"] for row in rows]
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"UPDATE drafts SET status='generating', claimed_at=?, "  # noqa: S608
                    f"attempts=attempts+1, updated_at=? WHERE id IN ({placeholders})",
                    (now, now, *ids),
                )
                self._conn.commit()
        return [Draft.from_row(row) for row in rows]

    def complete_draft_generation(self, draft_id: int, body: str, title: str = "") -> bool:
        """Worker delivered a document — move it into the review queue."""
        fields: list[Any] = [body]
        assignments = "body = ?"
        if title:
            assignments += ", title = ?"
            fields.append(title)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE drafts SET {assignments}, status='pending_review', "  # noqa: S608
                "claimed_at=NULL, last_error='', updated_at=? "
                "WHERE id = ? AND status IN ('generating', 'pending_generation')",
                (*fields, time.time(), draft_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def fail_draft_generation(self, draft_id: int, error: str, max_attempts: int = 3) -> str:
        """Worker could not produce a document.

        Retried until ``max_attempts``, then parked as ``generation_failed``
        so the lawyer is told rather than the request vanishing.
        """
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                return ""
            status = "pending_generation" if row["attempts"] < max_attempts else "generation_failed"
            self._conn.execute(
                "UPDATE drafts SET status=?, claimed_at=NULL, last_error=?, updated_at=? "
                "WHERE id = ? AND status IN ('generating', 'pending_generation')",
                (status, error[:500], now, draft_id),
            )
            self._conn.commit()
            return status

    def requeue_stale_draft_jobs(self, older_than_s: float = 1800.0) -> int:
        """A worker that died mid-job must not strand the request forever."""
        cutoff = time.time() - older_than_s
        cur = self._exec(
            "UPDATE drafts SET status='pending_generation', claimed_at=NULL, updated_at=? "
            "WHERE status='generating' AND claimed_at IS NOT NULL AND claimed_at < ?",
            (time.time(), cutoff),
        )
        return cur.rowcount or 0

    def draft_queue_depth(self) -> int:
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM drafts WHERE status IN ('pending_generation','generating')"
        )
        return int(row["n"]) if row else 0

    def get_draft(self, draft_id: int) -> Draft | None:
        row = self._query_one("SELECT * FROM drafts WHERE id = ?", (draft_id,))
        return Draft.from_row(row) if row else None

    def list_drafts(self, status: str = "", limit: int = 50) -> list[Draft]:
        if status:
            rows = self._query(
                "SELECT * FROM drafts WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
            )
        else:
            rows = self._query("SELECT * FROM drafts ORDER BY id DESC LIMIT ?", (limit,))
        return [Draft.from_row(row) for row in rows]

    def update_draft(self, draft_id: int, **fields: Any) -> None:
        allowed = {
            "title",
            "body",
            "status",
            "lawyer_note",
            "client_email",
            "sent_at",
            "instructions",
        }
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        assignments = ", ".join(f"{k} = ?" for k in sets)
        self._exec(
            f"UPDATE drafts SET {assignments}, updated_at = ? WHERE id = ?",  # noqa: S608
            (*sets.values(), time.time(), draft_id),
        )

    # ── answer audit log ─────────────────────────────────────────────────
    def log_answer(
        self,
        room_id: str,
        question: str,
        answer: str,
        sender_key: str = "",
        citations: Iterable[str] = (),
        tools_used: Iterable[str] = (),
        latency_ms: int = 0,
    ) -> None:
        self._exec(
            """
            INSERT INTO answers(room_id, sender_key, question, answer, citations,
                                tools_used, latency_ms, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                room_id,
                sender_key,
                question,
                answer,
                json.dumps(list(citations), ensure_ascii=False),
                json.dumps(list(tools_used), ensure_ascii=False),
                latency_ms,
                time.time(),
            ),
        )

    # ── dashboard ────────────────────────────────────────────────────────
    def dashboard_snapshot(self, since: float, room_limit: int = 25) -> dict[str, Any]:
        """One read for the whole 업무 대시보드.

        The lawyer opens this on a phone between hearings, so it is one
        query pass and one page — not five endpoints to poke at.
        """

        def count(sql: str, params: Sequence[Any] = ()) -> int:
            row = self._query_one(sql, params)
            return int(row["n"]) if row else 0

        drafts_by_status = {
            str(row["status"]): int(row["n"])
            for row in self._query("SELECT status, COUNT(*) AS n FROM drafts GROUP BY status")
        }
        rooms = self._query(
            "SELECT r.*, "
            "  (SELECT text FROM messages m WHERE m.room_id = r.room_id "
            "    ORDER BY m.id DESC LIMIT 1) AS last_text, "
            "  (SELECT role FROM messages m WHERE m.room_id = r.room_id "
            "    ORDER BY m.id DESC LIMIT 1) AS last_role "
            "FROM rooms r ORDER BY r.updated_at DESC LIMIT ?",
            (room_limit,),
        )
        return {
            "today": {
                "questions": count(
                    "SELECT COUNT(*) AS n FROM messages WHERE role = 'user' AND created_at >= ?",
                    (since,),
                ),
                "answers": count(
                    "SELECT COUNT(*) AS n FROM answers WHERE created_at >= ?", (since,)
                ),
                "new_rooms": count(
                    "SELECT COUNT(*) AS n FROM rooms WHERE created_at >= ?", (since,)
                ),
                "new_drafts": count(
                    "SELECT COUNT(*) AS n FROM drafts WHERE created_at >= ?", (since,)
                ),
                "slow_answers": count(
                    "SELECT COUNT(*) AS n FROM answers WHERE created_at >= ? AND latency_ms >= 90000",
                    (since,),
                ),
            },
            "drafts_by_status": drafts_by_status,
            "waiting_review": self.list_drafts("pending_review", 20),
            "waiting_generation": [
                *self.list_drafts("pending_generation", 20),
                *self.list_drafts("generating", 20),
            ],
            "failed": self.list_drafts("generation_failed", 10),
            "approved": self.list_drafts("approved", 20),
            "intakes": self._query(
                "SELECT * FROM intakes WHERE status IN "
                "('form_sent','collecting','report_review','quoted') "
                "ORDER BY updated_at DESC LIMIT 20"
            ),
            "escalated": self._query(
                "SELECT * FROM consultations WHERE status = 'awaiting_lawyer' "
                "ORDER BY updated_at DESC LIMIT 20"
            ),
            "rooms": rooms,
            "outbox_depth": self.outbox_depth(),
        }

    def count_answers_since(self, room_id: str, since: float) -> int:
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM answers WHERE room_id = ? AND created_at >= ?",
            (room_id, since),
        )
        return int(row["n"]) if row else 0

    # ── outbox (poll delivery mode) ──────────────────────────────────────
    def enqueue_outbox(self, room_id: str, text: str) -> int:
        cur = self._exec(
            "INSERT INTO outbox(room_id, text, created_at) VALUES(?, ?, ?)",
            (room_id, text, time.time()),
        )
        return int(cur.lastrowid or 0)

    def claim_outbox(self, limit: int = 10) -> list[sqlite3.Row]:
        now = time.time()
        with self._lock:
            rows = list(
                self._conn.execute(
                    "SELECT * FROM outbox WHERE status = 'queued' ORDER BY id LIMIT ?", (limit,)
                ).fetchall()
            )
            if rows:
                ids = [row["id"] for row in rows]
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"UPDATE outbox SET status='claimed', claimed_at=?, attempts=attempts+1 "  # noqa: S608
                    f"WHERE id IN ({placeholders})",
                    (now, *ids),
                )
                self._conn.commit()
        return rows

    def ack_outbox(self, ids: Sequence[int], ok: bool = True, error: str = "") -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        status = "delivered" if ok else "queued"
        self._exec(
            f"UPDATE outbox SET status=?, delivered_at=?, last_error=? "  # noqa: S608
            f"WHERE id IN ({placeholders})",
            (status, time.time() if ok else None, error, *ids),
        )

    def requeue_stale_outbox(self, older_than_s: float = 120.0) -> int:
        cutoff = time.time() - older_than_s
        cur = self._exec(
            "UPDATE outbox SET status='queued' WHERE status='claimed' AND claimed_at < ?",
            (cutoff,),
        )
        return cur.rowcount or 0

    def outbox_depth(self) -> int:
        row = self._query_one("SELECT COUNT(*) AS n FROM outbox WHERE status='queued'")
        return int(row["n"]) if row else 0

    # ── dedupe + cache ───────────────────────────────────────────────────
    def mark_seen(self, event_key: str, ttl_s: float = 3600.0) -> bool:
        """Return True the first time an event key is seen, False after."""
        if not event_key:
            return True
        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM seen_events WHERE created_at < ?", (now - ttl_s,))
            try:
                self._conn.execute(
                    "INSERT INTO seen_events(event_key, created_at) VALUES(?, ?)", (event_key, now)
                )
            except sqlite3.IntegrityError:
                self._conn.commit()
                return False
            self._conn.commit()
            return True

    def cache_get(self, key: str, ttl_s: int) -> str | None:
        row = self._query_one("SELECT body, created_at FROM http_cache WHERE cache_key = ?", (key,))
        if row is None:
            return None
        if ttl_s > 0 and time.time() - row["created_at"] > ttl_s:
            return None
        return str(row["body"])

    def cache_put(self, key: str, body: str) -> None:
        self._exec(
            "INSERT OR REPLACE INTO http_cache(cache_key, body, created_at) VALUES(?, ?, ?)",
            (key, body, time.time()),
        )

    # ── kv ───────────────────────────────────────────────────────────────
    def kv_get(self, key: str, default: str = "") -> str:
        row = self._query_one("SELECT value FROM kv WHERE key = ?", (key,))
        return str(row["value"]) if row else default

    def kv_set(self, key: str, value: str) -> None:
        self._exec(
            "INSERT OR REPLACE INTO kv(key, value, updated_at) VALUES(?, ?, ?)",
            (key, value, time.time()),
        )
