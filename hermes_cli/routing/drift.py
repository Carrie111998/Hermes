"""Doctrine decision classification, hourly rollups, and drift alerts."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_cli.cost import telegram_alert
from hermes_cli.routing import drift_schema
from hermes_cli.sqlite_util import retrying_write_txn


logger = logging.getLogger(__name__)

_MIN_ALERT_DECISIONS = 20
_OVERRIDE_ALERT_PCT = 40.0
# CS-06: tightened. Bypass expected only via forced_legacy path.
_BYPASS_ALERT_PCT = 0.0
_CASCADE_ALERT_PCT = 5.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    text = str(value).strip()
    if not text:
        raise ValueError("timestamp must be non-empty")
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_hour(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(
        minute=0,
        second=0,
        microsecond=0,
    ).strftime("%Y-%m-%dT%H:00:00Z")


def _hour_bucket(chosen_at: str) -> str:
    """Return one canonical ISO UTC hourly bucket."""
    return _format_hour(_parse_utc(chosen_at))


def _bucket_bounds(bucket_ts: str) -> tuple[str, str]:
    start = _parse_utc(_hour_bucket(bucket_ts)).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    return _format_hour(start), _format_hour(start + timedelta(hours=1))


def _pct(count: int, total: int) -> float:
    return 0.0 if total <= 0 else (float(count) * 100.0) / float(total)


def _decision_table_exists(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
              FROM sqlite_master
             WHERE type = 'table' AND name = 'routing_decisions'
            """
        ).fetchone()
        is not None
    )


def refresh_bucket(conn: sqlite3.Connection, bucket_ts: str) -> None:
    """Recompute and upsert one UTC hourly decision bucket."""
    start, end = _bucket_bounds(bucket_ts)
    aggregate = conn.execute(
        """
        SELECT
            COUNT(*) AS total_decisions,
            COALESCE(SUM(
                CASE
                    WHEN used_doctrine_reader = 1
                     AND overridden_by_caller = 0
                     AND matched_rule_id IS NOT NULL
                    THEN 1 ELSE 0
                END
            ), 0) AS followed_count,
            COALESCE(SUM(
                CASE
                    WHEN used_doctrine_reader = 1
                     AND overridden_by_caller = 1
                    THEN 1 ELSE 0
                END
            ), 0) AS overridden_count,
            COALESCE(SUM(
                CASE WHEN used_doctrine_reader = 0 THEN 1 ELSE 0 END
            ), 0) AS bypassed_count,
            COALESCE(SUM(
                CASE
                    WHEN used_doctrine_reader = 1
                     AND overridden_by_caller = 0
                     AND matched_rule_id IS NULL
                    THEN 1 ELSE 0
                END
            ), 0) AS no_rule_count,
            COALESCE(SUM(
                CASE
                    WHEN chosen_provider = '__all_failed__'
                    THEN 1 ELSE 0
                END
            ), 0) AS all_failed_count
          FROM routing_decisions
         WHERE chosen_at >= ? AND chosen_at < ?
        """,
        (start, end),
    ).fetchone()
    total = int(aggregate["total_decisions"])
    followed = int(aggregate["followed_count"])
    overridden = int(aggregate["overridden_count"])
    bypassed = int(aggregate["bypassed_count"])
    no_rule = int(aggregate["no_rule_count"])
    all_failed = int(aggregate["all_failed_count"])
    top = conn.execute(
        """
        SELECT lane, COUNT(*) AS count
          FROM routing_decisions
         WHERE chosen_at >= ? AND chosen_at < ?
           AND used_doctrine_reader = 1
           AND overridden_by_caller = 1
         GROUP BY lane
         ORDER BY count DESC, lane ASC
         LIMIT 1
        """,
        (start, end),
    ).fetchone()
    top_lane = str(top["lane"]) if top is not None else None
    top_count = int(top["count"]) if top is not None else None
    conn.execute(
        """
        INSERT INTO routing_drift_rollup (
            window_bucket_ts, total_decisions, followed_count,
            overridden_count, bypassed_count, no_rule_count,
            followed_pct, overridden_pct, bypassed_pct, no_rule_pct,
            top_override_lane, top_override_count, updated_ts,
            all_failed_count, all_failed_pct
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(window_bucket_ts) DO UPDATE SET
            total_decisions = excluded.total_decisions,
            followed_count = excluded.followed_count,
            overridden_count = excluded.overridden_count,
            bypassed_count = excluded.bypassed_count,
            no_rule_count = excluded.no_rule_count,
            followed_pct = excluded.followed_pct,
            overridden_pct = excluded.overridden_pct,
            bypassed_pct = excluded.bypassed_pct,
            no_rule_pct = excluded.no_rule_pct,
            top_override_lane = excluded.top_override_lane,
            top_override_count = excluded.top_override_count,
            updated_ts = excluded.updated_ts,
            all_failed_count = excluded.all_failed_count,
            all_failed_pct = excluded.all_failed_pct
        """,
        (
            start,
            total,
            followed,
            overridden,
            bypassed,
            no_rule,
            _pct(followed, total),
            _pct(overridden, total),
            _pct(bypassed, total),
            _pct(no_rule, total),
            top_lane,
            top_count,
            _utc_now(),
            all_failed,
            _pct(all_failed, total),
        ),
    )


