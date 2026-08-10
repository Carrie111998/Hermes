"""Device-local durable state for Hermes workspace runners."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_cli.runner_protocol import RunnerCommand, RunnerEvent


@dataclass(frozen=True, slots=True)
class BindingRecord:
    binding_id: str
    label: str
    project_id: str
    revoked: bool
    root_path: Path

    def public_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "label": self.label,
            "project_id": self.project_id,
            "revoked": self.revoked,
        }


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    binding_id: str
    expected_head: str | None
    expires_at: float
    fencing_token: int
    lease_id: str
    owner: str


class RunnerSpool:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).expanduser()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.database_path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30,
        )
        if os.name != "nt":
            self.database_path.chmod(0o600)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS bindings (
                binding_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                label TEXT NOT NULL,
                root_path TEXT NOT NULL,
                root_dev INTEGER NOT NULL,
                root_ino INTEGER NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lease_counters (
                binding_id TEXT PRIMARY KEY REFERENCES bindings(binding_id) ON DELETE CASCADE,
                last_token INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leases (
                binding_id TEXT PRIMARY KEY REFERENCES bindings(binding_id) ON DELETE CASCADE,
                lease_id TEXT NOT NULL UNIQUE,
                owner TEXT NOT NULL,
                fencing_token INTEGER NOT NULL,
                expires_at REAL NOT NULL,
                expected_head TEXT
            );
            CREATE TABLE IF NOT EXISTS commands (
                command_id TEXT PRIMARY KEY,
                command_json TEXT NOT NULL,
                state TEXT NOT NULL,
                result_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_json TEXT NOT NULL,
                acked INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                UNIQUE(attempt_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS events_pending_idx
                ON events(attempt_id, acked, sequence);
            CREATE TABLE IF NOT EXISTS push_approvals (
                request_id TEXT PRIMARY KEY,
                binding_id TEXT NOT NULL REFERENCES bindings(binding_id) ON DELETE CASCADE,
                request_json TEXT NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            """
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def register_binding(
        self,
        *,
        project_id: str,
        root_path: str | Path,
        label: str,
        binding_id: str | None = None,
    ) -> BindingRecord:
        root = Path(root_path).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("binding root must be a directory")
        stat = root.stat()
        now = time.time()

        with self._lock:
            if binding_id is None:
                existing = self._connection.execute(
                    "SELECT * FROM bindings WHERE root_path=? AND project_id=? AND revoked=0",
                    (str(root), project_id),
                ).fetchone()
                if existing is not None:
                    return BindingRecord(
                        existing["binding_id"],
                        existing["label"],
                        existing["project_id"],
                        False,
                        root,
                    )
            identifier = binding_id or str(uuid.uuid4())
            self._connection.execute(
                """
                INSERT INTO bindings(
                    binding_id, project_id, label, root_path, root_dev, root_ino, revoked, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(binding_id) DO UPDATE SET
                    project_id=excluded.project_id,
                    label=excluded.label,
                    root_path=excluded.root_path,
                    root_dev=excluded.root_dev,
                    root_ino=excluded.root_ino,
                    revoked=0
                """,
                (identifier, project_id, label, str(root), stat.st_dev, stat.st_ino, now),
            )

        return BindingRecord(identifier, label, project_id, False, root)

    def revoke_binding(self, binding_id: str) -> None:
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE bindings SET revoked=1 WHERE binding_id=?", (binding_id,)
            )
            self._connection.execute("DELETE FROM leases WHERE binding_id=?", (binding_id,))
            if cursor.rowcount != 1:
                raise ValueError("binding is unknown")

    def _binding_row(self, binding_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM bindings WHERE binding_id=?", (binding_id,)
        ).fetchone()
        if row is None:
            raise ValueError("binding is unknown")
        if bool(row["revoked"]):
            raise ValueError("binding is revoked")
        return row

    def resolve_binding(self, binding_id: str) -> Path:
        with self._lock:
            row = self._binding_row(binding_id)
            root = Path(row["root_path"]).resolve(strict=True)
            stat = root.stat()
            if stat.st_dev != row["root_dev"] or stat.st_ino != row["root_ino"]:
                raise ValueError("binding root identity changed")
            return root

    def binding_record(self, binding_id: str) -> BindingRecord:
        with self._lock:
            row = self._binding_row(binding_id)
            root = self.resolve_binding(binding_id)
        return BindingRecord(
            binding_id=row["binding_id"],
            label=row["label"],
            project_id=row["project_id"],
            revoked=bool(row["revoked"]),
            root_path=root,
        )

    def public_bindings(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT binding_id, project_id, label, revoked FROM bindings ORDER BY created_at"
            ).fetchall()
        return [
            {
                "binding_id": row["binding_id"],
                "label": row["label"],
                "project_id": row["project_id"],
                "revoked": bool(row["revoked"]),
            }
            for row in rows
        ]

    def acquire_lease(
        self,
        *,
        binding_id: str,
        owner: str,
        ttl_seconds: float,
        expected_head: str | None,
        now: float | None = None,
    ) -> LeaseRecord:
        timestamp = time.time() if now is None else now
        if ttl_seconds <= 0:
            raise ValueError("lease ttl must be positive")

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._binding_row(binding_id)
                current = self._connection.execute(
                    "SELECT * FROM leases WHERE binding_id=?", (binding_id,)
                ).fetchone()
                if current is not None and current["expires_at"] > timestamp:
                    if current["owner"] != owner:
                        raise ValueError("binding is already leased")
                    self._connection.execute(
                        "UPDATE leases SET expires_at=? WHERE binding_id=?",
                        (timestamp + ttl_seconds, binding_id),
                    )
                    record = LeaseRecord(
                        binding_id=binding_id,
                        expected_head=current["expected_head"],
                        expires_at=timestamp + ttl_seconds,
                        fencing_token=current["fencing_token"],
                        lease_id=current["lease_id"],
                        owner=owner,
                    )
                    self._connection.execute("COMMIT")
                    return record

                counter = self._connection.execute(
                    "SELECT last_token FROM lease_counters WHERE binding_id=?", (binding_id,)
                ).fetchone()
                fencing_token = (counter["last_token"] if counter else 0) + 1
                lease_id = str(uuid.uuid4())
                expires_at = timestamp + ttl_seconds
                self._connection.execute(
                    """
                    INSERT INTO lease_counters(binding_id, last_token) VALUES (?, ?)
                    ON CONFLICT(binding_id) DO UPDATE SET last_token=excluded.last_token
                    """,
                    (binding_id, fencing_token),
                )
                self._connection.execute(
                    """
                    INSERT INTO leases(binding_id, lease_id, owner, fencing_token, expires_at, expected_head)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(binding_id) DO UPDATE SET
                        lease_id=excluded.lease_id,
                        owner=excluded.owner,
                        fencing_token=excluded.fencing_token,
                        expires_at=excluded.expires_at,
                        expected_head=excluded.expected_head
                    """,
                    (binding_id, lease_id, owner, fencing_token, expires_at, expected_head),
                )
                self._connection.execute("COMMIT")
                return LeaseRecord(
                    binding_id=binding_id,
                    expected_head=expected_head,
                    expires_at=expires_at,
                    fencing_token=fencing_token,
                    lease_id=lease_id,
                    owner=owner,
                )
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def validate_lease(
        self,
        *,
        binding_id: str,
        lease_id: str,
        fencing_token: int,
        live_head: str | None,
        now: float | None = None,
    ) -> LeaseRecord:
        timestamp = time.time() if now is None else now
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM leases WHERE binding_id=?", (binding_id,)
            ).fetchone()
        if row is None or row["lease_id"] != lease_id or row["fencing_token"] != fencing_token:
            raise ValueError("lease or fencing token is stale")
        if row["expires_at"] <= timestamp:
            raise ValueError("lease expired")
        if row["expected_head"] is not None and row["expected_head"] != live_head:
            raise ValueError("repository HEAD changed after lease acquisition")
        return LeaseRecord(
            binding_id=binding_id,
            expected_head=row["expected_head"],
            expires_at=row["expires_at"],
            fencing_token=row["fencing_token"],
            lease_id=row["lease_id"],
            owner=row["owner"],
        )

    def update_lease_head(
        self,
        *,
        binding_id: str,
        lease_id: str,
        fencing_token: int,
        expected_head: str,
    ) -> None:
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE leases SET expected_head=?
                WHERE binding_id=? AND lease_id=? AND fencing_token=?
                """,
                (expected_head, binding_id, lease_id, fencing_token),
            )
            if cursor.rowcount != 1:
                raise ValueError("lease or fencing token is stale")

    def release_lease(
        self,
        *,
        binding_id: str,
        lease_id: str,
        fencing_token: int,
    ) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM leases WHERE binding_id=? AND lease_id=? AND fencing_token=?",
                (binding_id, lease_id, fencing_token),
            )
            return cursor.rowcount == 1

    def has_live_lease(self, binding_id: str, *, now: float | None = None) -> bool:
        timestamp = time.time() if now is None else now
        with self._lock:
            row = self._connection.execute(
                "SELECT expires_at FROM leases WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
        return row is not None and float(row["expires_at"]) > timestamp

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def begin_command(self, command: RunnerCommand) -> bool:
        serialized = self._json(command.to_dict())
        now = time.time()
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO commands(command_id, command_json, state, created_at, updated_at)
                VALUES (?, ?, 'accepted', ?, ?)
                """,
                (command.command_id, serialized, now, now),
            )
            if cursor.rowcount == 1:
                return True
            existing = self._connection.execute(
                "SELECT command_json FROM commands WHERE command_id=?", (command.command_id,)
            ).fetchone()
            if existing is None or existing["command_json"] != serialized:
                raise ValueError("command id was reused with a different payload")
            return False

    def complete_command(self, command_id: str, *, state: str, result: Any) -> None:
        if state not in {"canceled", "completed", "failed", "uncertain"}:
            raise ValueError("command terminal state is invalid")
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE commands SET state=?, result_json=?, updated_at=? WHERE command_id=?",
                (state, self._json(result), time.time(), command_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("command is unknown")

    def complete_command_with_event(
        self,
        command_id: str,
        *,
        state: str,
        result: Any,
        event: RunnerEvent,
    ) -> None:
        if state not in {"canceled", "completed", "failed", "uncertain"}:
            raise ValueError("command terminal state is invalid")
        serialized_event = self._json(event.to_dict())
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._connection.execute(
                    "UPDATE commands SET state=?, result_json=?, updated_at=? "
                    "WHERE command_id=? AND state='accepted'",
                    (state, self._json(result), time.time(), command_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("command is unknown or already terminal")
                self._connection.execute(
                    "INSERT INTO events(event_id, attempt_id, sequence, event_json, acked, created_at) "
                    "VALUES (?, ?, ?, ?, 0, ?)",
                    (
                        event.event_id,
                        event.attempt_id,
                        event.sequence,
                        serialized_event,
                        time.time(),
                    ),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def command_result(self, command_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT state, result_json FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "result": json.loads(row["result_json"]) if row["result_json"] is not None else None,
            "state": row["state"],
        }

    def reconcile_incomplete_commands(self) -> list[str]:
        reconciled: list[str] = []
        result = {"error": "runner restarted before command completion", "uncertain": True}
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self._connection.execute(
                    "SELECT command_id, command_json FROM commands WHERE state='accepted' "
                    "ORDER BY created_at, command_id"
                ).fetchall()
                for row in rows:
                    command = RunnerCommand.from_dict(json.loads(row["command_json"]))
                    sequence_row = self._connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) AS value FROM events WHERE attempt_id=?",
                        (command.attempt_id,),
                    ).fetchone()
                    sequence = int(sequence_row["value"]) + 1
                    event = RunnerEvent.create(
                        attempt_id=command.attempt_id,
                        event_type="run.uncertain",
                        payload={"command_id": command.command_id},
                        run_id=command.run_id,
                        sequence=sequence,
                    )
                    self._connection.execute(
                        "UPDATE commands SET state='uncertain', result_json=?, updated_at=? "
                        "WHERE command_id=?",
                        (self._json(result), time.time(), command.command_id),
                    )
                    self._connection.execute(
                        "INSERT INTO events(event_id, attempt_id, sequence, event_json, acked, created_at) "
                        "VALUES (?, ?, ?, ?, 0, ?)",
                        (
                            event.event_id,
                            event.attempt_id,
                            event.sequence,
                            self._json(event.to_dict()),
                            time.time(),
                        ),
                    )
                    reconciled.append(command.command_id)
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return reconciled

    def store_push_request(self, *, binding_id: str, request: dict[str, Any]) -> None:
        request_id = str(request.get("requestId") or "")
        if not request_id:
            raise ValueError("push request id is required")
        serialized = self._json(request)
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO push_approvals(
                    request_id, binding_id, request_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (request_id, binding_id, serialized, time.time()),
            )
            if cursor.rowcount == 1:
                return
            existing = self._connection.execute(
                "SELECT binding_id, request_json FROM push_approvals WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if (
                existing is None
                or existing["binding_id"] != binding_id
                or existing["request_json"] != serialized
            ):
                raise ValueError("push request id was reused")

    def consume_push_request(self, *, binding_id: str, request_id: str) -> dict[str, Any]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM push_approvals WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if row is None or row["binding_id"] != binding_id or bool(row["consumed"]):
                    raise ValueError("push approval request is unknown or already consumed")
                self._connection.execute(
                    "UPDATE push_approvals SET consumed=1 WHERE request_id=?",
                    (request_id,),
                )
                self._connection.execute("COMMIT")
                return json.loads(row["request_json"])
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def append_event(self, event: RunnerEvent) -> bool:
        serialized = self._json(event.to_dict())
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO events(event_id, attempt_id, sequence, event_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event.event_id, event.attempt_id, event.sequence, serialized, time.time()),
            )
            if cursor.rowcount == 1:
                return True
            existing = self._connection.execute(
                "SELECT event_id, event_json FROM events WHERE event_id=? OR (attempt_id=? AND sequence=?)",
                (event.event_id, event.attempt_id, event.sequence),
            ).fetchone()
            if existing and existing["event_id"] == event.event_id and existing["event_json"] == serialized:
                return False
            raise ValueError("event identity or sequence was reused")

    def next_event_sequence(self, attempt_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS latest FROM events WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        return int(row["latest"]) + 1

    def pending_events(self, attempt_id: str, *, limit: int = 1000) -> list[RunnerEvent]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT event_json FROM events
                WHERE attempt_id=? AND acked=0
                ORDER BY sequence ASC LIMIT ?
                """,
                (attempt_id, max(1, min(limit, 10_000))),
            ).fetchall()
        return [RunnerEvent.from_dict(json.loads(row["event_json"])) for row in rows]

    def pending_events_all(self, *, limit: int = 256) -> list[RunnerEvent]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT event_json FROM events WHERE acked=0 "
                "ORDER BY created_at,event_id LIMIT ?",
                (max(1, min(limit, 256)),),
            ).fetchall()
        return [RunnerEvent.from_dict(json.loads(row["event_json"])) for row in rows]

    def ack_event_ids(self, event_ids: list[str]) -> None:
        normalized = sorted({value for value in event_ids if value})
        if len(normalized) > 256:
            raise ValueError("event acknowledgement exceeds the limit")
        if not normalized:
            return
        placeholders = ",".join("?" for _value in normalized)
        with self._lock:
            self._connection.execute(
                f"UPDATE events SET acked=1 WHERE event_id IN ({placeholders})",
                normalized,
            )

    def ack_events(self, attempt_id: str, *, through_sequence: int) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE events SET acked=1 WHERE attempt_id=? AND sequence<=?",
                (attempt_id, through_sequence),
            )
