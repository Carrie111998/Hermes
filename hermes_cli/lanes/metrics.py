"""Lane metric writes and read-side aggregates."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hermes_cli.lanes import schema
from hermes_cli.sqlite_util import retrying_write_txn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def record_metric(
    *,
    lane_id: str,
    lane_task_id: int | None,
    metric_name: str,
    value: float,
    db_path: str | Path | None = None,
) -> int:
    schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            cursor = conn.execute(
                """INSERT INTO lane_metric(
                     lane_id,lane_task_id,metric_name,value,recorded_at)
                   VALUES(?,?,?,?,?)""",
                (
                    lane_id,
                    lane_task_id,
                    str(metric_name),
                    float(value),
                    _now(),
                ),
            )
            return int(cursor.lastrowid)
    finally:
        conn.close()


def aggregate(
    lane_id: str,
    *,
    db_path: str | Path | None = None,
) -> dict[str, float]:
    schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT metric_name,SUM(value) AS total
                 FROM lane_metric WHERE lane_id=?
                 GROUP BY metric_name ORDER BY metric_name""",
            (lane_id,),
        ).fetchall()
        return {str(row["metric_name"]): float(row["total"]) for row in rows}
    finally:
        conn.close()


__all__ = ["aggregate", "record_metric"]
