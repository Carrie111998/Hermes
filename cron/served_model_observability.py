"""Per-job served-model observability for LLM cron executions."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

_LOCK = threading.RLock()
_FILENAME = "served-model.jsonl"


def _valid_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and not any(ord(ch) < 32 for ch in value)


def _job_path(job_id: str) -> Path | None:
    if not _valid_text(job_id):
        return None
    if any(ch in job_id for ch in ("/", "\\")) or job_id in {".", ".."}:
        return None
    home = get_hermes_home().expanduser()
    if not home.is_absolute():
        home = home.resolve()
    return home / "cron" / "output" / job_id / _FILENAME


def record_served_model(
    job_id: Any,
    requested_model: Any,
    served_model: Any,
    *,
    observed_at: Any = None,
    output_file: Any = None,
) -> bool:
    """Append one fail-open, per-job served-model record."""
    if not _valid_text(requested_model) or not _valid_text(served_model):
        return False
    path = _job_path(job_id)
    if path is None:
        return False
    if observed_at is None:
        observed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if not _valid_text(observed_at):
        return False
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.parent.is_symlink() or path.is_symlink():
                return False
            row = {
                "job_id": job_id,
                "observed_at": observed_at,
                "output_file": None,
                "requested_model": requested_model,
                "served_model": served_model,
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False
