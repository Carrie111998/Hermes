"""Delegation ledger — durable SQLite authority for the DDP control plane.

Canonical DB: ~/.hermes/devflow/delegation_ledger.db (cross-profile, resolved
via events.paths.delegation_ledger_path by callers). Mirrors the EventBus WAL
PRAGMAs (SR-446 / ADR-0018) — do NOT lower wal_autocheckpoint below 1000.

The ledger is the lifecycle/dedup authority; mailbox envelopes are durable
evidence; EventBus is trigger/telemetry only.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from devflow_delegation.contract import WorkRequest, parse_request

# Terminal states close a lifecycle (no forward transitions). MERGED is NOT
# terminal: it proceeds to DEPLOYING (or REVERT_REQUESTED).
TERMINAL_STATES = frozenset(
    {"DEPLOYED", "DECLINED", "DUPLICATE", "SUPPRESSED", "CANCELLED", "FAILED", "REVERTED"}
)
# Successful terminals whose fingerprint re-opens are cooldown-gated as
# "success cooldown" (distinct from the declined cooldown).
SUCCESS_TERMINAL_STATES = frozenset({"MERGED", "AUTO_MERGED", "DEPLOYED"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    fingerprint TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'REQUESTED',
    terminal_reason TEXT,
    source_agent TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    target_repo TEXT NOT NULL,
    target_subsystem TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_fingerprint ON requests(fingerprint);
CREATE INDEX IF NOT EXISTS idx_requests_state ON requests(state);
CREATE INDEX IF NOT EXISTS idx_requests_source ON requests(source_agent);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES requests(request_id),
    from_state TEXT,
    to_state TEXT NOT NULL,
    actor TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    evidence_ref TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES requests(request_id),
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (
    request_id TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL,
    holder TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    heartbeat_at TEXT
);
"""

_TERMINAL_SQL = ",".join("?" for _ in TERMINAL_STATES)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DelegationLedger:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10)
            # Same WAL tuning as events.bus.EventBus (SR-446 / ADR-0018).
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA journal_size_limit=33554432")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        conn = self._conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    def close(self) -> None:
        """Release the calling thread's connection so the db file can be
        deleted or handed to a fresh reader. WAL keeps an exclusive OS handle
        on the file while open, which blocks unlink on Windows (WinError 32);
        the reconcile hand-off (a new emitter re-opening the same path) needs
        this. Idempotent — a later query lazily reconnects via _conn()."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------ write
    def insert_request(self, req: WorkRequest) -> None:
        now = _now_iso()
        conn = self._conn()
        try:
            conn.execute(
                """INSERT INTO requests
                   (request_id, idempotency_key, fingerprint, envelope_json, state,
                    terminal_reason, source_agent, source_kind, target_repo,
                    target_subsystem, kind, severity, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    req.request_id, req.idempotency_key, req.dedup_fingerprint,
                    json.dumps(req.to_envelope(), ensure_ascii=False), "REQUESTED", None,
                    req.source_agent, req.source_kind, req.target_repo,
                    req.target_subsystem, req.kind, req.severity, req.created_at, now,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def adopt_envelope(self, env: Dict[str, Any]) -> None:
        """Reconciler path: adopt an envelope dict (v3) already carrying its
        request_id, without minting a new identity."""
        req = parse_request(env)
        # parse_request re-mints identity/timestamp; pin the envelope's own so an
        # aged envelope (crash recovery) keeps its original creation time and id.
        if req.request_id != env.get("request_id"):
            req.request_id = env["request_id"]
        if env.get("created_at"):
            req.created_at = env["created_at"]
        self.insert_request(req)

    def append_evidence(self, request_id: str, evidence: Dict[str, Any]) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT INTO evidence_log (request_id, evidence_json, created_at) VALUES (?,?,?)",
            (request_id, json.dumps(evidence, ensure_ascii=False), _now_iso()),
        )
        conn.execute("UPDATE requests SET updated_at=? WHERE request_id=?", (_now_iso(), request_id))
        conn.commit()

    def set_state(self, request_id: str, state: str, terminal_reason: Optional[str] = None) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE requests SET state=?, terminal_reason=?, updated_at=? WHERE request_id=?",
            (state, terminal_reason, _now_iso(), request_id),
        )
        conn.commit()

    def record_transition(
        self,
        request_id: str,
        from_state: Optional[str],
        to_state: str,
        actor: str,
        policy_version: str,
        evidence_ref: Optional[str] = None,
    ) -> None:
        conn = self._conn()
        conn.execute(
            """INSERT INTO transitions
               (request_id, from_state, to_state, actor, policy_version, evidence_ref, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (request_id, from_state, to_state, actor, policy_version, evidence_ref, _now_iso()),
        )
        conn.commit()

    # ------------------------------------------------------------------- read
    @staticmethod
    def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        return dict(row) if row is not None else None

    def get_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM requests WHERE request_id=?", (request_id,)
        ).fetchone()
        return self._row_to_dict(row)

    def find_by_idempotency_key(self, key: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM requests WHERE idempotency_key=?", (key,)
        ).fetchone()
        return self._row_to_dict(row)

    def find_active_by_fingerprint(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        sql = (
            f"SELECT * FROM requests WHERE fingerprint=? AND state NOT IN ({_TERMINAL_SQL}) "
            "ORDER BY created_at DESC LIMIT 1"
        )
        row = self._conn().execute(sql, (fingerprint, *sorted(TERMINAL_STATES))).fetchone()
        return self._row_to_dict(row)

    def latest_terminal_for_fingerprint(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        sql = (
            f"SELECT * FROM requests WHERE fingerprint=? AND state IN ({_TERMINAL_SQL}) "
            "ORDER BY updated_at DESC LIMIT 1"
        )
        row = self._conn().execute(sql, (fingerprint, *sorted(TERMINAL_STATES))).fetchone()
        return self._row_to_dict(row)

    def evidence_count(self, request_id: str) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM evidence_log WHERE request_id=?", (request_id,)
        ).fetchone()
        return int(row["n"])

    def transitions_for(self, request_id: str) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM transitions WHERE request_id=? ORDER BY id", (request_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def count_since(self, source_agent: Optional[str], since_iso: str) -> int:
        if source_agent is None:
            row = self._conn().execute(
                "SELECT COUNT(*) AS n FROM requests WHERE created_at>=?", (since_iso,)
            ).fetchone()
        else:
            row = self._conn().execute(
                "SELECT COUNT(*) AS n FROM requests WHERE source_agent=? AND created_at>=?",
                (source_agent, since_iso),
            ).fetchone()
        return int(row["n"])

    def count_critical_since(self, since_iso: str) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM requests WHERE severity='critical' AND created_at>=?",
            (since_iso,),
        ).fetchone()
        return int(row["n"])

    def list_requests(self, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        if state is None:
            rows = self._conn().execute(
                "SELECT * FROM requests ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM requests WHERE state=? ORDER BY created_at DESC LIMIT ?",
                (state, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def summary_counts(self) -> Dict[str, Any]:
        conn = self._conn()
        by_state = {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, COUNT(*) AS n FROM requests GROUP BY state")}
        by_source = {r["source_agent"]: r["n"] for r in conn.execute(
            "SELECT source_agent, COUNT(*) AS n FROM requests GROUP BY source_agent")}
        total = conn.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"]
        return {"by_state": by_state, "by_source": by_source, "total": int(total)}
