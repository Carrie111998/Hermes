"""Read-only aggregation over the activity telemetry database.

This module never creates, migrates, checkpoints, or writes the database: the
connection is opened through a ``mode=ro`` URI so an accidental write fails
loudly instead of mutating evidence. Process-level completion is never
relabelled as semantic success — that distinction is the whole point of the
telemetry, so it is preserved verbatim in the aggregate.
"""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any

#: Emitted columns, in order. Kept explicit (never ``SELECT *``) so a schema
#: addition cannot silently widen a report consumers already depend on.
COLUMNS = (
    "activity_id",
    "policy_version",
    "requested_provider",
    "requested_model",
    "served_provider",
    "served_model",
    "final_outcome",
    "runs",
    "semantic_successes",
    "unknowns",
    "turns",
    "model_calls",
    "tool_calls",
    "retries",
    "uncached_input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
    "recorded_provider_cost_usd",
    "api_equivalent_cost_usd",
    "average_wall_time_ms",
)

_SUMMARY_SQL = """
SELECT
    r.activity_id                          AS activity_id,
    r.policy_version                       AS policy_version,
    r.requested_provider                   AS requested_provider,
    r.requested_model                      AS requested_model,
    u.served_provider                      AS served_provider,
    u.served_model                         AS served_model,
    r.final_outcome                        AS final_outcome,
    COUNT(DISTINCT r.run_id)               AS runs,
    COUNT(DISTINCT CASE WHEN r.final_outcome = 'succeeded' THEN r.run_id END)
                                           AS semantic_successes,
    COUNT(DISTINCT CASE WHEN r.final_outcome = 'unknown' THEN r.run_id END)
                                           AS unknowns,
    COALESCE(SUM(u.turns), 0)              AS turns,
    COALESCE(SUM(u.model_calls), 0)        AS model_calls,
    COALESCE(SUM(u.tool_calls), 0)         AS tool_calls,
    COALESCE(SUM(u.retries), 0)            AS retries,
    COALESCE(SUM(u.uncached_input_tokens), 0) AS uncached_input_tokens,
    COALESCE(SUM(u.cache_read_tokens), 0)  AS cache_read_tokens,
    COALESCE(SUM(u.cache_write_tokens), 0) AS cache_write_tokens,
    COALESCE(SUM(u.output_tokens), 0)      AS output_tokens,
    COALESCE(SUM(u.reasoning_tokens), 0)   AS reasoning_tokens,
    AVG(r.wall_time_ms)                    AS average_wall_time_ms
FROM logical_activity_runs AS r
LEFT JOIN logical_activity_route_usage AS u ON u.run_id = r.run_id
WHERE r.started_at >= ?
GROUP BY
    r.activity_id, r.policy_version, r.requested_provider, r.requested_model,
    u.served_provider, u.served_model, r.final_outcome
ORDER BY
    r.activity_id, r.policy_version, u.served_provider, u.served_model,
    r.final_outcome
"""

#: Exact decimal strings are summed separately: SQLite would coerce them to
#: floats, and provider cost must stay exact.
_COST_SQL = """
SELECT u.recorded_provider_cost_usd, u.api_equivalent_cost_usd
FROM logical_activity_runs AS r
JOIN logical_activity_route_usage AS u ON u.run_id = r.run_id
WHERE r.started_at >= ?
  AND r.activity_id IS ? AND r.policy_version IS ?
  AND r.requested_provider IS ? AND r.requested_model IS ?
  AND u.served_provider IS ? AND u.served_model IS ?
  AND r.final_outcome IS ?
"""


def _normalized_since(since: Any) -> str:
    if not isinstance(since, str) or not since.strip():
        raise ValueError("since must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(since)
    except ValueError as exc:
        raise ValueError(f"since is not a valid ISO-8601 timestamp: {since!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("since must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


def _read_only_connection(db_path: Path) -> sqlite3.Connection:
    resolved = Path(db_path).resolve()
    if not resolved.is_file():
        # Opening a missing file with mode=ro would raise a bare
        # OperationalError; be explicit and never create anything.
        raise FileNotFoundError(f"activity telemetry database not found: {resolved}")
    # as_uri() percent-encodes spaces and '#', which a hand-built
    # "file:{path}" string would not.
    # isolation_level=None hands transaction control to us; summarize() opens an
    # explicit read transaction so all of its queries share one snapshot.
    conn = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro", uri=True, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    return conn


def _decimal_total(values) -> str | None:
    """Sum exact decimal strings, preserving None (unknown) versus 0 (free)."""
    from decimal import Decimal

    present = [value for value in values if value is not None]
    if not present:
        return None
    return str(sum((Decimal(value) for value in present), Decimal(0)))


def summarize(db_path: Path, since: str) -> list[dict[str, Any]]:
    """Aggregate finished and in-flight runs since an ISO-8601 lower bound."""
    lower_bound = _normalized_since(since)
    conn = _read_only_connection(db_path)
    try:
        # The database is written live by the cron adapter. Without one read
        # transaction the aggregate and the exact-cost requeries would see
        # different snapshots, and a row could report counters from before a
        # write alongside costs from after it.
        conn.execute("BEGIN")
        rows = conn.execute(_SUMMARY_SQL, (lower_bound,)).fetchall()
        summaries = []
        for row in rows:
            group = (
                row["activity_id"], row["policy_version"],
                row["requested_provider"], row["requested_model"],
                row["served_provider"], row["served_model"], row["final_outcome"],
            )
            costs = conn.execute(_COST_SQL, (lower_bound, *group)).fetchall()
            summary = {name: row[name] for name in COLUMNS if name in row.keys()}
            summary["recorded_provider_cost_usd"] = _decimal_total(
                [cost["recorded_provider_cost_usd"] for cost in costs]
            )
            summary["api_equivalent_cost_usd"] = _decimal_total(
                [cost["api_equivalent_cost_usd"] for cost in costs]
            )
            summaries.append({name: summary[name] for name in COLUMNS})
        return summaries
    finally:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        conn.close()