def source_bucket_count(
    *,
    db_path: str | Path | None = None,
) -> int:
    """Count distinct valid UTC source buckets without changing state."""
    drift_schema.ensure_migrated(db_path)
    conn = drift_schema.connect(db_path)
    try:
        if not _decision_table_exists(conn):
            return 0
        values = conn.execute(
            "SELECT DISTINCT chosen_at FROM routing_decisions"
        ).fetchall()
        return len({_hour_bucket(str(row["chosen_at"])) for row in values})
    finally:
        conn.close()


def refresh_all_buckets(
    *,
    db_path: str | Path | None = None,
) -> int:
    """Rebuild every materialized bucket in one BUSY-safe transaction."""
    drift_schema.ensure_migrated(db_path)
    conn = drift_schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            conn.execute("DELETE FROM routing_drift_rollup")
            if not _decision_table_exists(conn):
                return 0
            values = conn.execute(
                "SELECT DISTINCT chosen_at FROM routing_decisions"
            ).fetchall()
            buckets = sorted(
                {_hour_bucket(str(row["chosen_at"])) for row in values}
            )
            for bucket in buckets:
                refresh_bucket(conn, bucket)
            return len(buckets)
    finally:
        conn.close()


def _cutoff_bucket(hours: int) -> str:
    normalized = int(hours)
    if normalized <= 0:
        raise ValueError("hours must be greater than zero")
    current = datetime.now(timezone.utc).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    return _format_hour(current - timedelta(hours=normalized - 1))


def _zero_result(hours: int) -> dict[str, Any]:
    return {
        "window_hours": int(hours),
        "total_decisions": 0,
        "followed_count": 0,
        "followed_pct": 0.0,
        "overridden_count": 0,
        "overridden_pct": 0.0,
        "bypassed_count": 0,
        "bypassed_pct": 0.0,
        "no_rule_count": 0,
        "no_rule_pct": 0.0,
        "forced_legacy_count": 0,
        "forced_legacy_pct": 0.0,
        "all_failed_count": 0,
        "all_failed_pct": 0.0,
        "top_override_lanes": [],
        "top_override_profiles": [],
        "top_overridden_pairs": [],
        "top_cascade_failure_classes": [],
    }


