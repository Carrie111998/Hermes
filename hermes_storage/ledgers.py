"""Ledger helpers — cron/kanban/projects/etc via Mongo when enabled."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def mongo_ledger_enabled() -> bool:
    try:
        from hermes_storage import is_mongo_mode
        return is_mongo_mode()
    except Exception:
        return False


def load_json_document(collection: str, key: str = "default") -> Any:
    from hermes_storage import require_storage

    storage = require_storage()
    rows = storage.ledgers.find(collection, {"key": key}, limit=1)
    if not rows:
        return None
    return rows[0].get("data")


def save_json_document(collection: str, data: Any, key: str = "default") -> None:
    from hermes_storage import require_storage

    storage = require_storage()
    storage.ledgers.replace_one(collection, {"key": key}, {"key": key, "data": data})


def load_cron_jobs() -> Optional[Any]:
    if not mongo_ledger_enabled():
        return None
    return load_json_document("cron_jobs")


def save_cron_jobs(data: Any) -> bool:
    if not mongo_ledger_enabled():
        return False
    save_json_document("cron_jobs", data)
    return True


def record_execution(doc: dict[str, Any]) -> Optional[str]:
    if not mongo_ledger_enabled():
        return None
    from hermes_storage import require_storage

    storage = require_storage()
    return storage.ledgers.insert("cron_executions", doc)


def bridge_json_file(path: Path, collection: str) -> Any:
    """Load ledger JSON from Mongo when enabled; else from local file.

    In Mongo mode an empty remote document is authoritative — local files are
    never used as fallback (would split fleet state).
    """
    if mongo_ledger_enabled():
        return load_json_document(collection)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read %s: %s", path, exc)
    return None