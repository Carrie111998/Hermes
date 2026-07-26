"""Durable approval inbox and target-scoped standing approval rules."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from hermes_constants import get_hermes_home
from tools.governance import RiskClass, ToolCallEnvelope, risk_at_most


_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_requests (
    id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL,
    risk_class TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    args_digest TEXT NOT NULL,
    args_preview TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    pattern_key TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'tool',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    decided_at REAL,
    decision_reason TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL DEFAULT '',
    consumed_at REAL,
    envelope_json TEXT NOT NULL DEFAULT '',
    envelope_sha256 TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_approval_requests_status_created
    ON approval_requests(status, created_at);
CREATE INDEX IF NOT EXISTS idx_approval_requests_exact_call
    ON approval_requests(session_key, tool_name, args_digest, pattern_key, status);

CREATE TABLE IF NOT EXISTS standing_approval_rules (
    id TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    target_pattern TEXT NOT NULL,
    match_mode TEXT NOT NULL DEFAULT 'exact',
    risk_ceiling TEXT NOT NULL,
    operation TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL DEFAULT '',
    workspace TEXT NOT NULL DEFAULT '',
    job_id TEXT NOT NULL DEFAULT '',
    expires_at REAL,
    max_uses INTEGER,
    use_count INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    last_used_at REAL
);
CREATE INDEX IF NOT EXISTS idx_standing_approval_rules_tool_enabled
    ON standing_approval_rules(tool_name, enabled);
"""