def _class_counts_from_decisions(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    lane: str | None,
    profile: str | None,
) -> tuple[int, int, int, int, int, int]:
    clauses = ["chosen_at >= ?"]
    values: list[Any] = [cutoff]
    if lane is not None:
        clauses.append("lane = ?")
        values.append(str(lane))
    if profile is not None:
        clauses.append("profile = ?")
        values.append(str(profile))
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total_decisions,
            COALESCE(SUM(
                CASE
                    WHEN used_doctrine_reader = 1
                     AND overridden_by_caller = 0
                     AND matched_rule_id IS NOT NULL
                    THEN 1 ELSE 0
                END
            ), 0) AS followed_count,
            COALESCE(SUM(
                CASE
                    WHEN used_doctrine_reader = 1
                     AND overridden_by_caller = 1
                    THEN 1 ELSE 0
                END
            ), 0) AS overridden_count,
            COALESCE(SUM(
                CASE WHEN used_doctrine_reader = 0 THEN 1 ELSE 0 END
            ), 0) AS bypassed_count,
            COALESCE(SUM(
                CASE
                    WHEN used_doctrine_reader = 1
                     AND overridden_by_caller = 0
                     AND matched_rule_id IS NULL
                    THEN 1 ELSE 0
                END
            ), 0) AS no_rule_count,
            COALESCE(SUM(
                CASE
                    WHEN chosen_provider = '__all_failed__'
                    THEN 1 ELSE 0
                END
            ), 0) AS all_failed_count
          FROM routing_decisions
         WHERE {" AND ".join(clauses)}
        """,
        values,
    ).fetchone()
    return tuple(
        int(row[name])
        for name in (
            "total_decisions",
            "followed_count",
            "overridden_count",
            "bypassed_count",
            "no_rule_count",
            "all_failed_count",
        )
    )


def _rankings(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    lane: str | None,
    profile: str | None,
) -> tuple[list, list, list]:
    clauses = [
        "chosen_at >= ?",
        "used_doctrine_reader = 1",
        "overridden_by_caller = 1",
    ]
    values: list[Any] = [cutoff]
    if lane is not None:
        clauses.append("lane = ?")
        values.append(str(lane))
    if profile is not None:
        clauses.append("profile = ?")
        values.append(str(profile))
    where = " AND ".join(clauses)
    lanes = conn.execute(
        f"""
        SELECT lane, COUNT(*) AS count
          FROM routing_decisions
         WHERE {where}
         GROUP BY lane
         ORDER BY count DESC, lane ASC
         LIMIT 5
        """,
        values,
    ).fetchall()
    profiles = conn.execute(
        f"""
        SELECT COALESCE(NULLIF(profile, ''), '<unknown>') AS profile_name,
               COUNT(*) AS count
          FROM routing_decisions
         WHERE {where}
         GROUP BY profile_name
         ORDER BY count DESC, profile_name ASC
         LIMIT 5
        """,
        values,
    ).fetchall()
    pairs = conn.execute(
        f"""
        SELECT
            COALESCE(doctrine_suggested_provider, '<none>') AS source_provider,
            COALESCE(doctrine_suggested_model, '<none>') AS source_model,
            chosen_provider AS caller_provider,
            chosen_model AS caller_model,
            COUNT(*) AS count
          FROM routing_decisions
         WHERE {where}
         GROUP BY source_provider, source_model, caller_provider, caller_model
         ORDER BY count DESC, source_provider, source_model,
                  caller_provider, caller_model
         LIMIT 5
        """,
        values,
    ).fetchall()
    return (
        [(str(row["lane"]), int(row["count"])) for row in lanes],
        [
            (str(row["profile_name"]), int(row["count"]))
            for row in profiles
        ],
        [
            (
                (str(row["source_provider"]), str(row["source_model"])),
                (str(row["caller_provider"]), str(row["caller_model"])),
                int(row["count"]),
            )
            for row in pairs
        ],
    )


def _forced_legacy_count(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    lane: str | None,
    profile: str | None,
) -> int:
    clauses = ["chosen_at >= ?", "forced_legacy = 1"]
    values: list[Any] = [cutoff]
    if lane is not None:
        clauses.append("lane = ?")
        values.append(str(lane))
    if profile is not None:
        clauses.append("profile = ?")
        values.append(str(profile))
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
          FROM routing_decisions
         WHERE {" AND ".join(clauses)}
        """,
        values,
    ).fetchone()
    return int(row["count"] if row is not None else 0)


def _top_cascade_failure_classes(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    lane: str | None,
    profile: str | None,
) -> list[tuple[str, int]]:
    clauses = ["d.chosen_at >= ?"]
    values: list[Any] = [cutoff]
    if lane is not None:
        clauses.append("d.lane = ?")
        values.append(str(lane))
    if profile is not None:
        clauses.append("d.profile = ?")
        values.append(str(profile))
    rows = conn.execute(
        f"""
        SELECT
            json_extract(item.value, '$.failure_class') AS failure_class,
            COUNT(*) AS count
          FROM routing_decisions AS d
          JOIN json_each(
              CASE
                  WHEN json_valid(d.failure_history_json)
                  THEN d.failure_history_json
                  ELSE '[]'
              END
          ) AS item
         WHERE {" AND ".join(clauses)}
           AND json_type(item.value, '$.failure_class') = 'text'
           AND trim(json_extract(item.value, '$.failure_class')) <> ''
         GROUP BY failure_class
         ORDER BY count DESC, failure_class ASC
         LIMIT 5
        """,
        values,
    ).fetchall()
    return [
        (str(row["failure_class"]), int(row["count"]))
        for row in rows
    ]


