"""Mongo document mirrors for SQLite/JSON ledgers.

When Mongo mode is on, these helpers are the durable path. Local SQLite/JSON
may still be used as a per-PC working cache, but writes must succeed on Mongo
first (fail-hard) so fleets do not split brain.

Cron is wired in ``cron/jobs.py``. Other ledgers call these helpers from their
mutation paths.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from hermes_storage.ledgers import (
    load_json_document,
    mongo_ledger_enabled,
    save_json_document,
)

logger = logging.getLogger(__name__)

PROJECTS_COLLECTION = "projects_mirror"
KANBAN_COLLECTION = "kanban_mirror"
RESPONSE_STORE_COLLECTION = "response_store"
VERIFICATION_COLLECTION = "verification_evidence"
DISCORD_RECOVERY_COLLECTION = "discord_message_recovery"


def mirror_put(collection: str, key: str, data: Any) -> bool:
    """Persist ``data`` under ``key``. Returns False if Mongo mode is off."""
    if not mongo_ledger_enabled():
        return False
    save_json_document(collection, data, key=key)
    return True


def mirror_get(collection: str, key: str = "default") -> Optional[Any]:
    if not mongo_ledger_enabled():
        return None
    return load_json_document(collection, key=key)


def mirror_delete(collection: str, key: str) -> bool:
    if not mongo_ledger_enabled():
        return False
    from hermes_storage import require_storage

    storage = require_storage()
    return bool(storage.ledgers.delete(collection, {"key": key}))


def sync_projects_from_conn(conn: Any) -> None:
    """Snapshot projects SQLite into Mongo (authoritative when Mongo mode on)."""
    if not mongo_ledger_enabled():
        return
    from hermes_cli.projects_db import (
        get_active_id,
        list_discovered_repos,
        list_projects,
    )

    projects = []
    for p in list_projects(conn, include_archived=True):
        if hasattr(p, "to_dict"):
            projects.append(p.to_dict())
        elif isinstance(p, dict):
            projects.append(p)
        else:
            projects.append(dict(getattr(p, "__dict__", {}) or {}))
    try:
        discovered = list_discovered_repos(conn)
    except Exception:
        discovered = []
    mirror_put(
        PROJECTS_COLLECTION,
        "default",
        {
            "projects": projects,
            "active_id": get_active_id(conn),
            "discovered": discovered,
        },
    )


def sync_kanban_board_meta(slug: str, meta: dict[str, Any]) -> None:
    if not mongo_ledger_enabled():
        return
    mirror_put(KANBAN_COLLECTION, f"board_meta:{slug}", meta)


def sync_kanban_current(slug: Optional[str]) -> None:
    if not mongo_ledger_enabled():
        return
    if slug is None:
        mirror_delete(KANBAN_COLLECTION, "current")
    else:
        mirror_put(KANBAN_COLLECTION, "current", {"slug": slug})
