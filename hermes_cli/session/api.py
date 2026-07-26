"""BUSY-safe persistence and deterministic continuity handoff construction."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hermes_cli.session import schema
from hermes_cli.session.rotation_config import ROTATION_CAPS
from hermes_cli.sqlite_util import retrying_write_txn


logger = logging.getLogger(__name__)
_HANDOFF_OPEN = "<hermes:handoff>"
_HANDOFF_CLOSE = "</hermes:handoff>"
_ROTATION_REASONS = frozenset(
    {"soft_limit", "hard_limit", "manual", "error"}
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def open_session(
    *,
    task_id: str,
    lane: str,
    profile: Optional[str],
    route: Optional[str],
    parent_session_id: Optional[str] = None,
    db_path=None,
    session_id: Optional[str] = None,
) -> str:
    """Insert a shared-ledger session and return its identity.

    ``session_id`` is an optional compatibility hook for the runtime's
    pre-existing state.db session identity. Standalone callers receive UUID4.
    """
    schema.ensure_migrated(db_path)
    new_id = str(session_id or uuid.uuid4())
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            conn.execute(
                """
                INSERT INTO sessions (
                    id, task_id, parent_session_id, lane, profile, route,
                    opened_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    str(task_id),
                    (
                        str(parent_session_id)
                        if parent_session_id is not None
                        else None
                    ),
                    str(lane),
                    str(profile) if profile is not None else None,
                    str(route) if route is not None else None,
                    _utc_now(),
                ),
            )
    finally:
        conn.close()
    return new_id