def compute_drift_window(
    *,
    hours: int = 24,
    db_path: str | Path | None = None,
    lane: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Return drift counts, percentages, and top override dimensions."""
    normalized_hours = int(hours)
    cutoff = _cutoff_bucket(normalized_hours)
    drift_schema.ensure_migrated(db_path)
    conn = drift_schema.connect(db_path)
    try:
        if not _decision_table_exists(conn):
            return _zero_result(normalized_hours)
        if lane is None and profile is None:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(total_decisions), 0) AS total_decisions,
                    COALESCE(SUM(followed_count), 0) AS followed_count,
                    COALESCE(SUM(overridden_count), 0) AS overridden_count,
                    COALESCE(SUM(bypassed_count), 0) AS bypassed_count,
                    COALESCE(SUM(no_rule_count), 0) AS no_rule_count,
                    COALESCE(SUM(all_failed_count), 0) AS all_failed_count
                  FROM routing_drift_rollup
                 WHERE window_bucket_ts >= ?
                """,
                (cutoff,),
            ).fetchone()
            counts = tuple(
                int(row[name])
                for name in (
                    "total_decisions",
                    "followed_count",
                    "overridden_count",
                    "bypassed_count",
                    "no_rule_count",
                    "all_failed_count",
                )
            )
        else:
            counts = _class_counts_from_decisions(
                conn,
                cutoff=cutoff,
                lane=lane,
                profile=profile,
            )
        top_lanes, top_profiles, top_pairs = _rankings(
            conn,
            cutoff=cutoff,
            lane=lane,
            profile=profile,
        )
        forced_legacy = _forced_legacy_count(
            conn,
            cutoff=cutoff,
            lane=lane,
            profile=profile,
        )
        top_cascade_classes = _top_cascade_failure_classes(
            conn,
            cutoff=cutoff,
            lane=lane,
            profile=profile,
        )
    finally:
        conn.close()
    total, followed, overridden, bypassed, no_rule, all_failed = counts
    return {
        "window_hours": normalized_hours,
        "total_decisions": total,
        "followed_count": followed,
        "followed_pct": _pct(followed, total),
        "overridden_count": overridden,
        "overridden_pct": _pct(overridden, total),
        "bypassed_count": bypassed,
        "bypassed_pct": _pct(bypassed, total),
        "no_rule_count": no_rule,
        "no_rule_pct": _pct(no_rule, total),
        "forced_legacy_count": forced_legacy,
        "forced_legacy_pct": _pct(forced_legacy, total),
        "all_failed_count": all_failed,
        "all_failed_pct": _pct(all_failed, total),
        "top_override_lanes": top_lanes,
        "top_override_profiles": top_profiles,
        "top_overridden_pairs": top_pairs,
        "top_cascade_failure_classes": top_cascade_classes,
    }


