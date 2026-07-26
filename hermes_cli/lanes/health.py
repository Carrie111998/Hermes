"""Fail-fast framework health validation used by ``hermes doctor``."""

from __future__ import annotations

from pathlib import Path

from hermes_constants import get_default_hermes_root
from hermes_cli.lanes import schema
from hermes_cli.lanes.manifest import default_path, load_manifest

_CORE_TABLES = frozenset(
    {
        "programme_state",
        "tasks",
        "cost_ledger",
        "side_effects",
        "subscription_turns_ledger",
        "dispatch_envelopes",
        "leaf_verdicts",
        "routing_decisions",
        "routing_doctrine",
        "routing_doctrine_meta",
        "sessions",
        "service_manifest_state",
        "service_restart_run",
    }
)
_LANE_TABLES = frozenset(
    {
        "lane_manifest_state",
        "lane_task",
        "lane_approval_queue",
        "lane_publish_log",
        "lane_rate_limit_state",
        "lane_metric",
    }
)


def check_framework(
    *,
    db_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
) -> tuple[bool, str]:
    db = (
        Path(db_path).expanduser()
        if db_path
        else get_default_hermes_root() / "kanban.db"
    )
    manifest = (
        Path(manifest_path).expanduser()
        if manifest_path
        else default_path()
    )
    if not db.exists():
        return False, f"database missing: {db}"
    conn = schema.connect(db)
    try:
        existing = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    missing_core = sorted(_CORE_TABLES - existing)
    if missing_core:
        return False, "missing CS-01..CS-13 tables: " + ",".join(missing_core)
    try:
        schema.ensure_migrated(db)
        loaded = load_manifest(manifest, db_path=db, record_state=True)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    conn = schema.connect(db)
    try:
        existing = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    missing_lane = sorted(_LANE_TABLES - existing)
    if missing_lane:
        return False, "missing lane tables: " + ",".join(missing_lane)
    enabled_count = sum(1 for lane in loaded.lanes if lane.enabled)
    publish_count = sum(
        1 for lane in loaded.lanes if lane.publish_enabled
    )
    return (
        True,
        f"schema=6/6 manifest=v1 lanes={len(loaded.lanes)} "
        f"enabled={enabled_count} publish={publish_count}",
    )


__all__ = ["check_framework"]