def close_session(
    *,
    session_id: str,
    rotation_reason: str,
    token_count_at_close: int,
    handoff_summary_json: Optional[str] = None,
    db_path=None,
) -> None:
    """Close a session once; repeat closes are intentional no-ops."""
    reason = str(rotation_reason)
    if reason not in _ROTATION_REASONS:
        raise ValueError(f"invalid rotation reason: {reason!r}")
    token_count = int(token_count_at_close)
    if token_count < 0:
        raise ValueError("token_count_at_close must be non-negative")
    schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            row = conn.execute(
                "SELECT closed_ts FROM sessions WHERE id = ?",
                (str(session_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown session: {session_id}")
            if row["closed_ts"] is not None:
                logger.warning(
                    "Session %s is already closed; ignoring duplicate close",
                    session_id,
                )
                return
            conn.execute(
                """
                UPDATE sessions
                   SET closed_ts = ?,
                       rotation_reason = ?,
                       token_count_at_close = ?,
                       handoff_summary_json = ?
                 WHERE id = ? AND closed_ts IS NULL
                """,
                (
                    _utc_now(),
                    reason,
                    token_count,
                    handoff_summary_json,
                    str(session_id),
                ),
            )
    finally:
        conn.close()


def get_open_session_for_task(
    task_id: str,
    db_path=None,
) -> Optional[dict]:
    schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        return _row_dict(
            conn.execute(
                """
                SELECT *
                  FROM sessions
                 WHERE task_id = ? AND closed_ts IS NULL
                 ORDER BY opened_ts DESC, rowid DESC
                 LIMIT 1
                """,
                (str(task_id),),
            ).fetchone()
        )
    finally:
        conn.close()


def list_sessions_for_task(task_id: str, db_path=None) -> list[dict]:
    schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                  FROM sessions
                 WHERE task_id = ?
                 ORDER BY opened_ts ASC, rowid ASC
                """,
                (str(task_id),),
            ).fetchall()
        ]
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            """
            SELECT 1 FROM sqlite_master
             WHERE type = 'table' AND name = ?
            """,
            (name,),
        ).fetchone()
        is not None
    )


def _day_bounds() -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    tomorrow = datetime.combine(
        today,
        datetime.min.time(),
        tzinfo=timezone.utc,
    ).timestamp() + 86_400
    return (
        f"{today.isoformat()}T00:00:00Z",
        datetime.fromtimestamp(tomorrow, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    )


def _sum(
    conn: sqlite3.Connection,
    query: str,
    params: tuple[Any, ...] = (),
) -> float:
    row = conn.execute(query, params).fetchone()
    return float(row["total"] if row is not None and row["total"] else 0.0)


def _lineage_clause(conn: sqlite3.Connection, task_id: str) -> tuple[str, tuple]:
    """Match task attribution plus runtime session IDs registered to the task."""
    if not _table_exists(conn, "sessions"):
        return "task_id = ?", (task_id,)
    return (
        "(task_id = ? OR session_id IN "
        "(SELECT id FROM sessions WHERE task_id = ?))",
        (task_id, task_id),
    )


def build_handoff_summary(task_id: str, db_path=None) -> dict:
    """Extract bounded continuity state without a model call."""
    schema.ensure_migrated(db_path)
    task = str(task_id)
    conn = schema.connect(db_path)
    try:
        session = conn.execute(
            """
            SELECT * FROM sessions
             WHERE task_id = ?
             ORDER BY opened_ts DESC, rowid DESC
             LIMIT 1
            """,
            (task,),
        ).fetchone()
        lane = str(session["lane"]) if session is not None else "platform"

        verdicts: list[dict[str, Any]] = []
        current_rung = "r0_baseline"
        if _table_exists(conn, "leaf_verdicts"):
            clause, params = _lineage_clause(conn, task)
            rows = conn.execute(
                f"""
                SELECT rung_id, outcome, failure_class, model_used, confidence
                  FROM leaf_verdicts
                 WHERE {clause}
                 ORDER BY id DESC
                 LIMIT ?
                """,
                (*params, ROTATION_CAPS.max_recent_verdicts),
            ).fetchall()
            verdicts = [dict(row) for row in rows]
            if rows:
                current_rung = str(rows[0]["rung_id"])

        dispatch_hashes: list[str] = []
        if _table_exists(conn, "dispatch_envelopes"):
            clause, params = _lineage_clause(conn, task)
            rows = conn.execute(
                f"""
                SELECT strategy_hash, rung_id
                  FROM dispatch_envelopes
                 WHERE {clause}
                 ORDER BY id DESC
                 LIMIT ?
                """,
                (*params, ROTATION_CAPS.max_recent_dispatches),
            ).fetchall()
            dispatch_hashes = [str(row["strategy_hash"]) for row in rows]
            if not verdicts and rows:
                current_rung = str(rows[0]["rung_id"])

        active_effects: list[dict[str, Any]] = []
        if _table_exists(conn, "side_effects"):
            active_effects = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, lane, action_type, status, attempt_number
                      FROM side_effects
                     WHERE task_id = ?
                       AND status IN ('pending', 'in_flight')
                     ORDER BY id ASC
                    """,
                    (task,),
                ).fetchall()
            ]

        start, end = _day_bounds()
        task_spend = lane_spend = global_spend = escalation_spend = 0.0
        if _table_exists(conn, "cost_ledger"):
            task_spend = _sum(
                conn,
                "SELECT COALESCE(SUM(aud_amount),0) total "
                "FROM cost_ledger WHERE task_id = ?",
                (task,),
            )
            lane_spend = _sum(
                conn,
                "SELECT COALESCE(SUM(aud_amount),0) total "
                "FROM cost_ledger WHERE lane = ? AND ts >= ? AND ts < ?",
                (lane, start, end),
            )
            global_spend = _sum(
                conn,
                "SELECT COALESCE(SUM(aud_amount),0) total "
                "FROM cost_ledger WHERE ts >= ? AND ts < ?",
                (start, end),
            )
            escalation_spend = _sum(
                conn,
                "SELECT COALESCE(SUM(aud_amount),0) total "
                "FROM cost_ledger WHERE escalation = 1 AND ts >= ? AND ts < ?",
                (start, end),
            )

        from hermes_cli.cost import config as cost_config

        lane_cap = float(
            cost_config.LANE_DAILY_CAPS_AUD.get(
                lane,
                cost_config.ESCALATION_DAILY_CAP_AUD,
            )
        )
        cost_totals = {
            "global_spend_aud": round(global_spend, 6),
            "lane_spend_aud": round(lane_spend, 6),
            "task_spend_aud": round(task_spend, 6),
        }
        cap_state = {
            "escalation_remaining_aud": round(
                max(
                    0.0,
                    float(cost_config.ESCALATION_DAILY_CAP_AUD)
                    - escalation_spend,
                ),
                6,
            ),
            "global_remaining_aud": round(
                max(
                    0.0,
                    float(cost_config.GLOBAL_DAILY_CAP_AUD) - global_spend,
                ),
                6,
            ),
            "lane_remaining_aud": round(max(0.0, lane_cap - lane_spend), 6),
            "soft_remaining_aud": round(max(0.0, lane_cap - lane_spend), 6),
            "task_remaining_aud": round(
                max(
                    0.0,
                    float(cost_config.PER_TASK_CAP_AUD) - task_spend,
                ),
                6,
            ),
        }

        turns_used = 0
        if _table_exists(conn, "subscription_turns_ledger"):
            turns_used = int(
                _sum(
                    conn,
                    "SELECT COALESCE(SUM(turns_consumed),0) total "
                    "FROM subscription_turns_ledger "
                    "WHERE ts >= ? AND ts < ?",
                    (start, end),
                )
            )
        from hermes_cli.cost.bridge_config import BRIDGE_CAPS

        fallthrough_disabled = False
        if _table_exists(conn, "bridge_state"):
            row = conn.execute(
                "SELECT value FROM bridge_state "
                "WHERE key = 'bridge_fallthrough_disabled'"
            ).fetchone()
            if row is not None:
                try:
                    fallthrough_disabled = bool(
                        json.loads(str(row["value"])).get("disabled")
                    )
                except (TypeError, json.JSONDecodeError, AttributeError):
                    fallthrough_disabled = False

        programme: dict[str, Any] = {"state": "UNKNOWN"}
        if _table_exists(conn, "programme_state"):
            row = conn.execute(
                """
                SELECT state, reason, changed_by, changed_at,
                       task_count_at_change
                  FROM programme_state
                 WHERE id = 1
                """
            ).fetchone()
            if row is not None:
                programme = dict(row)

        return {
            "active_side_effects": active_effects,
            "cap_state": cap_state,
            "cost_totals": cost_totals,
            "current_rung_id": current_rung,
            "last_dispatch_strategy_hashes": dispatch_hashes,
            "recent_leaf_verdicts": verdicts,
            "programme_state": programme,
            "subscription_bridge_state": {
                "fallthrough_disabled": fallthrough_disabled,
                "hard_cap": int(BRIDGE_CAPS.hard_turns_daily),
                "soft_cap": int(BRIDGE_CAPS.soft_turns_daily),
                "turns_used_today": turns_used,
            },
            "task_id": task,
        }
    finally:
        conn.close()


def serialize_handoff(summary: dict) -> str:
    """Return a sorted tagged JSON payload bounded by the configured limit."""
    body = json.dumps(
        summary,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    wrapped = f"{_HANDOFF_OPEN}{body}{_HANDOFF_CLOSE}"
    limit = ROTATION_CAPS.handoff_summary_max_chars
    if len(wrapped) <= limit:
        return wrapped

    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    envelope = {
        "sha256": digest,
        "summary_prefix": "",
        "truncated": True,
    }
    fixed = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    allowance = max(
        0,
        limit - len(_HANDOFF_OPEN) - len(_HANDOFF_CLOSE) - len(fixed),
    )
    envelope["summary_prefix"] = body[:allowance]
    while True:
        truncated = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        wrapped = f"{_HANDOFF_OPEN}{truncated}{_HANDOFF_CLOSE}"
        if len(wrapped) <= limit:
            return wrapped
        overflow = len(wrapped) - limit
        prefix = str(envelope["summary_prefix"])
        envelope["summary_prefix"] = prefix[: max(0, len(prefix) - overflow)]


__all__ = [
    "build_handoff_summary",
    "close_session",
    "get_open_session_for_task",
    "list_sessions_for_task",
    "open_session",
    "serialize_handoff",
]
