"""Minimal durable journal for accepted inbound gateway messages.

The journal deliberately stores routing metadata only: never message bodies,
transcripts, sender names, credentials, or media paths.  It exists solely so a
cold gateway restart can tell the originating chat/topic that processing was
interrupted before a final delivery obligation was created.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1


def _home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def _root() -> Path:
    return _home() / "runtime" / "inflight"


def _platform_value(event: Any) -> str:
    platform = getattr(getattr(event, "source", None), "platform", None)
    value = getattr(platform, "value", platform)
    return str(value or "unknown").strip().lower()


def _record_path(event: Any) -> Path:
    source = getattr(event, "source", None)
    platform = _platform_value(event)
    identity = "\0".join(
        [
            platform,
            str(getattr(source, "chat_id", "") or ""),
            str(getattr(source, "thread_id", "") or ""),
            str(getattr(event, "message_id", "") or ""),
        ]
    )
    digest = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()
    return _root() / platform / f"{digest}.json"


def claim_inflight(event: Any) -> str:
    """Atomically persist an accepted message's non-content routing metadata."""
    source = getattr(event, "source", None)
    path = _record_path(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    message_type = getattr(event, "message_type", None)
    message_type = getattr(message_type, "value", message_type)
    record = {
        "version": _SCHEMA_VERSION,
        "platform": _platform_value(event),
        "chat_id": str(getattr(source, "chat_id", "") or ""),
        "thread_id": str(getattr(source, "thread_id", "") or ""),
        "message_id": str(getattr(event, "message_id", "") or ""),
        "message_type": str(message_type or "unknown"),
        "phase": "accepted",
        "process_id": os.getpid(),
        "started_at": time.time(),
    }
    tmp = path.with_suffix(f".tmp-{os.getpid()}")
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with open(tmp, "w", encoding="utf-8") as handle:
        os.chmod(tmp, 0o600)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return str(path)


def clear_inflight(token: str | Path | None) -> bool:
    """Acknowledge a record, rejecting paths outside the journal root."""
    if not token:
        return True
    try:
        root = _root().resolve()
        path = Path(token).resolve()
        if path != root and root not in path.parents:
            return False
        path.unlink(missing_ok=True)
        return not path.exists()
    except OSError:
        return False


def list_inflight(platform: str) -> list[dict[str, Any]]:
    """Return valid records for one platform, oldest first, with path tokens."""
    directory = _root() / str(platform).strip().lower()
    if not directory.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
                continue
            if data.get("platform") != str(platform).strip().lower():
                continue
            data["_token"] = str(path)
            records.append(data)
        except (OSError, ValueError, TypeError):
            continue
    records.sort(key=lambda item: (float(item.get("started_at", 0.0)), item.get("_token", "")))
    return records
