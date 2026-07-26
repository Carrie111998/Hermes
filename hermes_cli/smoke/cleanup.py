"""Narrow cleanup for rows carrying the immutable CS-13 smoke sentinel."""

from __future__ import annotations

from pathlib import Path

from hermes_cli.programme.gate import get_state
from hermes_cli.sqlite_util import open_connection, retrying_write_txn


class CleanupRefused(RuntimeError):
    """Raised when cleanup would run while customer work may be active."""


_DELETE_ORDER = (
    (
        "dispatch_envelopes",
        "profile = 'smoke_test' OR route = 'smoke_test' "
        "OR task_id LIKE 'smoke-t-%'",
    ),
    (
        "leaf_verdicts",
        "profile = 'smoke_test' OR route = 'smoke_test' "
        "OR task_id LIKE 'smoke-t-%'",
    ),
    (
        "cost_ledger",
        "profile = 'smoke_test' OR route = 'smoke_test' "
        "OR task_id LIKE 'smoke-t-%'",
    ),
    ("subscription_turns_ledger", "task_id LIKE 'smoke-t-%'"),
    (
        "routing_decisions",
        "profile = 'smoke_test' OR route = 'smoke_test' "
        "OR task_id LIKE 'smoke-t-%'",
    ),
    (
        "tasks",
        "id LIKE 'smoke-t-%' OR created_by = 'smoke_test'",
    ),
)


def cleanup_smoke_rows(
    db_path: str | Path,
    *,
    force: bool = False,
) -> dict[str, int]:
    """Delete only sentinel-labelled rows, in child-to-parent order."""
    path = Path(db_path).expanduser()
    state = get_state(path, migrate_if_missing=False)
    if str(state.state) == "RUNNING" and not force:
        raise CleanupRefused(
            "smoke cleanup refused while programme is RUNNING; "
            "pause first or explicitly pass --force"
        )
    conn = open_connection(
        path,
        busy_timeout_ms=5_000,
        enable_wal=True,
        synchronous="FULL",
        db_label=f"CS-13 cleanup ({path.name})",
    )
    counts: dict[str, int] = {}
    try:
        with retrying_write_txn(conn):
            existing = {
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            for table, predicate in _DELETE_ORDER:
                if table not in existing:
                    counts[table] = 0
                    continue
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE {predicate}"
                )
                counts[table] = max(0, int(cursor.rowcount))
    finally:
        conn.close()
    return counts


__all__ = ["CleanupRefused", "cleanup_smoke_rows"]