def _alert_window(conn: sqlite3.Connection) -> dict[str, Any] | None:
    current_hour = datetime.now(timezone.utc).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    cutoff = _format_hour(current_hour - timedelta(hours=5))
    end = _format_hour(current_hour + timedelta(hours=1))
    rows = conn.execute(
        """
        SELECT *
          FROM routing_drift_rollup
         WHERE window_bucket_ts >= ? AND window_bucket_ts < ?
         ORDER BY window_bucket_ts DESC
        """,
        (cutoff, end),
    ).fetchall()
    if not rows:
        return None
    total = sum(int(row["total_decisions"]) for row in rows)
    followed = sum(int(row["followed_count"]) for row in rows)
    overridden = sum(int(row["overridden_count"]) for row in rows)
    bypassed = sum(int(row["bypassed_count"]) for row in rows)
    no_rule = sum(int(row["no_rule_count"]) for row in rows)
    all_failed = sum(int(row["all_failed_count"]) for row in rows)
    non_forced = conn.execute(
        """
        SELECT
            COUNT(*) AS eligible_total,
            COALESCE(SUM(
                CASE WHEN used_doctrine_reader = 0 THEN 1 ELSE 0 END
            ), 0) AS bypassed_count
          FROM routing_decisions
         WHERE chosen_at >= ? AND chosen_at < ?
           AND (forced_legacy IS NULL OR forced_legacy = 0)
        """,
        (cutoff, end),
    ).fetchone()
    eligible_total = int(non_forced["eligible_total"])
    non_forced_bypassed = int(non_forced["bypassed_count"])
    top = conn.execute(
        """
        SELECT lane, COUNT(*) AS count
          FROM routing_decisions
         WHERE chosen_at >= ? AND chosen_at < ?
           AND used_doctrine_reader = 1
           AND overridden_by_caller = 1
         GROUP BY lane
         ORDER BY count DESC, lane ASC
         LIMIT 1
        """,
        (cutoff, end),
    ).fetchone()
    return {
        "total_decisions": total,
        "followed_pct": _pct(followed, total),
        "overridden_pct": _pct(overridden, total),
        "bypassed_pct": _pct(non_forced_bypassed, eligible_total),
        "raw_bypassed_pct": _pct(bypassed, total),
        "no_rule_pct": _pct(no_rule, total),
        "all_failed_pct": _pct(all_failed, total),
        "top_override_lane": str(top["lane"]) if top is not None else "-",
        "top_override_count": int(top["count"]) if top is not None else 0,
    }


def maybe_alert(conn: sqlite3.Connection) -> str | None:
    """Send one same-transaction, hourly-deduplicated drift alert."""
    try:
        window = _alert_window(conn)
        if (
            window is None
            or int(window["total_decisions"]) < _MIN_ALERT_DECISIONS
        ):
            return None
        if float(window["all_failed_pct"]) > _CASCADE_ALERT_PCT:
            failure_class = "cascade_failing_high"
        elif float(window["no_rule_pct"]) > 0.0:
            failure_class = "no_rule_present"
        elif float(window["overridden_pct"]) > _OVERRIDE_ALERT_PCT:
            failure_class = "override_high"
        elif float(window["bypassed_pct"]) > _BYPASS_ALERT_PCT:
            failure_class = "bypass_high"
        else:
            return None

        message = (
            "⚠️ DOCTRINE DRIFT\n"
            f"class: {failure_class}\n"
            "window: last 6h "
            f"({window['total_decisions']} decisions)\n"
            f"followed: {window['followed_pct']:.1f}%\n"
            f"overridden: {window['overridden_pct']:.1f}%\n"
            f"bypassed: {window['bypassed_pct']:.1f}%\n"
            f"no_rule: {window['no_rule_pct']:.1f}%\n"
            f"all_failed: {window['all_failed_pct']:.1f}%\n"
            "top override lane: "
            f"{window['top_override_lane']} "
            f"({window['top_override_count']})"
        )
        from hermes_cli.side_effects import api as side_effects

        bucket = (
            f"doctrine_drift:{failure_class}:"
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H')}"
        )
        reservation = side_effects.reserve(
            task_id="system:doctrine_drift",
            lane="platform",
            action_type="telegram.send",
            payload={"target": "telegram", "message": message},
            idempotency_key=bucket,
            conn=conn,
        )
        if (
            reservation.already_done is not None
            or reservation.already_in_flight is not None
            or reservation.reserved_id is None
        ):
            return None
        row_id = int(reservation.reserved_id)
        side_effects.mark_in_flight(reserved_id=row_id, conn=conn)
        try:
            telegram_alert.send_bridge_alert(message)
        except Exception as exc:
            side_effects.fail(
                reserved_id=row_id,
                error_class=type(exc).__name__,
                error_message=str(exc),
                conn=conn,
            )
            logger.warning(
                "Doctrine drift alert send failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return None
        side_effects.confirm(
            reserved_id=row_id,
            external_ref=None,
            result_summary="doctrine drift alert delivered",
            conn=conn,
        )
        return failure_class
    except Exception as exc:
        logger.warning(
            "Doctrine drift alert skipped without blocking route decision: "
            "%s: %s",
            type(exc).__name__,
            exc,
        )
        return None


__all__ = [
    "_hour_bucket",
    "compute_drift_window",
    "maybe_alert",
    "refresh_all_buckets",
    "refresh_bucket",
    "source_bucket_count",
]
