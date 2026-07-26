"""Atomic UTC-window rate limiting for business lanes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hermes_cli.lanes import schema
from hermes_cli.lanes.errors import LaneRateLimitExceeded
from hermes_cli.sqlite_util import retrying_write_txn


def window_start(kind: str, now: datetime | None = None) -> str:
    value = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if kind == "hourly_ingest":
        value = value.replace(minute=0, second=0, microsecond=0)
    elif kind in {"daily_task", "daily_cost"}:
        value = value.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"unknown rate-limit window: {kind}")
    return value.isoformat().replace("+00:00", "Z")


def check_and_increment(
    *,
    lane_id: str,
    window_kind: str,
    increment: float,
    cap: float,
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> bool:
    if increment < 0 or cap < 0:
        raise ValueError("increment and cap must be non-negative")
    schema.ensure_migrated(db_path)
    start = window_start(window_kind, now)
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            row = conn.execute(
                """SELECT id,count,aud_total FROM lane_rate_limit_state
                   WHERE lane_id=? AND window_kind=? AND window_start=?""",
                (lane_id, window_kind, start),
            ).fetchone()
            count = int(row["count"]) if row else 0
            aud = float(row["aud_total"]) if row else 0.0
            current = aud if window_kind == "daily_cost" else count
            if current + float(increment) > float(cap):
                return False
            new_count = count + (
                0 if window_kind == "daily_cost" else int(increment)
            )
            new_aud = aud + (
                float(increment) if window_kind == "daily_cost" else 0.0
            )
            conn.execute(
                """INSERT INTO lane_rate_limit_state(
                     lane_id,window_kind,window_start,count,aud_total)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(lane_id,window_kind,window_start) DO UPDATE SET
                     count=excluded.count,aud_total=excluded.aud_total""",
                (lane_id, window_kind, start, new_count, new_aud),
            )
            return True
    finally:
        conn.close()


def enforce(**kwargs) -> None:
    if not check_and_increment(**kwargs):
        raise LaneRateLimitExceeded(
            f"lane rate limit exceeded: {kwargs['lane_id']} "
            f"{kwargs['window_kind']}"
        )


def record_cost_advisory(
    *,
    lane_id: str,
    increment: float,
    cap: float,
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> bool:
    """Record daily lane spend and report threshold state without blocking."""
    if increment < 0 or cap < 0:
        raise ValueError("increment and cap must be non-negative")
    schema.ensure_migrated(db_path)
    start = window_start("daily_cost", now)
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            row = conn.execute(
                """SELECT aud_total FROM lane_rate_limit_state
                   WHERE lane_id=? AND window_kind='daily_cost'
                     AND window_start=?""",
                (lane_id, start),
            ).fetchone()
            current = float(row["aud_total"]) if row else 0.0
            projected = current + float(increment)
            conn.execute(
                """INSERT INTO lane_rate_limit_state(
                     lane_id,window_kind,window_start,count,aud_total)
                   VALUES(?,'daily_cost',?,0,?)
                   ON CONFLICT(lane_id,window_kind,window_start) DO UPDATE SET
                     count=excluded.count,aud_total=excluded.aud_total""",
                (lane_id, start, projected),
            )
            return projected <= float(cap)
    finally:
        conn.close()


def read_bucket(
    *,
    lane_id: str,
    window_kind: str,
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> tuple[int, float]:
    """Return the active bucket without creating or changing a counter row."""
    schema.ensure_migrated(db_path)
    start = window_start(window_kind, now)
    conn = schema.connect(db_path)
    try:
        row = conn.execute(
            """SELECT count,aud_total FROM lane_rate_limit_state
               WHERE lane_id=? AND window_kind=? AND window_start=?""",
            (lane_id, window_kind, start),
        ).fetchone()
        if row is None:
            return 0, 0.0
        return int(row["count"]), float(row["aud_total"])
    finally:
        conn.close()


__all__ = [
    "check_and_increment",
    "enforce",
    "read_bucket",
    "record_cost_advisory",
    "window_start",
]
