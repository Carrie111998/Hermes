"""Durable bounded policy projection for hosted Group Chat preparation.

The append-only room log remains the user-visible source of truth. This module
materializes only the state needed to choose and reconstruct the next active
discussion, so a busy room does not replay its complete history every poll.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from gateway import hosted_rooms


MAX_ACTIVE_POLICY_EVENTS = 64
_TERMINAL_KINDS = frozenset({
    "turn.settled",
    "turn.failed",
    "turn.cancelled",
    "turn.deferred",
})


@dataclass(frozen=True)
class PolicySnapshot:
    """Bounded active policy input at one durable room-log cursor."""

    through_seq: int
    stopped_through_seq: int
    events: tuple[dict[str, Any], ...]
    watermarks: Mapping[tuple[str, str], int]


class HostedRoomPolicyCheckpoint:
    """Incrementally index room policy without compacting visible history."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        from hermes_state import apply_wal_with_fallback

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        apply_wal_with_fallback(conn, db_label="state.db (room policy checkpoint)")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS hosted_room_policy_cursors (
                    room_id TEXT PRIMARY KEY,
                    through_seq INTEGER NOT NULL DEFAULT 0,
                    stopped_through_seq INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS hosted_room_policy_threads (
                    room_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    discussion_event_id TEXT NOT NULL,
                    latest_user_seq INTEGER NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(room_id, thread_id)
                )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_hosted_room_policy_pending
                   ON hosted_room_policy_threads(
                       room_id, completed, latest_user_seq, thread_id
                   )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS hosted_room_policy_events (
                    room_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    discussion_event_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY(room_id, seq)
                )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_hosted_room_policy_events_active
                   ON hosted_room_policy_events(
                       room_id, discussion_event_id, seq
                   )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS hosted_room_policy_watermarks (
                    room_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    member_id TEXT NOT NULL,
                    seen_through_seq INTEGER NOT NULL,
                    PRIMARY KEY(room_id, thread_id, member_id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS hosted_room_policy_publications (
                    room_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    execution_generation INTEGER NOT NULL DEFAULT 0,
                    seq INTEGER NOT NULL,
                    PRIMARY KEY(room_id, task_id, kind, execution_generation)
                )"""
            )

    @staticmethod
    def _event_json(event: Mapping[str, Any]) -> str:
        return json.dumps(
            dict(event),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _store_active_event(
        self,
        conn: sqlite3.Connection,
        *,
        event: Mapping[str, Any],
        thread_id: str,
        discussion_event_id: str,
    ) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO hosted_room_policy_events(
                   room_id, thread_id, discussion_event_id, seq, event_json
               ) VALUES (?, ?, ?, ?, ?)""",
            (
                event["room_id"],
                thread_id,
                discussion_event_id,
                int(event["seq"]),
                self._event_json(event),
            ),
        )

    def _apply_event(self, conn: sqlite3.Connection, event: Mapping[str, Any]) -> None:
        room_id = str(event["room_id"])
        seq = int(event["seq"])
        kind = str(event.get("kind") or "")
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}

        if kind == "message.user":
            thread_id = str(payload.get("thread_id") or "")
            event_id = str(event.get("event_id") or "")
            if not thread_id or not event_id:
                return
            conn.execute(
                """INSERT INTO hosted_room_policy_threads(
                       room_id, thread_id, discussion_event_id,
                       latest_user_seq, completed
                   ) VALUES (?, ?, ?, ?, 0)
                   ON CONFLICT(room_id, thread_id) DO UPDATE SET
                       discussion_event_id=excluded.discussion_event_id,
                       latest_user_seq=excluded.latest_user_seq,
                       completed=0""",
                (room_id, thread_id, event_id, seq),
            )
            self._store_active_event(
                conn,
                event=event,
                thread_id=thread_id,
                discussion_event_id=event_id,
            )
            return

        if kind in {"message.member", *_TERMINAL_KINDS}:
            thread_id = str(payload.get("thread_id") or "")
            discussion_event_id = str(payload.get("discussion_event_id") or "")
            source = conn.execute(
                """SELECT 1 FROM hosted_room_policy_events
                   WHERE room_id=? AND discussion_event_id=? LIMIT 1""",
                (room_id, discussion_event_id),
            ).fetchone()
            if source is None:
                return
            self._store_active_event(
                conn,
                event=event,
                thread_id=thread_id,
                discussion_event_id=discussion_event_id,
            )
            if kind in _TERMINAL_KINDS:
                task_id = str(payload.get("task_id") or "")
                execution_generation = (
                    int(payload.get("execution_generation") or 0)
                    if kind == "turn.deferred"
                    else 0
                )
                if task_id:
                    conn.execute(
                        """INSERT OR IGNORE INTO hosted_room_policy_publications(
                               room_id, task_id, kind, execution_generation, seq
                           ) VALUES (?, ?, ?, ?, ?)""",
                        (
                            room_id,
                            task_id,
                            kind,
                            execution_generation,
                            seq,
                        ),
                    )
                member_id = str(payload.get("member_id") or "")
                seen_through_seq = int(payload.get("seen_through_seq") or 0)
                if kind == "turn.settled" and payload.get("message_event_id"):
                    messages = conn.execute(
                        """SELECT seq, event_json FROM hosted_room_policy_events
                           WHERE room_id=? AND discussion_event_id=?""",
                        (room_id, discussion_event_id),
                    ).fetchall()
                    message_seq = next(
                        (
                            int(message["seq"])
                            for message in messages
                            if json.loads(message["event_json"]).get("event_id")
                            == payload["message_event_id"]
                        ),
                        None,
                    )
                    if message_seq is not None:
                        seen_through_seq = max(seen_through_seq, message_seq)
                if member_id and seen_through_seq > 0:
                    conn.execute(
                        """INSERT INTO hosted_room_policy_watermarks(
                               room_id, thread_id, member_id, seen_through_seq
                           ) VALUES (?, ?, ?, ?)
                           ON CONFLICT(room_id, thread_id, member_id) DO UPDATE SET
                               seen_through_seq=MAX(
                                   hosted_room_policy_watermarks.seen_through_seq,
                                   excluded.seen_through_seq
                               )""",
                        (room_id, thread_id, member_id, seen_through_seq),
                    )
            return

        if kind == "room.activity":
            thread_id = str(payload.get("thread_id") or "")
            discussion_event_id = str(payload.get("discussion_event_id") or "")
            conn.execute(
                """DELETE FROM hosted_room_policy_events
                   WHERE room_id=? AND discussion_event_id=?""",
                (room_id, discussion_event_id),
            )
            conn.execute(
                """DELETE FROM hosted_room_policy_threads
                   WHERE room_id=? AND thread_id=?""",
                (room_id, thread_id),
            )
            return

        if kind == "room.stop_requested":
            conn.execute(
                """UPDATE hosted_room_policy_cursors
                   SET stopped_through_seq=MAX(stopped_through_seq, ?)
                   WHERE room_id=?""",
                (seq, room_id),
            )

    def sync(self, *, room_id: str, latest_seq: int) -> int:
        """Materialize each unseen event exactly once by durable cursor."""

        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO hosted_room_policy_cursors(
                       room_id, through_seq, stopped_through_seq, updated_at
                   ) VALUES (?, 0, 0, 0)""",
                (room_id,),
            )
            row = conn.execute(
                "SELECT through_seq FROM hosted_room_policy_cursors WHERE room_id=?",
                (room_id,),
            ).fetchone()
            cursor = int(row["through_seq"])
        if cursor > latest_seq:
            raise RuntimeError("room policy cursor is ahead of the durable log")

        while cursor < latest_seq:
            page = hosted_rooms.read_events(
                self.db_path,
                room_id=room_id,
                since_seq=cursor,
                limit=hosted_rooms.MAX_LOG_LIMIT,
            )
            rows = [
                event for event in page.get("events", []) if isinstance(event, Mapping)
            ]
            next_cursor = int(page.get("cursor") or cursor)
            if not rows or next_cursor <= cursor:
                raise RuntimeError("hosted room policy cursor did not advance")
            with self._connect() as conn:
                for event in rows:
                    self._apply_event(conn, event)
                conn.execute(
                    """UPDATE hosted_room_policy_cursors
                       SET through_seq=?, updated_at=? WHERE room_id=?""",
                    (
                        next_cursor,
                        float(rows[-1].get("created_at") or 0),
                        room_id,
                    ),
                )
            cursor = next_cursor
        return cursor

    def snapshot(self, *, room_id: str, latest_seq: int) -> PolicySnapshot:
        """Return only the oldest active discussion and its watermark set."""

        through_seq = self.sync(room_id=room_id, latest_seq=latest_seq)
        with self._connect() as conn:
            cursor = conn.execute(
                """SELECT stopped_through_seq FROM hosted_room_policy_cursors
                   WHERE room_id=?""",
                (room_id,),
            ).fetchone()
            stopped_through_seq = int(cursor["stopped_through_seq"])
            thread = conn.execute(
                """SELECT thread_id, discussion_event_id
                   FROM hosted_room_policy_threads
                   WHERE room_id=? AND completed=0 AND latest_user_seq>?
                   ORDER BY latest_user_seq, thread_id LIMIT 1""",
                (room_id, stopped_through_seq),
            ).fetchone()
            if thread is None:
                return PolicySnapshot(
                    through_seq=through_seq,
                    stopped_through_seq=stopped_through_seq,
                    events=(),
                    watermarks={},
                )
            rows = conn.execute(
                """SELECT event_json FROM hosted_room_policy_events
                   WHERE room_id=? AND discussion_event_id=?
                   ORDER BY seq LIMIT ?""",
                (
                    room_id,
                    str(thread["discussion_event_id"]),
                    MAX_ACTIVE_POLICY_EVENTS + 1,
                ),
            ).fetchall()
            if len(rows) > MAX_ACTIVE_POLICY_EVENTS:
                raise RuntimeError("active room policy projection exceeded its bound")
            watermark_rows = conn.execute(
                """SELECT member_id, seen_through_seq
                   FROM hosted_room_policy_watermarks
                   WHERE room_id=? AND thread_id=?""",
                (room_id, str(thread["thread_id"])),
            ).fetchall()
        return PolicySnapshot(
            through_seq=through_seq,
            stopped_through_seq=stopped_through_seq,
            events=tuple(json.loads(row["event_json"]) for row in rows),
            watermarks={
                (str(thread["thread_id"]), str(row["member_id"])): int(
                    row["seen_through_seq"]
                )
                for row in watermark_rows
            },
        )

    def publication_exists(
        self,
        *,
        room_id: str,
        task_id: str,
        status: str,
        execution_generation: int,
    ) -> bool:
        """Return whether one exact driver outcome is already in the room log."""

        kind = f"turn.{status}"
        generation = execution_generation if status == "deferred" else 0
        with self._connect() as conn:
            if status == "deferred":
                row = conn.execute(
                    """SELECT 1 FROM hosted_room_policy_publications
                       WHERE room_id=? AND task_id=? AND kind=?
                         AND execution_generation=?""",
                    (room_id, task_id, kind, generation),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT 1 FROM hosted_room_policy_publications
                       WHERE room_id=? AND task_id=? AND kind IN (
                           'turn.settled', 'turn.failed', 'turn.cancelled'
                       )""",
                    (room_id, task_id),
                ).fetchone()
        return row is not None

    def events_for_task(
        self,
        *,
        room_id: str,
        source_event_seq: int,
    ) -> list[dict[str, Any]]:
        """Load one bounded discussion projection for terminal reconstruction."""

        with self._connect() as conn:
            source = conn.execute(
                """SELECT discussion_event_id FROM hosted_room_policy_events
                   WHERE room_id=? AND seq=?""",
                (room_id, source_event_seq),
            ).fetchone()
            if source is None:
                return []
            rows = conn.execute(
                """SELECT event_json FROM hosted_room_policy_events
                   WHERE room_id=? AND discussion_event_id=?
                   ORDER BY seq LIMIT ?""",
                (
                    room_id,
                    str(source["discussion_event_id"]),
                    MAX_ACTIVE_POLICY_EVENTS + 1,
                ),
            ).fetchall()
        if len(rows) > MAX_ACTIVE_POLICY_EVENTS:
            raise RuntimeError("task policy projection exceeded its bound")
        return [json.loads(row["event_json"]) for row in rows]

    def compact_completed(self, *, room_id: str) -> None:
        """Drop any completed projections left by an interrupted sync."""

        with self._connect() as conn:
            completed = conn.execute(
                """SELECT discussion_event_id FROM hosted_room_policy_threads
                   WHERE room_id=? AND completed=1""",
                (room_id,),
            ).fetchall()
            for row in completed:
                conn.execute(
                    """DELETE FROM hosted_room_policy_events
                       WHERE room_id=? AND discussion_event_id=?""",
                    (room_id, str(row["discussion_event_id"])),
                )
            conn.execute(
                """DELETE FROM hosted_room_policy_threads
                   WHERE room_id=? AND completed=1""",
                (room_id,),
            )
