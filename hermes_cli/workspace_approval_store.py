"""Durable public approval queue for Project Workspace clients."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_PUBLIC_REQUEST_FIELDS = (
    "changeSetDigest",
    "commitSha",
    "createdAt",
    "destinationBranch",
    "expiresAt",
    "remote",
    "remoteUrl",
    "remoteUrlDigest",
    "requestId",
)


def _json(value: Any) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _timestamp(value: str) -> float:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("approval timestamp must include a timezone")
    return parsed.timestamp()


class WorkspaceApprovalStore:
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
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspace_approvals (
                request_id TEXT PRIMARY KEY,
                project_id TEXT,
                binding_id TEXT,
                request_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                decision_json TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_approvals_pending
                ON workspace_approvals(status, expires_at, created_at);
            """
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def publish(
        self,
        request: dict[str, Any],
        *,
        binding_id: str | None,
        project_id: str | None,
    ) -> dict[str, Any]:
        public = {field: request.get(field) for field in _PUBLIC_REQUEST_FIELDS}
        if any(public[field] in (None, "") for field in _PUBLIC_REQUEST_FIELDS):
            raise ValueError("approval request is incomplete")
        request_id = str(public["requestId"])
        created_at = _timestamp(str(public["createdAt"]))
        expires_at = _timestamp(str(public["expiresAt"]))
        now = time.time()
        serialized = _json(public)
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO workspace_approvals(
                    request_id, project_id, binding_id, request_json,
                    created_at, expires_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, project_id, binding_id, serialized, created_at, expires_at, now),
            )
            if cursor.rowcount == 0:
                row = self._connection.execute(
                    "SELECT request_json, project_id, binding_id FROM workspace_approvals WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if (
                    row is None
                    or row["request_json"] != serialized
                    or row["project_id"] != project_id
                    or row["binding_id"] != binding_id
                ):
                    raise ValueError("approval request id was reused")
        return public

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "binding_id": row["binding_id"],
            "decision": json.loads(row["decision_json"]) if row["decision_json"] else None,
            "error": row["error"],
            "project_id": row["project_id"],
            "request": json.loads(row["request_json"]),
            "status": row["status"],
            "updated_at": row["updated_at"],
        }

    def list_pending(self, *, now: float | None = None) -> list[dict[str, Any]]:
        timestamp = time.time() if now is None else now
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM workspace_approvals "
                "WHERE status='pending' AND expires_at>? ORDER BY created_at ASC",
                (timestamp,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def decide(
        self,
        request_id: str,
        *,
        approved: bool,
        approved_by: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if now is None else now
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM workspace_approvals WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("approval request is unknown")
                if row["status"] != "pending":
                    raise ValueError("approval request was already decided")
                if float(row["expires_at"]) <= timestamp:
                    self._connection.execute(
                        "UPDATE workspace_approvals SET status='expired', updated_at=? WHERE request_id=?",
                        (timestamp, request_id),
                    )
                    raise ValueError("approval request expired")
                request = json.loads(row["request_json"])
                decision = {
                    **request,
                    "approved": bool(approved),
                    "approvedBy": approved_by,
                    "decidedAt": datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z"),
                }
                self._connection.execute(
                    "UPDATE workspace_approvals SET status=?, decision_json=?, updated_at=? WHERE request_id=?",
                    (
                        "approved" if approved else "denied",
                        _json(decision),
                        timestamp,
                        request_id,
                    ),
                )
                self._connection.execute("COMMIT")
                return decision
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def mark_result(self, request_id: str, *, error: str | None = None) -> None:
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE workspace_approvals SET status=?, error=?, updated_at=? WHERE request_id=?",
                ("failed" if error else "completed", error, time.time(), request_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("approval request is unknown")
