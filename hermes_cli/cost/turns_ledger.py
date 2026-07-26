"""Durable ChatGPT Pro subscription-turn and bridge-health accounting."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_cli.cost import bridge_config, turns_schema, vendors
from hermes_cli.sqlite_util import retrying_write_txn


VALID_OUTCOMES = frozenset(
    {"success", "failure", "degraded", "rate_limited"}
)
VALID_BRIDGE_TIERS = frozenset({"pro", "plus", "free", "unknown"})
VALID_HEALTH_SOURCES = frozenset({"probe", "nightly", "on_call", "on_error"})
VALID_HEALTH_OUTCOMES = frozenset(
    {"healthy", "degraded", "exhausted", "error"}
)


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp with a ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _utc_day_bounds() -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    tomorrow = today + timedelta(days=1)
    return (
        f"{today.isoformat()}T00:00:00Z",
        f"{tomorrow.isoformat()}T00:00:00Z",
    )


def _positive_int(value: Any, field: str) -> int:
    result = int(value)
    if result <= 0:
        raise ValueError(f"{field} must be greater than zero")
    return result


def _optional_nonnegative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    result = int(value)
    if result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


def _json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def record_turn(
    *,
    task_id: Optional[str],
    lane: str,
    outcome: str,
    bridge_tier: str = "pro",
    model_reported: Optional[str] = None,
    model_requested: Optional[str] = None,
    turns_consumed: int = 1,
    latency_ms: Optional[int] = None,
    error_class: Optional[str] = None,
    error_message: Optional[str] = None,
    request_id: Optional[str] = None,
    raw_response_meta: Optional[dict] = None,
    db_path: str | Path | None = None,
) -> int:
    """Append one attributed Pro-bridge attempt and return its row id."""
    normalized_lane = str(lane).strip().lower()
    normalized_outcome = str(outcome).strip().lower()
    normalized_tier = str(bridge_tier).strip().lower()
    vendors.validate_lane(normalized_lane)
    if normalized_outcome not in VALID_OUTCOMES:
        raise ValueError(f"unknown bridge outcome: {outcome!r}")
    if normalized_tier not in VALID_BRIDGE_TIERS:
        raise ValueError(f"unknown bridge tier: {bridge_tier!r}")
    normalized_turns = _positive_int(turns_consumed, "turns_consumed")
    normalized_latency = _optional_nonnegative_int(latency_ms, "latency_ms")

    turns_schema.ensure_migrated(db_path)
    conn = turns_schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            cursor = conn.execute(
                """
                INSERT INTO subscription_turns_ledger (
                    ts, task_id, lane, vendor, bridge_tier, model_reported,
                    model_requested, turns_consumed, latency_ms, outcome,
                    error_class, error_message, request_id, raw_response_meta
                ) VALUES (?, ?, ?, 'openai-codex', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    str(task_id) if task_id is not None else None,
                    normalized_lane,
                    normalized_tier,
                    str(model_reported) if model_reported is not None else None,
                    str(model_requested)
                    if model_requested is not None
                    else None,
                    normalized_turns,
                    normalized_latency,
                    normalized_outcome,
                    str(error_class) if error_class is not None else None,
                    str(error_message) if error_message is not None else None,
                    str(request_id) if request_id is not None else None,
                    _json(raw_response_meta),
                ),
            )
            return int(cursor.lastrowid)
    finally:
        conn.close()


