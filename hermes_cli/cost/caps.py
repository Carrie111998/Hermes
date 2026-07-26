"""Read-side daily and per-task cost cap evaluation."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from hermes_cli.cost import config as cost_config
from hermes_cli.cost import ledger


def _utc_day_bounds() -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    tomorrow = today + timedelta(days=1)
    return (
        f"{today.isoformat()}T00:00:00Z",
        f"{tomorrow.isoformat()}T00:00:00Z",
    )


def _sum(
    query: str,
    params: tuple = (),
    *,
    conn: sqlite3.Connection | None = None,
) -> float:
    owned = conn is None
    active = conn or ledger.connect()
    try:
        row = active.execute(query, params).fetchone()
        return float(row["total"] if row is not None and row["total"] else 0.0)
    finally:
        if owned:
            active.close()


def daily_spend_aud(
    lane: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> float:
    """Return today's UTC spend, optionally restricted to one lane."""
    start, end = _utc_day_bounds()
    if lane is None:
        return _sum(
            """
            SELECT COALESCE(SUM(aud_amount), 0.0) AS total
              FROM cost_ledger
             WHERE ts >= ? AND ts < ?
            """,
            (start, end),
            conn=conn,
        )
    normalized_lane = str(lane).strip().lower()
    if normalized_lane not in cost_config.VALID_LANES:
        raise ValueError(f"invalid cost lane: {lane!r}")
    return _sum(
        """
        SELECT COALESCE(SUM(aud_amount), 0.0) AS total
          FROM cost_ledger
         WHERE ts >= ? AND ts < ? AND lane = ?
        """,
        (start, end, normalized_lane),
        conn=conn,
    )


def daily_spend_aud_billable(
    lane: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> float:
    """Return today's cap-counting spend, excluding attribution-only rows."""
    start, end = _utc_day_bounds()
    params: tuple = (start, end)
    lane_clause = ""
    if lane is not None:
        normalized_lane = str(lane).strip().lower()
        if normalized_lane not in cost_config.VALID_LANES:
            raise ValueError(f"invalid cost lane: {lane!r}")
        lane_clause = " AND lane = ?"
        params = (*params, normalized_lane)
    return _sum(
        f"""
        SELECT COALESCE(SUM(aud_amount), 0.0) AS total
          FROM cost_ledger
         WHERE ts >= ? AND ts < ?
           AND COALESCE(is_free_tier, 0) = 0
           AND COALESCE(is_subscription_bridge, 0) = 0
           {lane_clause}
        """,
        params,
        conn=conn,
    )


def lane_spend_aud(
    lane: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> float:
    """Compatibility-named gross spend helper for one lane."""
    return daily_spend_aud(lane, conn=conn)


def lane_spend_aud_billable(
    lane: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> float:
    return daily_spend_aud_billable(lane, conn=conn)


def task_spend_aud(
    task_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> float:
    """Return all recorded spend for one task."""
    return _sum(
        """
        SELECT COALESCE(SUM(aud_amount), 0.0) AS total
          FROM cost_ledger
         WHERE task_id = ?
        """,
        (str(task_id),),
        conn=conn,
    )


def task_spend_aud_billable(
    task_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> float:
    return _sum(
        """
        SELECT COALESCE(SUM(aud_amount), 0.0) AS total
          FROM cost_ledger
         WHERE task_id = ?
           AND COALESCE(is_free_tier, 0) = 0
           AND COALESCE(is_subscription_bridge, 0) = 0
        """,
        (str(task_id),),
        conn=conn,
    )


def escalation_spend_today_aud(
    *,
    conn: sqlite3.Connection | None = None,
) -> float:
    """Return today's UTC spend for escalation-tagged calls."""
    start, end = _utc_day_bounds()
    return _sum(
        """
        SELECT COALESCE(SUM(aud_amount), 0.0) AS total
          FROM cost_ledger
         WHERE ts >= ? AND ts < ? AND escalation = 1
        """,
        (start, end),
        conn=conn,
    )


def escalation_spend_today_aud_billable(
    *,
    conn: sqlite3.Connection | None = None,
) -> float:
    start, end = _utc_day_bounds()
    return _sum(
        """
        SELECT COALESCE(SUM(aud_amount), 0.0) AS total
          FROM cost_ledger
         WHERE ts >= ? AND ts < ? AND escalation = 1
           AND COALESCE(is_free_tier, 0) = 0
           AND COALESCE(is_subscription_bridge, 0) = 0
        """,
        (start, end),
        conn=conn,
    )


def check_all_caps(
    task_id: str | None,
    lane: str,
    escalation: bool,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[bool, str | None]:
    """Evaluate caps after a call is recorded.

    Global is checked first because any spend above the combined lane
    envelopes necessarily also breaches at least one lane; returning global
    makes the programme-wide limit observable instead of masking it.
    """
    normalized_lane = str(lane).strip().lower()
    if normalized_lane not in cost_config.VALID_LANES:
        raise ValueError(f"invalid cost lane: {lane!r}")

    if daily_spend_aud_billable(conn=conn) > float(
        cost_config.GLOBAL_DAILY_CAP_AUD
    ):
        return True, "global"
    if (
        task_id is not None
        and task_spend_aud_billable(str(task_id), conn=conn)
        > float(cost_config.PER_TASK_CAP_AUD)
    ):
        return True, "per_task"
    lane_cap = cost_config.LANE_DAILY_CAPS_AUD.get(
        normalized_lane,
        cost_config.ESCALATION_DAILY_CAP_AUD,
    )
    if daily_spend_aud_billable(normalized_lane, conn=conn) > float(lane_cap):
        return True, f"per_lane_{normalized_lane}"
    if (
        escalation
        and escalation_spend_today_aud_billable(conn=conn)
        > float(cost_config.ESCALATION_DAILY_CAP_AUD)
    ):
        return True, "escalation_envelope"
    return False, None


def breach_amount_aud(
    which_cap: str,
    *,
    task_id: str | None,
    lane: str,
    conn: sqlite3.Connection | None = None,
) -> float:
    """Return the total whose threshold identified ``which_cap``."""
    if which_cap == "global":
        return daily_spend_aud_billable(conn=conn)
    if which_cap == "per_task":
        return (
            task_spend_aud_billable(str(task_id), conn=conn)
            if task_id is not None
            else 0.0
        )
    if which_cap == "escalation_envelope":
        return escalation_spend_today_aud_billable(conn=conn)
    if which_cap.startswith("per_lane_"):
        return daily_spend_aud_billable(lane, conn=conn)
    raise ValueError(f"unknown cap: {which_cap!r}")


__all__ = [
    "breach_amount_aud",
    "check_all_caps",
    "daily_spend_aud",
    "daily_spend_aud_billable",
    "escalation_spend_today_aud",
    "escalation_spend_today_aud_billable",
    "lane_spend_aud",
    "lane_spend_aud_billable",
    "task_spend_aud",
    "task_spend_aud_billable",
]
