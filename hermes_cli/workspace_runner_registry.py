"""Durable cloud-safe registry for outbound Project Workspace runners."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hermes_cli.runner_protocol import RunnerEvent

MAX_BINDINGS_PER_RUNNER = 128
MAX_CAPABILITIES_PER_RUNNER = 64
MAX_COMMAND_FRAME_BYTES = 512 * 1024
MAX_COMMAND_RESULT_BYTES = 2 * 1024 * 1024
MAX_PENDING_COMMANDS_PER_RUNNER = 256


@dataclass(frozen=True, slots=True)
class RunnerEnrollment:
    enrollment_token: str
    expires_at: float
    runner_id: str


@dataclass(frozen=True, slots=True)
class RunnerCredentials:
    command_key: bytes
    device_token: str
    runner_id: str


class WorkspaceRunnerRegistry:
    def __init__(self, database_path: str | Path, *, master_key_path: str | Path):
        self.database_path = Path(database_path).expanduser()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._master_key = self._load_or_create_key(Path(master_key_path).expanduser())
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
            CREATE TABLE IF NOT EXISTS workspace_runners (
                runner_id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                token_hash TEXT,
                command_key_cipher TEXT,
                capabilities_json TEXT NOT NULL DEFAULT '[]',
                revoked INTEGER NOT NULL DEFAULT 0,
                last_seen REAL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_runner_enrollments (
                runner_id TEXT PRIMARY KEY REFERENCES workspace_runners(runner_id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL,
                expires_at REAL NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS workspace_runner_bindings (
                runner_id TEXT NOT NULL REFERENCES workspace_runners(runner_id) ON DELETE CASCADE,
                binding_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                label TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY (runner_id, binding_id)
            );
            CREATE TABLE IF NOT EXISTS workspace_runner_commands (
                command_id TEXT PRIMARY KEY,
                runner_id TEXT NOT NULL REFERENCES workspace_runners(runner_id) ON DELETE CASCADE,
                frame_json TEXT NOT NULL,
                state TEXT NOT NULL,
                result_json TEXT,
                sent_at REAL,
                acknowledged_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS workspace_runner_commands_pending_idx
                ON workspace_runner_commands(runner_id, state, created_at);
            CREATE TABLE IF NOT EXISTS workspace_runner_command_reconciliations (
                command_id TEXT PRIMARY KEY
                    REFERENCES workspace_runner_commands(command_id) ON DELETE CASCADE,
                decision TEXT NOT NULL,
                outcome TEXT,
                replacement_command_id TEXT,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_runner_events (
                event_id TEXT PRIMARY KEY,
                runner_id TEXT NOT NULL REFERENCES workspace_runners(runner_id),
                attempt_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(runner_id, attempt_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS workspace_runner_events_order_idx
                ON workspace_runner_events(runner_id, attempt_id, sequence);
            """
        )
        columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(workspace_runners)").fetchall()
        }
        if "capabilities_json" not in columns:
            self._connection.execute(
                "ALTER TABLE workspace_runners "
                "ADD COLUMN capabilities_json TEXT NOT NULL DEFAULT '[]'"
            )
        command_columns = {
            str(row[1])
            for row in self._connection.execute(
                "PRAGMA table_info(workspace_runner_commands)"
            ).fetchall()
        }
        if "sent_at" not in command_columns:
            self._connection.execute(
                "ALTER TABLE workspace_runner_commands ADD COLUMN sent_at REAL"
            )
        if "acknowledged_at" not in command_columns:
            self._connection.execute(
                "ALTER TABLE workspace_runner_commands ADD COLUMN acknowledged_at REAL"
            )
        self._connection.execute(
            "UPDATE workspace_runner_commands SET state='sent' WHERE state='dispatched'"
        )
        self._connection.commit()

    @staticmethod
    def _load_or_create_key(path: Path) -> bytes:
        try:
            value = path.read_bytes()
            if len(value) != 32:
                raise ValueError("workspace runner master key is invalid")
            return value
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
            value = secrets.token_bytes(32)
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return WorkspaceRunnerRegistry._load_or_create_key(path)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            return value

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _encrypt_key(self, runner_id: str, value: bytes) -> str:
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(self._master_key).encrypt(nonce, value, runner_id.encode("utf-8"))
        return base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")

    def _decrypt_key(self, runner_id: str, value: str) -> bytes:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        return AESGCM(self._master_key).decrypt(raw[:12], raw[12:], runner_id.encode("utf-8"))

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_enrollment(
        self,
        label: str,
        *,
        now: float | None = None,
        ttl_seconds: float = 600,
    ) -> RunnerEnrollment:
        cleaned = label.strip()
        if not cleaned:
            raise ValueError("runner label is required")
        if ttl_seconds <= 0 or ttl_seconds > 3600:
            raise ValueError("runner enrollment TTL is invalid")
        timestamp = time.time() if now is None else now
        runner_id = f"r_{uuid.uuid4().hex}"
        enrollment_token = secrets.token_urlsafe(32)
        expires_at = timestamp + ttl_seconds
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    "INSERT INTO workspace_runners(runner_id, label, created_at) VALUES (?, ?, ?)",
                    (runner_id, cleaned, timestamp),
                )
                self._connection.execute(
                    "INSERT INTO workspace_runner_enrollments(runner_id, token_hash, expires_at) "
                    "VALUES (?, ?, ?)",
                    (runner_id, self._hash(enrollment_token), expires_at),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return RunnerEnrollment(enrollment_token, expires_at, runner_id)

    def consume_enrollment(
        self,
        runner_id: str,
        enrollment_token: str,
        *,
        now: float | None = None,
    ) -> RunnerCredentials:
        timestamp = time.time() if now is None else now
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM workspace_runner_enrollments WHERE runner_id=?",
                    (runner_id,),
                ).fetchone()
                if (
                    row is None
                    or bool(row["consumed"])
                    or float(row["expires_at"]) <= timestamp
                    or not hmac.compare_digest(row["token_hash"], self._hash(enrollment_token))
                ):
                    raise ValueError("runner enrollment token is invalid or expired")
                device_token = secrets.token_urlsafe(32)
                command_key = secrets.token_bytes(32)
                self._connection.execute(
                    "UPDATE workspace_runners SET token_hash=?, command_key_cipher=?, last_seen=? "
                    "WHERE runner_id=?",
                    (
                        self._hash(device_token),
                        self._encrypt_key(runner_id, command_key),
                        timestamp,
                        runner_id,
                    ),
                )
                self._connection.execute(
                    "UPDATE workspace_runner_enrollments SET consumed=1 WHERE runner_id=?",
                    (runner_id,),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return RunnerCredentials(command_key, device_token, runner_id)

    def authenticate(self, runner_id: str, device_token: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT token_hash, revoked FROM workspace_runners WHERE runner_id=?",
                (runner_id,),
            ).fetchone()
        return bool(
            row
            and not bool(row["revoked"])
            and row["token_hash"]
            and hmac.compare_digest(row["token_hash"], self._hash(device_token))
        )

    def command_key(self, runner_id: str) -> bytes:
        with self._lock:
            row = self._connection.execute(
                "SELECT command_key_cipher, revoked FROM workspace_runners WHERE runner_id=?",
                (runner_id,),
            ).fetchone()
        if row is None or bool(row["revoked"]) or not row["command_key_cipher"]:
            raise ValueError("runner credentials are unavailable")
        return self._decrypt_key(runner_id, row["command_key_cipher"])

    def heartbeat(self, runner_id: str, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE workspace_runners SET last_seen=? WHERE runner_id=? AND revoked=0",
                (timestamp, runner_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("runner is unknown or revoked")

    def sync_capabilities(self, runner_id: str, capabilities: list[str]) -> None:
        if len(capabilities) > MAX_CAPABILITIES_PER_RUNNER:
            raise ValueError("runner capability inventory exceeds the limit")
        normalized = sorted({str(item).strip() for item in capabilities if str(item).strip()})
        if any(not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", item) for item in normalized):
            raise ValueError("runner capability is invalid")
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE workspace_runners SET capabilities_json=? "
                "WHERE runner_id=? AND revoked=0",
                (self._json(normalized), runner_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("runner is unknown or revoked")

    def rotate_credentials(self, runner_id: str) -> RunnerCredentials:
        device_token = secrets.token_urlsafe(32)
        command_key = secrets.token_bytes(32)
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE workspace_runners SET token_hash=?, command_key_cipher=?, last_seen=? "
                "WHERE runner_id=? AND revoked=0",
                (
                    self._hash(device_token),
                    self._encrypt_key(runner_id, command_key),
                    time.time(),
                    runner_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("runner is unknown or revoked")
        return RunnerCredentials(command_key, device_token, runner_id)

    def revoke_runner(self, runner_id: str) -> None:
        timestamp = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._connection.execute(
                    "UPDATE workspace_runners "
                    "SET revoked=1,token_hash=NULL,command_key_cipher=NULL "
                    "WHERE runner_id=?",
                    (runner_id,),
                )
                if cursor.rowcount != 1:
                    raise ValueError("runner is unknown")
                commands = self._connection.execute(
                    "SELECT command_id FROM workspace_runner_commands "
                    "WHERE runner_id=? AND state IN ('queued','sent','acknowledged')",
                    (runner_id,),
                ).fetchall()
                for row in commands:
                    command_id = str(row["command_id"])
                    result = self._json(
                        {
                            "command_id": command_id,
                            "error": "runner was revoked",
                            "ok": False,
                            "replayed": False,
                            "result": None,
                            "state": "revoked",
                        }
                    )
                    self._connection.execute(
                        "UPDATE workspace_runner_commands "
                        "SET state='revoked',result_json=?,updated_at=? WHERE command_id=?",
                        (result, timestamp, command_id),
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def _reject_absolute_paths(cls, value: Any, *, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                cls._reject_absolute_paths(child_value, key=str(child_key))
            return
        if isinstance(value, list):
            for child in value:
                cls._reject_absolute_paths(child, key=key)
            return
        lowered = key.lower()
        if (
            isinstance(value, str)
            and ("path" in lowered or lowered in {"cwd", "root"})
            and Path(value).expanduser().is_absolute()
        ):
            raise ValueError("runner command must not contain an absolute local path")

    def ingest_events(
        self,
        runner_id: str,
        events: list[dict[str, Any]],
        *,
        now: float | None = None,
    ) -> list[str]:
        if len(events) > 256:
            raise ValueError("runner event batch exceeds the limit")
        timestamp = time.time() if now is None else now
        parsed: list[tuple[RunnerEvent, str]] = []
        for raw in events:
            self._reject_absolute_paths(raw)
            event = RunnerEvent.from_dict(raw)
            parsed.append((event, self._json(event.to_dict())))
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                runner = self._connection.execute(
                    "SELECT revoked FROM workspace_runners WHERE runner_id=?",
                    (runner_id,),
                ).fetchone()
                if runner is None or bool(runner["revoked"]):
                    raise ValueError("runner is unknown or revoked")
                for event, serialized in parsed:
                    existing = self._connection.execute(
                        "SELECT runner_id,event_json FROM workspace_runner_events "
                        "WHERE event_id=?",
                        (event.event_id,),
                    ).fetchone()
                    if existing is not None:
                        if existing["runner_id"] != runner_id or existing["event_json"] != serialized:
                            raise ValueError("runner event id was reused")
                        continue
                    self._connection.execute(
                        "INSERT INTO workspace_runner_events("
                        "event_id,runner_id,attempt_id,sequence,event_json,created_at"
                        ") VALUES(?,?,?,?,?,?)",
                        (
                            event.event_id,
                            runner_id,
                            event.attempt_id,
                            event.sequence,
                            serialized,
                            timestamp,
                        ),
                    )
                self._connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._connection.execute("ROLLBACK")
                raise ValueError("runner event sequence conflicts with durable history") from exc
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return [event.event_id for event, _serialized in parsed]

    def list_events(self, runner_id: str, attempt_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT event_json FROM workspace_runner_events "
                "WHERE runner_id=? AND attempt_id=? ORDER BY sequence",
                (runner_id, attempt_id),
            ).fetchall()
        return [json.loads(row["event_json"]) for row in rows]

    def queue_command(
        self,
        runner_id: str,
        command_id: str,
        frame: dict[str, Any],
        *,
        now: float | None = None,
    ) -> None:
        if not command_id.strip() or frame.get("command_id") != command_id:
            raise ValueError("runner command id is invalid")
        self._reject_absolute_paths(frame)
        serialized = self._json(frame)
        if len(serialized.encode("utf-8")) > MAX_COMMAND_FRAME_BYTES:
            raise ValueError("runner command frame exceeds the durable size limit")
        timestamp = time.time() if now is None else now
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                runner = self._connection.execute(
                    "SELECT revoked FROM workspace_runners WHERE runner_id=?",
                    (runner_id,),
                ).fetchone()
                if runner is None or bool(runner["revoked"]):
                    raise ValueError("runner is unknown or revoked")
                existing = self._connection.execute(
                    "SELECT runner_id, frame_json FROM workspace_runner_commands WHERE command_id=?",
                    (command_id,),
                ).fetchone()
                if existing is None:
                    pending_count = self._connection.execute(
                        "SELECT COUNT(*) FROM workspace_runner_commands "
                        "WHERE runner_id=? AND state IN "
                        "('queued','sent','acknowledged','uncertain','reconciling')",
                        (runner_id,),
                    ).fetchone()[0]
                    if int(pending_count) >= MAX_PENDING_COMMANDS_PER_RUNNER:
                        raise ValueError("runner pending command quota is exhausted")
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO workspace_runner_commands(
                        command_id, runner_id, frame_json, state, created_at, updated_at
                    ) VALUES (?, ?, ?, 'queued', ?, ?)
                    """,
                    (command_id, runner_id, serialized, timestamp, timestamp),
                )
                if cursor.rowcount != 1:
                    if (
                        existing is None
                        or existing["runner_id"] != runner_id
                        or existing["frame_json"] != serialized
                    ):
                        raise ValueError("runner command id was reused with a different payload")
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def mark_command_sent(self, command_id: str, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE workspace_runner_commands SET state='sent',sent_at=?,updated_at=? "
                "WHERE command_id=? AND state='queued'",
                (timestamp, timestamp, command_id),
            )
            if cursor.rowcount == 1:
                return
            row = self._connection.execute(
                "SELECT state FROM workspace_runner_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if row is None or row["state"] not in {
                "sent",
                "acknowledged",
                "completed",
                "failed",
                "uncertain",
            }:
                raise ValueError("runner command is unavailable")

    # Backward-compatible alias for pre-ack callers.
    def mark_command_dispatched(self, command_id: str, *, now: float | None = None) -> None:
        self.mark_command_sent(command_id, now=now)

    def acknowledge_command(
        self,
        runner_id: str,
        command_id: str,
        *,
        ack_state: str,
        now: float | None = None,
    ) -> None:
        if ack_state not in {"accepted", "replayed"}:
            raise ValueError("runner command acknowledgement state is invalid")
        timestamp = time.time() if now is None else now
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE workspace_runner_commands "
                "SET state='acknowledged',acknowledged_at=?,updated_at=? "
                "WHERE runner_id=? AND command_id=? AND state IN ('queued','sent','acknowledged')",
                (timestamp, timestamp, runner_id, command_id),
            )
            if cursor.rowcount == 1:
                return
            row = self._connection.execute(
                "SELECT state FROM workspace_runner_commands WHERE runner_id=? AND command_id=?",
                (runner_id, command_id),
            ).fetchone()
            if row is None or row["state"] not in {"completed", "failed", "uncertain"}:
                raise ValueError("runner command is unavailable")

    def pending_commands(self, runner_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT frame_json FROM workspace_runner_commands "
                "WHERE runner_id=? AND state IN ('queued','sent','acknowledged') "
                "ORDER BY created_at, command_id",
                (runner_id,),
            ).fetchall()
        return [json.loads(row["frame_json"]) for row in rows]

    def complete_command(
        self,
        runner_id: str,
        command_id: str,
        *,
        result: dict[str, Any],
        now: float | None = None,
    ) -> None:
        self._reject_absolute_paths(result)
        serialized = self._json(result)
        if len(serialized.encode("utf-8")) > MAX_COMMAND_RESULT_BYTES:
            raise ValueError("runner command result exceeds the durable size limit")
        nested = result.get("result")
        is_uncertain = (
            result.get("state") == "uncertain"
            or result.get("uncertain") is True
            or (
                isinstance(nested, dict)
                and (nested.get("state") == "uncertain" or nested.get("uncertain") is True)
            )
        )
        reported_state = result.get("state")
        if reported_state in {"completed", "failed", "uncertain", "canceled", "revoked"}:
            state = str(reported_state)
        else:
            state = (
                "uncertain"
                if is_uncertain
                else ("completed" if result.get("ok") is True else "failed")
            )
        if (state == "completed") != (result.get("ok") is True):
            raise ValueError("runner command result state conflicts with ok")
        timestamp = time.time() if now is None else now
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT runner_id, state, result_json FROM workspace_runner_commands WHERE command_id=?",
                    (command_id,),
                ).fetchone()
                if row is None or row["runner_id"] != runner_id:
                    raise ValueError("runner command is unknown")
                if row["state"] in {"completed", "failed", "uncertain", "canceled", "revoked"}:
                    if row["state"] != state or row["result_json"] != serialized:
                        raise ValueError("runner command result conflicts with its terminal state")
                    self._connection.execute("COMMIT")
                    return
                self._connection.execute(
                    "UPDATE workspace_runner_commands SET state=?, result_json=?, updated_at=? "
                    "WHERE command_id=?",
                    (state, serialized, timestamp, command_id),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def command_status(self, runner_id: str, command_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT state,result_json,sent_at,acknowledged_at,created_at,updated_at "
                "FROM workspace_runner_commands WHERE runner_id=? AND command_id=?",
                (runner_id, command_id),
            ).fetchone()
            reconciliation = self._connection.execute(
                "SELECT decision, outcome, replacement_command_id, updated_at "
                "FROM workspace_runner_command_reconciliations WHERE command_id=?",
                (command_id,),
            ).fetchone()
        if row is None:
            raise ValueError("runner command is unknown")
        status = {
            "command_id": command_id,
            "acknowledged_at": row["acknowledged_at"],
            "created_at": row["created_at"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "state": row["state"],
            "sent_at": row["sent_at"],
            "updated_at": row["updated_at"],
        }
        if reconciliation is not None:
            status["reconciliation"] = {
                "decision": reconciliation["decision"],
                "outcome": reconciliation["outcome"],
                "replacement_command_id": reconciliation["replacement_command_id"],
                "updated_at": reconciliation["updated_at"],
            }
        return status

    def command_frame(self, runner_id: str, command_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT frame_json FROM workspace_runner_commands WHERE runner_id=? AND command_id=?",
                (runner_id, command_id),
            ).fetchone()
        if row is None:
            raise ValueError("runner command is unknown")
        return json.loads(row["frame_json"])

    def begin_reconciliation(
        self,
        runner_id: str,
        command_id: str,
        *,
        decision: str,
        now: float | None = None,
    ) -> None:
        if decision != "retry":
            raise ValueError("runner reconciliation decision is invalid")
        timestamp = time.time() if now is None else now
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._connection.execute(
                    "UPDATE workspace_runner_commands SET state='reconciling', updated_at=? "
                    "WHERE runner_id=? AND command_id=? AND state='uncertain'",
                    (timestamp, runner_id, command_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("runner command is not uncertain")
                self._connection.execute(
                    "INSERT INTO workspace_runner_command_reconciliations("
                    "command_id, decision, outcome, replacement_command_id, updated_at"
                    ") VALUES (?, ?, NULL, NULL, ?)",
                    (command_id, decision, timestamp),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def finish_reconciliation(
        self,
        runner_id: str,
        command_id: str,
        *,
        outcome: str,
        replacement_command_id: str | None,
        now: float | None = None,
    ) -> None:
        if outcome not in {"resumed", "failed"}:
            raise ValueError("runner reconciliation outcome is invalid")
        if outcome == "resumed" and not replacement_command_id:
            raise ValueError("resumed reconciliation requires a replacement command")
        timestamp = time.time() if now is None else now
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._connection.execute(
                    "UPDATE workspace_runner_commands SET state=?, updated_at=? "
                    "WHERE runner_id=? AND command_id=? AND state='reconciling'",
                    (outcome, timestamp, runner_id, command_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("runner command is not reconciling")
                self._connection.execute(
                    "UPDATE workspace_runner_command_reconciliations "
                    "SET outcome=?, replacement_command_id=?, updated_at=? WHERE command_id=?",
                    (outcome, replacement_command_id, timestamp, command_id),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def abandon_command(
        self,
        runner_id: str,
        command_id: str,
        *,
        now: float | None = None,
    ) -> None:
        timestamp = time.time() if now is None else now
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._connection.execute(
                    "UPDATE workspace_runner_commands SET state='abandoned', updated_at=? "
                    "WHERE runner_id=? AND command_id=? AND state='uncertain'",
                    (timestamp, runner_id, command_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("runner command is not uncertain")
                self._connection.execute(
                    "INSERT INTO workspace_runner_command_reconciliations("
                    "command_id, decision, outcome, replacement_command_id, updated_at"
                    ") VALUES (?, 'abandon', 'abandoned', NULL, ?)",
                    (command_id, timestamp),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def sync_bindings(self, runner_id: str, bindings: list[dict[str, Any]]) -> None:
        if len(bindings) > MAX_BINDINGS_PER_RUNNER:
            raise ValueError("runner binding inventory exceeds the limit")
        allowed = {"binding_id", "label", "project_id", "revoked"}
        normalized: list[tuple[str, str, str, int]] = []
        for binding in bindings:
            extras = set(binding) - allowed
            if extras or any("path" in key.lower() or "cwd" in key.lower() for key in binding):
                raise ValueError("runner binding payload must not contain a local path")
            binding_id = str(binding.get("binding_id") or "").strip()
            project_id = str(binding.get("project_id") or "").strip()
            label = str(binding.get("label") or "").strip()
            if not binding_id or not project_id or not label:
                raise ValueError("runner binding is incomplete")
            if len(binding_id) > 128 or len(project_id) > 128 or len(label) > 200:
                raise ValueError("runner binding field exceeds the limit")
            normalized.append((binding_id, project_id, label, int(bool(binding.get("revoked")))))

        timestamp = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT revoked FROM workspace_runners WHERE runner_id=?",
                    (runner_id,),
                ).fetchone()
                if row is None or bool(row["revoked"]):
                    raise ValueError("runner is unknown or revoked")
                for binding_id, project_id, label, revoked in normalized:
                    self._connection.execute(
                        """
                        INSERT INTO workspace_runner_bindings(
                            runner_id, binding_id, project_id, label, revoked, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(runner_id, binding_id) DO UPDATE SET
                            project_id=excluded.project_id,
                            label=excluded.label,
                            revoked=excluded.revoked,
                            updated_at=excluded.updated_at
                        """,
                        (runner_id, binding_id, project_id, label, revoked, timestamp),
                    )
                self._connection.execute(
                    "UPDATE workspace_runners SET last_seen=? WHERE runner_id=?",
                    (timestamp, runner_id),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def require_binding(self, runner_id: str, binding_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT binding_id, project_id, label, revoked "
                "FROM workspace_runner_bindings WHERE runner_id=? AND binding_id=?",
                (runner_id, binding_id),
            ).fetchone()
            runner = self._connection.execute(
                "SELECT revoked FROM workspace_runners WHERE runner_id=?",
                (runner_id,),
            ).fetchone()
        if (
            row is None
            or runner is None
            or bool(row["revoked"])
            or bool(runner["revoked"])
        ):
            raise ValueError("runner binding is unknown or revoked")
        return {
            "binding_id": row["binding_id"],
            "label": row["label"],
            "project_id": row["project_id"],
        }

    def list_runners(self, *, now: float | None = None, online_ttl: float = 90) -> list[dict[str, Any]]:
        timestamp = time.time() if now is None else now
        with self._lock:
            runners = self._connection.execute(
                "SELECT runner_id, label, revoked, last_seen, capabilities_json "
                "FROM workspace_runners ORDER BY created_at"
            ).fetchall()
            result = []
            for runner in runners:
                bindings = self._connection.execute(
                    "SELECT binding_id, project_id, label, revoked FROM workspace_runner_bindings "
                    "WHERE runner_id=? ORDER BY binding_id",
                    (runner["runner_id"],),
                ).fetchall()
                online = bool(
                    not runner["revoked"]
                    and runner["last_seen"] is not None
                    and timestamp - float(runner["last_seen"]) <= online_ttl
                )
                result.append(
                    {
                        "bindings": [
                            {
                                "binding_id": binding["binding_id"],
                                "label": binding["label"],
                                "project_id": binding["project_id"],
                                "status": "online" if online and not binding["revoked"] else "offline",
                            }
                            for binding in bindings
                        ],
                        "capabilities": json.loads(str(runner["capabilities_json"] or "[]")),
                        "label": runner["label"],
                        "last_seen": runner["last_seen"],
                        "revoked": bool(runner["revoked"]),
                        "runner_id": runner["runner_id"],
                        "status": "online" if online else "offline",
                    }
                )
        return result