def turns_today(db_path: str | Path | None = None) -> int:
    """Return turns consumed during the current UTC calendar day."""
    turns_schema.ensure_migrated(db_path)
    start, end = _utc_day_bounds()
    conn = turns_schema.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(turns_consumed), 0) AS total
              FROM subscription_turns_ledger
             WHERE ts >= ? AND ts < ?
            """,
            (start, end),
        ).fetchone()
        return int(row["total"] if row is not None else 0)
    finally:
        conn.close()


def turns_today_by_lane(
    db_path: str | Path | None = None,
) -> Dict[str, int]:
    """Return today's turns grouped across all six programme lanes."""
    turns_schema.ensure_migrated(db_path)
    start, end = _utc_day_bounds()
    conn = turns_schema.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT lane, COALESCE(SUM(turns_consumed), 0) AS total
              FROM subscription_turns_ledger
             WHERE ts >= ? AND ts < ?
             GROUP BY lane
            """,
            (start, end),
        ).fetchall()
    finally:
        conn.close()
    result = {lane: 0 for lane in vendors.ALLOWED_LANES}
    for row in rows:
        result[str(row["lane"])] = int(row["total"])
    return result


def turns_by_outcome_last_hours(
    hours: int,
    db_path: str | Path | None = None,
) -> Dict[str, int]:
    """Return attempts grouped by outcome inside a rolling UTC window."""
    normalized_hours = _positive_int(hours, "hours")
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=normalized_hours)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    upper_bound = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    turns_schema.ensure_migrated(db_path)
    conn = turns_schema.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT outcome, COALESCE(SUM(turns_consumed), 0) AS total
             FROM subscription_turns_ledger
             WHERE ts >= ? AND ts <= ?
             GROUP BY outcome
            """,
            (cutoff, upper_bound),
        ).fetchall()
    finally:
        conn.close()
    result = {outcome: 0 for outcome in VALID_OUTCOMES}
    for row in rows:
        result[str(row["outcome"])] = int(row["total"])
    return result


def check_bridge_caps(
    db_path: str | Path | None = None,
) -> Dict[str, Any]:
    """Return daily threshold and recent degradation signals."""
    caps = bridge_config.BRIDGE_CAPS
    used = turns_today(db_path)
    outcomes = turns_by_outcome_last_hours(1, db_path)
    total = sum(outcomes.values())
    degraded = outcomes["degraded"] + outcomes["rate_limited"]
    degraded_rate = (degraded / total * 100.0) if total else 0.0
    hard_hit = used >= int(caps.hard_turns_daily)
    return {
        "turns_used": used,
        "soft_cap": int(caps.soft_turns_daily),
        "hard_cap": int(caps.hard_turns_daily),
        "soft_hit": used >= int(caps.soft_turns_daily),
        "hard_hit": hard_hit,
        "degraded_rate_pct": round(degraded_rate, 2),
    }


def record_health(
    *,
    source: str,
    outcome: str,
    tier_observed: str | None = None,
    model_observed: str | None = None,
    latency_ms: int | None = None,
    turns_used_today: int | None = None,
    turns_cap_daily: int | None = None,
    note: str | None = None,
    raw: Any = None,
    db_path: str | Path | None = None,
) -> int:
    """Append one bridge-health observation and return its row id."""
    normalized_source = str(source).strip().lower()
    normalized_outcome = str(outcome).strip().lower()
    if normalized_source not in VALID_HEALTH_SOURCES:
        raise ValueError(f"unknown bridge health source: {source!r}")
    if normalized_outcome not in VALID_HEALTH_OUTCOMES:
        raise ValueError(f"unknown bridge health outcome: {outcome!r}")
    turns_schema.ensure_migrated(db_path)
    conn = turns_schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            cursor = conn.execute(
                """
                INSERT INTO bridge_health_log (
                    ts, source, outcome, tier_observed, model_observed,
                    latency_ms, turns_used_today, turns_cap_daily, note, raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    normalized_source,
                    normalized_outcome,
                    str(tier_observed)
                    if tier_observed is not None
                    else None,
                    str(model_observed)
                    if model_observed is not None
                    else None,
                    _optional_nonnegative_int(latency_ms, "latency_ms"),
                    _optional_nonnegative_int(
                        turns_used_today, "turns_used_today"
                    ),
                    _optional_nonnegative_int(
                        turns_cap_daily, "turns_cap_daily"
                    ),
                    str(note) if note is not None else None,
                    _json(raw),
                ),
            )
            return int(cursor.lastrowid)
    finally:
        conn.close()


def last_health(
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return the newest bridge-health row, if one exists."""
    turns_schema.ensure_migrated(db_path)
    conn = turns_schema.connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM bridge_health_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


__all__ = [
    "VALID_BRIDGE_TIERS",
    "VALID_HEALTH_OUTCOMES",
    "VALID_HEALTH_SOURCES",
    "VALID_OUTCOMES",
    "check_bridge_caps",
    "last_health",
    "record_health",
    "record_turn",
    "turns_by_outcome_last_hours",
    "turns_today",
    "turns_today_by_lane",
    "utc_now",
]
