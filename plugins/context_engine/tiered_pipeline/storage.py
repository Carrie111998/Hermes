"""SQLite-backed L2/L3 storage for the tiered context engine."""

from __future__ import annotations

import heapq
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


class TieredContextStore:
    """Persist topic capsules and raw source messages transactionally."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(
            str(self.path),
            timeout=30,
            check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=30000")
        try:
            self._validate_existing_schema()
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._create_schema()
        except Exception:
            self._db.close()
            self._closed = True
            raise
        self._set_private_permissions()

    def _set_private_permissions(self) -> None:
        """Best-effort owner-only permissions for plaintext context files."""
        for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if not candidate.exists():
                continue
            try:
                os.chmod(candidate, 0o600)
            except OSError:
                pass

    @staticmethod
    def _schema_sql() -> str:
        return """
            CREATE TABLE IF NOT EXISTS capsules (
                session_id TEXT NOT NULL,
                topic_id TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                tier TEXT NOT NULL CHECK(tier IN ('L2', 'L3')),
                importance REAL NOT NULL DEFAULT 0.5,
                unresolved INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER NOT NULL DEFAULT 0,
                access_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_access_at REAL NOT NULL,
                source_tokens INTEGER NOT NULL DEFAULT 0,
                source_message_ids TEXT NOT NULL DEFAULT '[]',
                metadata TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(session_id, topic_id)
            );

            CREATE TABLE IF NOT EXISTS raw_messages (
                session_id TEXT NOT NULL,
                topic_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                message_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY(session_id, topic_id, ordinal),
                FOREIGN KEY(session_id, topic_id)
                    REFERENCES capsules(session_id, topic_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS session_scopes (
                session_id TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
        """

    def _create_indexes(self) -> None:
        statements = """
            CREATE INDEX IF NOT EXISTS idx_capsules_session_tier
                ON capsules(session_id, tier);
            CREATE INDEX IF NOT EXISTS idx_capsules_retention
                ON capsules(session_id, tier, pinned, unresolved, importance, last_access_at);
            CREATE INDEX IF NOT EXISTS idx_raw_messages_topic
                ON raw_messages(session_id, topic_id, ordinal);
            CREATE INDEX IF NOT EXISTS idx_session_scopes_scope
                ON session_scopes(scope_id);
        """
        for statement in statements.split(";"):
            if statement.strip():
                self._db.execute(statement)

    def _create_schema_tables(self) -> None:
        for statement in self._schema_sql().split(";"):
            if statement.strip():
                self._db.execute(statement)

    def _validate_existing_schema(self) -> None:
        expected = {
            "capsules": {
                "columns": {
                    "session_id", "topic_id", "title", "summary", "tier",
                    "importance", "unresolved", "pinned", "access_count",
                    "created_at", "updated_at", "last_access_at", "source_tokens",
                    "source_message_ids", "metadata",
                },
                "primary_key": ["session_id", "topic_id"],
            },
            "raw_messages": {
                "columns": {
                    "session_id", "topic_id", "ordinal", "message_json", "created_at",
                },
                "primary_key": ["session_id", "topic_id", "ordinal"],
            },
            "session_scopes": {
                "columns": {"session_id", "scope_id", "updated_at"},
                "primary_key": ["session_id"],
            },
        }
        table_info_sql = {
            "capsules": "PRAGMA table_info(capsules)",
            "raw_messages": "PRAGMA table_info(raw_messages)",
            "session_scopes": "PRAGMA table_info(session_scopes)",
        }
        for table, contract in expected.items():
            rows = self._db.execute(table_info_sql[table]).fetchall()
            if not rows:
                continue
            columns = {row["name"] for row in rows}
            primary_key = [
                row["name"]
                for row in sorted(rows, key=lambda row: row["pk"])
                if row["pk"]
            ]
            if columns != contract["columns"] or primary_key != contract["primary_key"]:
                raise RuntimeError(
                    f"Existing SQLite table {table!r} is incompatible with "
                    "tiered_pipeline; choose an empty database path"
                )

    def _create_schema(self) -> None:
        with self._lock:
            self._create_schema_tables()
            self._create_indexes()
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._db.close()
            self._closed = True

    def bind_session_scope(self, session_id: str, scope_id: str) -> None:
        """Persist an immutable physical-session to logical-scope mapping."""
        session_id = str(session_id or "").strip()
        scope_id = str(scope_id or "").strip()
        if not session_id or not scope_id:
            raise ValueError("session_id and scope_id are required")
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO session_scopes (session_id, scope_id, updated_at) "
                "VALUES (?, ?, ?)",
                (session_id, scope_id, time.time()),
            )
            row = self._db.execute(
                "SELECT scope_id FROM session_scopes WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None or str(row["scope_id"]) != scope_id:
                raise RuntimeError(
                    f"Session {session_id!r} is already bound to a different context scope"
                )

    def resolve_session_scope(self, session_id: str) -> Optional[str]:
        session_id = str(session_id or "").strip()
        if not session_id:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT scope_id FROM session_scopes WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return str(row["scope_id"]) if row is not None else None

    def put_capsule(
        self,
        *,
        topic_id: str,
        session_id: str,
        title: str,
        summary: str,
        importance: float = 0.5,
        unresolved: bool = False,
        pinned: bool = False,
        source_tokens: int = 0,
        source_message_ids: Optional[Iterable[Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        raw_messages: Optional[List[Dict[str, Any]]] = None,
        max_l2_topics: Optional[int] = None,
        l2_target_ratio: float = 0.70,
    ) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        now = time.time()
        encoded_ids = json.dumps(list(source_message_ids or []), ensure_ascii=False, allow_nan=False)
        encoded_metadata = json.dumps(metadata or {}, ensure_ascii=False, allow_nan=False)
        encoded_messages = [
            json.dumps(message, ensure_ascii=False, allow_nan=False)
            for message in (raw_messages or [])
        ]
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT INTO capsules (
                    session_id, topic_id, title, summary, tier, importance,
                    unresolved, pinned, created_at, updated_at, last_access_at,
                    source_tokens, source_message_ids, metadata
                ) VALUES (?, ?, ?, ?, 'L2', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, topic_id) DO UPDATE SET
                    title=excluded.title,
                    summary=excluded.summary,
                    tier='L2',
                    importance=excluded.importance,
                    unresolved=excluded.unresolved,
                    updated_at=excluded.updated_at,
                    last_access_at=excluded.last_access_at,
                    source_tokens=excluded.source_tokens,
                    source_message_ids=excluded.source_message_ids,
                    metadata=excluded.metadata
                """,
                (
                    session_id,
                    topic_id,
                    title,
                    summary,
                    max(0.0, min(1.0, float(importance))),
                    int(unresolved),
                    int(pinned),
                    now,
                    now,
                    now,
                    int(source_tokens),
                    encoded_ids,
                    encoded_metadata,
                ),
            )
            self._db.execute(
                "DELETE FROM raw_messages WHERE session_id=? AND topic_id=?",
                (session_id, topic_id),
            )
            if encoded_messages:
                self._db.executemany(
                    "INSERT INTO raw_messages("
                    "session_id, topic_id, ordinal, message_json, created_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    [
                        (session_id, topic_id, index, message_json, now)
                        for index, message_json in enumerate(encoded_messages)
                    ],
                )
            if max_l2_topics is not None:
                self._archive_l2_locked(
                    max_topics=max_l2_topics,
                    target_ratio=l2_target_ratio,
                    session_id=session_id,
                )
        self._set_private_permissions()

    def count(self, tier: str, session_id: Optional[str] = None) -> int:
        with self._lock:
            if session_id:
                row = self._db.execute(
                    "SELECT COUNT(*) AS n FROM capsules WHERE tier=? AND session_id=?",
                    (tier, session_id),
                ).fetchone()
            else:
                row = self._db.execute(
                    "SELECT COUNT(*) AS n FROM capsules WHERE tier=?", (tier,)
                ).fetchone()
        return int(row["n"])

    def _archive_l2_locked(
        self,
        *,
        max_topics: int,
        target_ratio: float,
        session_id: str,
    ) -> int:
        max_topics = max(1, int(max_topics))
        target = max(1, min(max_topics, int(max_topics * target_ratio)))
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM capsules "
            "WHERE tier='L2' AND session_id=?",
            (session_id,),
        ).fetchone()
        current = int(row["n"])
        if current <= max_topics:
            return 0
        amount = max(1, current - target)
        rows = self._db.execute(
            """
            SELECT topic_id FROM capsules
            WHERE tier='L2' AND session_id=? AND pinned=0
            ORDER BY unresolved ASC, importance ASC,
                     access_count ASC, last_access_at ASC
            LIMIT ?
            """,
            (session_id, amount),
        ).fetchall()
        archived = 0
        now = time.time()
        for row in rows:
            cursor = self._db.execute(
                "UPDATE capsules SET tier='L3', updated_at=? "
                "WHERE session_id=? AND topic_id=? "
                "AND tier='L2' AND pinned=0",
                (now, session_id, row["topic_id"]),
            )
            archived += cursor.rowcount
        return archived

    def archive_l2(self, *, max_topics: int, target_ratio: float, session_id: str) -> int:
        if not session_id:
            raise ValueError("session_id is required")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                archived = self._archive_l2_locked(
                    max_topics=max_topics,
                    target_ratio=target_ratio,
                    session_id=session_id,
                )
                self._db.commit()
                return archived
            except Exception:
                self._db.rollback()
                raise

    @staticmethod
    def _terms(query: str) -> List[str]:
        normalized = query[:1000].casefold().replace("_", " ")
        terms = {
            term
            for term in re.findall(r"[a-z0-9][a-z0-9.+-]*", normalized)
            if len(term) >= 2
        }
        for run in re.findall(r"[\u3400-\u9fff]+", normalized):
            if len(run) <= 4:
                terms.add(run)
            for width in (2, 3):
                terms.update(
                    run[index : index + width]
                    for index in range(len(run) - width + 1)
                )
        return sorted(terms, key=lambda term: (-len(term), term))

    def search(self, query: str, *, session_id: Optional[str], limit: int = 5) -> List[Dict[str, Any]]:
        if not session_id:
            return []
        terms = self._terms(query)
        if not terms:
            return []
        result_limit = max(1, min(20, int(limit)))
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT * FROM capsules WHERE session_id=?",
                (session_id,),
            )
            ranked = []
            for row in rows:
                haystack = f"{row['title']} {row['summary']}".casefold()
                lexical = sum(haystack.count(term) for term in terms)
                if lexical == 0:
                    continue
                tier_bonus = 0.05 if row["tier"] == "L2" else 0.0
                score = lexical + float(row["importance"]) * 0.25 + tier_bonus
                candidate = (
                    score,
                    float(row["updated_at"]),
                    str(row["topic_id"]),
                    row,
                )
                if len(ranked) < result_limit:
                    heapq.heappush(ranked, candidate)
                elif candidate[:3] > ranked[0][:3]:
                    heapq.heapreplace(ranked, candidate)
            ranked.sort(key=lambda item: item[:3], reverse=True)
            result = []
            now = time.time()
            for score, _updated_at, _topic_id, row in ranked:
                self._db.execute(
                    "UPDATE capsules SET access_count=access_count+1, last_access_at=? "
                    "WHERE session_id=? AND topic_id=?",
                    (now, session_id, row["topic_id"]),
                )
                result.append(
                    {
                        "topic_id": row["topic_id"],
                        "title": row["title"],
                        "summary": row["summary"],
                        "tier": row["tier"],
                        "importance": row["importance"],
                        "score": score,
                        "metadata": json.loads(row["metadata"]),
                    }
                )
        return result

    def get_raw_messages(
        self,
        topic_id: str,
        *,
        session_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not session_id:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT message_json FROM raw_messages "
                "WHERE session_id=? AND topic_id=? ORDER BY ordinal",
                (session_id, topic_id),
            ).fetchall()
        return [json.loads(row["message_json"]) for row in rows]

    def get_raw_message_json_page(
        self,
        topic_id: str,
        *,
        session_id: Optional[str],
        offset: int,
        limit: int,
    ) -> tuple[int, List[tuple[int, str]]]:
        """Return exact stored JSON so callers can page oversized messages."""
        if not session_id:
            return 0, []
        offset = max(0, int(offset))
        limit = max(1, min(20, int(limit)))
        with self._lock:
            total_row = self._db.execute(
                "SELECT COUNT(*) AS n FROM raw_messages "
                "WHERE session_id=? AND topic_id=?",
                (session_id, topic_id),
            ).fetchone()
            rows = self._db.execute(
                "SELECT ordinal, message_json FROM raw_messages "
                "WHERE session_id=? AND topic_id=? AND ordinal>=? "
                "ORDER BY ordinal LIMIT ?",
                (session_id, topic_id, offset, limit),
            ).fetchall()
        return int(total_row["n"]), [
            (int(row["ordinal"]), str(row["message_json"]))
            for row in rows
        ]

    def pin(
        self,
        topic_id: str,
        pinned: bool,
        *,
        session_id: Optional[str] = None,
    ) -> bool:
        if not session_id:
            return False
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE capsules SET pinned=?, updated_at=? "
                "WHERE topic_id=? AND session_id=?",
                (int(pinned), time.time(), topic_id, session_id),
            )
        return cursor.rowcount > 0

    def list_topics(self, *, session_id: Optional[str], limit: int = 20) -> List[Dict[str, Any]]:
        if not session_id:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT topic_id, title, tier, importance, pinned, unresolved, updated_at "
                "FROM capsules WHERE session_id=? ORDER BY updated_at DESC LIMIT ?",
                (session_id, max(1, min(100, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]