class ApprovalStore:
    """Small SQLite facade intentionally independent of ``SessionDB`` lifetimes."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else get_hermes_home() / "state.db"
        self.clock = clock
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(standing_approval_rules)").fetchall()
            }
            if "match_mode" not in columns:
                conn.execute(
                    "ALTER TABLE standing_approval_rules "
                    "ADD COLUMN match_mode TEXT NOT NULL DEFAULT 'exact'"
                )
            request_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(approval_requests)").fetchall()
            }
            if "envelope_json" not in request_columns:
                conn.execute(
                    "ALTER TABLE approval_requests "
                    "ADD COLUMN envelope_json TEXT NOT NULL DEFAULT ''"
                )
            if "envelope_sha256" not in request_columns:
                conn.execute(
                    "ALTER TABLE approval_requests "
                    "ADD COLUMN envelope_sha256 TEXT NOT NULL DEFAULT ''"
                )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @staticmethod
    def _row(row: sqlite3.Row | None) -> Optional[dict]:
        return dict(row) if row is not None else None

    @staticmethod
    def _approval_envelope_json(data: dict) -> str:
        immutable = {
            key: data[key]
            for key in (
                "id", "session_key", "tool_name", "risk_class", "target",
                "args_digest", "args_preview", "reason", "pattern_key", "source",
                "created_at", "expires_at",
            )
        }
        return json.dumps(
            {"schema_version": 1, **immutable},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def _request_row(
        self,
        row: sqlite3.Row | None,
        *,
        require_integrity: bool = False,
    ) -> Optional[dict]:
        if row is None:
            return None
        data = dict(row)
        envelope_json = data.get("envelope_json", "")
        stored_digest = data.get("envelope_sha256", "")
        expected_json = self._approval_envelope_json(data)
        expected_digest = hashlib.sha256(expected_json.encode("utf-8")).hexdigest()
        integrity_ok = bool(envelope_json and stored_digest) and hmac.compare_digest(
            envelope_json.encode("utf-8"), expected_json.encode("utf-8")
        ) and hmac.compare_digest(stored_digest, expected_digest)
        data.pop("envelope_json", None)
        data["integrity_ok"] = integrity_ok
        if require_integrity and not integrity_ok:
            raise ValueError(f"Approval {data['id']} failed envelope integrity verification")
        return data

    def _expire(self, conn: sqlite3.Connection, now: float) -> None:
        conn.execute(
            "UPDATE approval_requests SET status='expired' "
            "WHERE status IN ('pending', 'approved') AND expires_at <= ?",
            (now,),
        )

    def create_request(
        self,
        *,
        session_key: str,
        envelope: ToolCallEnvelope,
        reason: str,
        pattern_key: str,
        source: str = "tool",
        expires_in: float = 86400.0,
    ) -> dict:
        now = float(self.clock())
        expires_at = now + max(float(expires_in), 1.0)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire(conn, now)
            existing = conn.execute(
                "SELECT * FROM approval_requests WHERE session_key=? AND tool_name=? "
                "AND args_digest=? AND pattern_key=? AND status='pending' "
                "AND expires_at>? ORDER BY created_at DESC LIMIT 1",
                (
                    session_key,
                    envelope.tool_name,
                    envelope.args_digest,
                    pattern_key,
                    now,
                ),
            ).fetchone()
            if existing is not None:
                existing_data = self._request_row(existing)
                if existing_data and existing_data["integrity_ok"]:
                    conn.commit()
                    return existing_data
            request_id = uuid.uuid4().hex
            immutable = {
                "id": request_id,
                "session_key": session_key,
                "tool_name": envelope.tool_name,
                "risk_class": envelope.risk_class,
                "target": envelope.target,
                "args_digest": envelope.args_digest,
                "args_preview": envelope.args_preview,
                "reason": reason,
                "pattern_key": pattern_key,
                "source": source,
                "created_at": now,
                "expires_at": expires_at,
            }
            envelope_json = self._approval_envelope_json(immutable)
            envelope_sha256 = hashlib.sha256(envelope_json.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO approval_requests "
                "(id, session_key, tool_name, risk_class, target, args_digest, "
                "args_preview, reason, pattern_key, source, status, created_at, expires_at, "
                "envelope_json, envelope_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    request_id,
                    session_key,
                    envelope.tool_name,
                    envelope.risk_class,
                    envelope.target,
                    envelope.args_digest,
                    envelope.args_preview,
                    reason,
                    pattern_key,
                    source,
                    now,
                    expires_at,
                    envelope_json,
                    envelope_sha256,
                ),
            )
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE id=?", (request_id,)
            ).fetchone()
            conn.commit()
            verified = self._request_row(row, require_integrity=True)
            assert verified is not None
            return verified

    def get_request(self, request_id: str) -> Optional[dict]:
        now = float(self.clock())
        with self._connect() as conn:
            self._expire(conn, now)
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE id=?", (request_id,)
            ).fetchone()
            return self._request_row(row)

    def list_requests(
        self,
        *,
        status: str | None = None,
        session_key: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        now = float(self.clock())
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if session_key:
            clauses.append("session_key=?")
            params.append(session_key)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        with self._connect() as conn:
            self._expire(conn, now)
            rows = conn.execute(
                f"SELECT * FROM approval_requests{where} ORDER BY created_at ASC LIMIT ?",
                params,
            ).fetchall()
            return [data for row in rows if (data := self._request_row(row)) is not None]

    def resolve_request(
        self,
        request_id: str,
        decision: str,
        *,
        decision_reason: str = "",
        decided_by: str = "operator",
    ) -> dict:
        normalized = str(decision).strip().lower()
        if normalized in {"approve", "approved", "allow", "once"}:
            status = "approved"
        elif normalized in {"deny", "denied", "reject"}:
            status = "denied"
        else:
            raise ValueError("decision must be approved or denied")
        now = float(self.clock())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire(conn, now)
            current = conn.execute(
                "SELECT * FROM approval_requests WHERE id=?", (request_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"Approval not found: {request_id}")
            self._request_row(current, require_integrity=True)
            if current["status"] != "pending":
                raise ValueError(
                    f"Approval {request_id} is already {current['status']}"
                )
            conn.execute(
                "UPDATE approval_requests SET status=?, decided_at=?, "
                "decision_reason=?, decided_by=? WHERE id=?",
                (status, now, decision_reason, decided_by, request_id),
            )
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE id=?", (request_id,)
            ).fetchone()
            conn.commit()
            verified = self._request_row(row, require_integrity=True)
            assert verified is not None
            return verified

    def consume_matching_approval(
        self,
        envelope: ToolCallEnvelope,
        *,
        pattern_key: str,
        session_key: str,
    ) -> Optional[dict]:
        now = float(self.clock())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire(conn, now)
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE session_key=? AND tool_name=? "
                "AND args_digest=? AND target=? AND risk_class=? AND pattern_key=? "
                "AND status='approved' AND expires_at>? "
                "ORDER BY decided_at ASC LIMIT 1",
                (
                    session_key,
                    envelope.tool_name,
                    envelope.args_digest,
                    envelope.target,
                    envelope.risk_class,
                    pattern_key,
                    now,
                ),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            self._request_row(row, require_integrity=True)
            conn.execute(
                "UPDATE approval_requests SET status='consumed', consumed_at=? "
                "WHERE id=? AND status='approved'",
                (now, row["id"]),
            )
            consumed = conn.execute(
                "SELECT * FROM approval_requests WHERE id=?", (row["id"],)
            ).fetchone()
            conn.commit()
            verified = self._request_row(consumed, require_integrity=True)
            assert verified is not None
            return verified

    def resolve_request_with_standing_rule(
        self,
        request_id: str,
        *,
        expires_at: float,
        max_uses: int,
        decision_reason: str = "",
        decided_by: str = "operator",
        note: str = "",
    ) -> tuple[dict, dict]:
        """Atomically approve a pending request and create its exact bounded rule."""
        if int(max_uses) < 1:
            raise ValueError("max_uses must be at least 1")
        now = float(self.clock())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire(conn, now)
            current = conn.execute(
                "SELECT * FROM approval_requests WHERE id=?", (request_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"Approval not found: {request_id}")
            self._request_row(current, require_integrity=True)
            if current["status"] != "pending":
                raise ValueError(f"Approval {request_id} is already {current['status']}")
            if not current["target"]:
                raise ValueError("Targetless approvals cannot become standing rules")

            rule_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO standing_approval_rules "
                "(id, tool_name, target_pattern, match_mode, risk_ceiling, expires_at, "
                "max_uses, note, created_at) VALUES (?, ?, ?, 'exact', ?, ?, ?, ?, ?)",
                (
                    rule_id,
                    current["tool_name"],
                    current["target"],
                    current["risk_class"],
                    float(expires_at),
                    int(max_uses),
                    note,
                    now,
                ),
            )
            conn.execute(
                "UPDATE approval_requests SET status='approved', decided_at=?, "
                "decision_reason=?, decided_by=? WHERE id=? AND status='pending'",
                (now, decision_reason, decided_by, request_id),
            )
            resolved = conn.execute(
                "SELECT * FROM approval_requests WHERE id=?", (request_id,)
            ).fetchone()
            rule = conn.execute(
                "SELECT * FROM standing_approval_rules WHERE id=?", (rule_id,)
            ).fetchone()
            conn.commit()
            verified = self._request_row(resolved, require_integrity=True)
            assert verified is not None
            return verified, dict(rule)

    def add_standing_rule(
        self,
        *,
        tool_name: str,
        target_pattern: str,
        risk_ceiling: RiskClass | str,
        operation: str = "",
        profile: str = "",
        workspace: str = "",
        job_id: str = "",
        expires_at: float | None = None,
        max_uses: int | None = None,
        note: str = "",
        match_mode: str = "exact",
    ) -> dict:
        if not tool_name.strip():
            raise ValueError("tool_name is required")
        if not target_pattern.strip():
            raise ValueError("target_pattern is required")
        if max_uses is not None and int(max_uses) < 1:
            raise ValueError("max_uses must be at least 1")
        if match_mode != "exact":
            raise ValueError("standing approval rules must use exact target matching")
        now = float(self.clock())
        rule_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO standing_approval_rules "
                "(id, tool_name, target_pattern, match_mode, risk_ceiling, operation, profile, "
                "workspace, job_id, expires_at, max_uses, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rule_id,
                    tool_name,
                    target_pattern,
                    match_mode,
                    RiskClass.parse(risk_ceiling).value,
                    operation,
                    profile,
                    workspace,
                    job_id,
                    expires_at,
                    int(max_uses) if max_uses is not None else None,
                    note,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM standing_approval_rules WHERE id=?", (rule_id,)
            ).fetchone()
            return dict(row)

    def consume_standing_rule(
        self,
        envelope: ToolCallEnvelope,
        *,
        operation: str = "",
        profile: str = "",
        workspace: str = "",
        job_id: str = "",
    ) -> Optional[dict]:
        now = float(self.clock())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            candidates = conn.execute(
                "SELECT * FROM standing_approval_rules WHERE enabled=1 "
                "AND tool_name=? AND (expires_at IS NULL OR expires_at>?) "
                "AND (max_uses IS NULL OR use_count<max_uses) "
                "ORDER BY created_at ASC",
                (envelope.tool_name, now),
            ).fetchall()
            for row in candidates:
                if row["operation"] and row["operation"] != operation:
                    continue
                if row["profile"] and row["profile"] != profile:
                    continue
                if row["workspace"] and row["workspace"] != workspace:
                    continue
                if row["job_id"] and row["job_id"] != job_id:
                    continue
                if row["match_mode"] != "exact":
                    continue
                target_matches = envelope.target == row["target_pattern"]
                if not target_matches:
                    continue
                if not risk_at_most(envelope.risk_class, row["risk_ceiling"]):
                    continue
                conn.execute(
                    "UPDATE standing_approval_rules SET use_count=use_count+1, "
                    "last_used_at=? WHERE id=? AND enabled=1",
                    (now, row["id"]),
                )
                used = conn.execute(
                    "SELECT * FROM standing_approval_rules WHERE id=?", (row["id"],)
                ).fetchone()
                conn.commit()
                return dict(used)
            conn.commit()
            return None

    def list_standing_rules(self, *, active_only: bool = False) -> list[dict]:
        now = float(self.clock())
        where = ""
        params: tuple[object, ...] = ()
        if active_only:
            where = (
                " WHERE enabled=1 AND (expires_at IS NULL OR expires_at>?) "
                "AND (max_uses IS NULL OR use_count<max_uses)"
            )
            params = (now,)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM standing_approval_rules" + where + " ORDER BY created_at ASC",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def revoke_standing_rule(self, rule_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE standing_approval_rules SET enabled=0 WHERE id=? AND enabled=1",
                (rule_id,),
            )
            return cursor.rowcount == 1
