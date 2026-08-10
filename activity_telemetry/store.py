from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any

from .schema import (
    LogicalActivityStart,
    OutcomeLayers,
    RouteUsageDelta,
    ServedRoute,
    derive_final_outcome,
)

ESCALATION_REASONS = frozenset({
    "quality_gate_failed",
    "conflicting_evidence",
    "low_source_confidence",
    "high_value_application",
    "executive_role",
    "security_sensitive",
    "architecture_cross_service",
    "test_failure_nonlocal",
    "provider_unavailable",
    "context_limit_pressure",
})

_RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS logical_activity_runs (
    run_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    activity_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    trigger_source TEXT NOT NULL,
    profile TEXT NOT NULL,
    effective_hermes_home TEXT NOT NULL,
    requested_provider TEXT,
    requested_model TEXT,
    session_id TEXT UNIQUE,
    parent_run_id TEXT,
    child_count INTEGER NOT NULL DEFAULT 0,
    child_depth INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    wall_time_ms INTEGER,
    process_outcome TEXT NOT NULL DEFAULT 'unknown',
    protocol_outcome TEXT NOT NULL DEFAULT 'unknown',
    artifact_outcome TEXT NOT NULL DEFAULT 'unknown',
    domain_outcome TEXT NOT NULL DEFAULT 'unknown',
    delivery_outcome TEXT NOT NULL DEFAULT 'unknown',
    final_outcome TEXT NOT NULL DEFAULT 'unknown',
    escalation_reason TEXT,
    evidence_refs_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS logical_activity_route_usage (
    run_id TEXT NOT NULL REFERENCES logical_activity_runs(run_id),
    served_provider TEXT NOT NULL,
    served_model TEXT NOT NULL,
    turns INTEGER NOT NULL DEFAULT 0,
    model_calls INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    uncached_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    recorded_provider_cost_usd TEXT,
    api_equivalent_cost_usd TEXT,
    PRIMARY KEY (run_id, served_provider, served_model)
);
"""

_COUNTER_FIELDS = (
    "turns",
    "model_calls",
    "tool_calls",
    "retries",
    "uncached_input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
)
_EVIDENCE_REF = re.compile(r"^[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _decimal_sum(existing: str | None, delta: Decimal | None) -> str | None:
    if delta is None:
        return existing
    return str((Decimal(existing) if existing is not None else Decimal(0)) + delta)


class ActivityStore:
    def __init__(
        self,
        db_path: Path,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._local = threading.local()
        self._write_lock = threading.Lock()
        conn = self._get_conn()
        with self._write_lock:
            try:
                conn.executescript(_RUN_SCHEMA)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.db_path,
                timeout=10,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA journal_size_limit=33554432")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
            self._local.conn = conn
        return conn

    def start(self, record: LogicalActivityStart) -> None:
        conn = self._get_conn()
        started_at = _utc(self.clock()).isoformat()
        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if record.parent_run_id is not None:
                    parent = conn.execute(
                        """
                        SELECT child_depth, finished_at
                        FROM logical_activity_runs WHERE run_id = ?
                        """,
                        (record.parent_run_id,),
                    ).fetchone()
                    if parent is None:
                        raise ValueError(f"parent run does not exist: {record.parent_run_id}")
                    if parent["finished_at"] is not None:
                        raise ValueError(f"parent run already finished: {record.parent_run_id}")
                    if record.child_depth != parent["child_depth"] + 1:
                        raise ValueError("child_depth must be parent child_depth plus one")
                conn.execute(
                    """
                    INSERT INTO logical_activity_runs (
                        run_id, correlation_id, activity_id, policy_version,
                        trigger_source, profile, effective_hermes_home,
                        requested_provider, requested_model, session_id,
                        parent_run_id, child_depth, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.run_id,
                        record.correlation_id,
                        record.activity_id,
                        record.policy_version,
                        record.trigger_source,
                        record.profile,
                        record.effective_hermes_home,
                        record.requested_provider,
                        record.requested_model,
                        record.session_id,
                        record.parent_run_id,
                        record.child_depth,
                        started_at,
                    ),
                )
                if record.parent_run_id is not None:
                    conn.execute(
                        """
                        UPDATE logical_activity_runs
                        SET child_count = child_count + 1
                        WHERE run_id = ?
                        """,
                        (record.parent_run_id,),
                    )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                if self.get_run(record.run_id) is not None:
                    raise ValueError(f"run already exists: {record.run_id}") from exc
                raise ValueError("session_id is already linked") from exc
            except Exception:
                conn.rollback()
                raise

    def link_session(self, run_id: str, session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be non-empty")
        conn = self._get_conn()
        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """
                    UPDATE logical_activity_runs
                    SET session_id = ?
                    WHERE run_id = ? AND session_id IS NULL AND finished_at IS NULL
                    """,
                    (session_id, run_id),
                )
                if cursor.rowcount == 0:
                    row = conn.execute(
                        "SELECT session_id, finished_at FROM logical_activity_runs WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()
                    if row is None:
                        raise KeyError(f"run missing: {run_id}")
                    if row["session_id"] is not None:
                        raise ValueError(f"session already linked: {run_id}")
                    raise ValueError(f"run already finished: {run_id}")
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise ValueError(f"session_id is already linked: {session_id}") from exc
            except Exception:
                conn.rollback()
                raise

    def record_usage(
        self,
        run_id: str,
        route: ServedRoute,
        delta: RouteUsageDelta,
    ) -> None:
        conn = self._get_conn()
        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                run = conn.execute(
                    "SELECT finished_at FROM logical_activity_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if run is None:
                    raise KeyError(f"run missing: {run_id}")
                if run["finished_at"] is not None:
                    raise ValueError(f"run already finished: {run_id}")
                existing = conn.execute(
                    """
                    SELECT recorded_provider_cost_usd, api_equivalent_cost_usd
                    FROM logical_activity_route_usage
                    WHERE run_id = ? AND served_provider = ? AND served_model = ?
                    """,
                    (run_id, route.provider, route.model),
                ).fetchone()
                recorded = _decimal_sum(
                    existing["recorded_provider_cost_usd"] if existing else None,
                    delta.recorded_provider_cost_usd,
                )
                equivalent = _decimal_sum(
                    existing["api_equivalent_cost_usd"] if existing else None,
                    delta.api_equivalent_cost_usd,
                )
                values = [getattr(delta, name) for name in _COUNTER_FIELDS]
                conn.execute(
                    """
                    INSERT INTO logical_activity_route_usage (
                        run_id, served_provider, served_model, turns, model_calls,
                        tool_calls, retries, uncached_input_tokens, cache_read_tokens,
                        cache_write_tokens, output_tokens, reasoning_tokens,
                        recorded_provider_cost_usd, api_equivalent_cost_usd
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, served_provider, served_model) DO UPDATE SET
                        turns = turns + excluded.turns,
                        model_calls = model_calls + excluded.model_calls,
                        tool_calls = tool_calls + excluded.tool_calls,
                        retries = retries + excluded.retries,
                        uncached_input_tokens = uncached_input_tokens + excluded.uncached_input_tokens,
                        cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,
                        cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens,
                        output_tokens = output_tokens + excluded.output_tokens,
                        reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens,
                        recorded_provider_cost_usd = excluded.recorded_provider_cost_usd,
                        api_equivalent_cost_usd = excluded.api_equivalent_cost_usd
                    """,
                    (run_id, route.provider, route.model, *values, recorded, equivalent),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def finish(
        self,
        run_id: str,
        layers: OutcomeLayers,
        evidence_refs: tuple[str, ...] = (),
        escalation_reason: str | None = None,
    ) -> str:
        if escalation_reason is not None and escalation_reason not in ESCALATION_REASONS:
            raise ValueError(f"invalid escalation_reason: {escalation_reason}")
        if any(
            not isinstance(ref, str) or _EVIDENCE_REF.fullmatch(ref) is None
            for ref in evidence_refs
        ):
            raise ValueError("evidence refs must be bounded opaque references")
        final_outcome = derive_final_outcome(layers)
        conn = self._get_conn()
        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT started_at, finished_at FROM logical_activity_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"run missing: {run_id}")
                if row["finished_at"] is not None:
                    raise ValueError(f"run already finished: {run_id}")
                finished = _utc(self.clock())
                started = datetime.fromisoformat(row["started_at"])
                wall_time_ms = max(0, round((finished - started).total_seconds() * 1000))
                cursor = conn.execute(
                    """
                    UPDATE logical_activity_runs SET
                        finished_at = ?, wall_time_ms = ?, process_outcome = ?,
                        protocol_outcome = ?, artifact_outcome = ?, domain_outcome = ?,
                        delivery_outcome = ?, final_outcome = ?, escalation_reason = ?,
                        evidence_refs_json = ?
                    WHERE run_id = ? AND finished_at IS NULL
                    """,
                    (
                        finished.isoformat(),
                        wall_time_ms,
                        layers.process,
                        layers.protocol,
                        layers.artifact,
                        layers.domain,
                        layers.delivery,
                        final_outcome,
                        escalation_reason,
                        json.dumps(evidence_refs),
                        run_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"run already finished: {run_id}")
                conn.commit()
                return final_outcome
            except Exception:
                conn.rollback()
                raise

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._get_conn().execute(
            "SELECT * FROM logical_activity_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_routes(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._get_conn().execute(
            """
            SELECT * FROM logical_activity_route_usage
            WHERE run_id = ? ORDER BY rowid
            """,
            (run_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for name in ("recorded_provider_cost_usd", "api_equivalent_cost_usd"):
                if item[name] is not None:
                    item[name] = Decimal(item[name])
            result.append(item)
        return result
