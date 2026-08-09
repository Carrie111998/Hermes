"""Schema-v1 event validation and crash-safe local spool handling."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol

from plugins.agentops.control.models import AppendResult, EventEnvelope, SpoolReplayResult


class EventValidationError(ValueError):
    """Raised without echoing untrusted event content."""


class SpoolCapacityError(RuntimeError):
    """Raised when the configured local spool budget has been reached."""


class EventStore(Protocol):
    def append_event(self, event: EventEnvelope) -> AppendResult: ...


_SECRET_KEY = re.compile(r"(?:api[_-]?key|token|cookie|password|secret|authorization|credential)", re.I)
_SECRET_VALUE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}\b|\bBearer\s+[A-Za-z0-9._-]{8,}\b|\bgh[pousr]_[A-Za-z0-9]{8,}\b)",
    re.I,
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically after recursively rejecting unsafe values."""
    safe_value = _validate_json(value)
    return json.dumps(safe_value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _validate_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EventValidationError("event validation failed")
        return value
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise EventValidationError("event validation failed")
            if _SECRET_KEY.search(key):
                raise EventValidationError("event validation failed")
            output[key] = _validate_json(child)
        return output
    if isinstance(value, (list, tuple)):
        return [_validate_json(child) for child in value]
    raise EventValidationError("event validation failed")


def contains_secret(value: Any) -> bool:
    try:
        encoded = canonical_json(value)
    except EventValidationError:
        return True
    return bool(_SECRET_VALUE.search(encoded))


def contains_secret_blob(value: str) -> bool:
    return bool(_SECRET_VALUE.search(value) or _SECRET_KEY.search(value))


def validate_event_fields(
    *,
    schema_version: Any,
    event_id: Any,
    event_type: Any,
    occurred_at: Any,
    producer: Any,
    target_id: Any,
    correlation_id: Any,
    payload: Any,
    redaction_version: Any,
) -> None:
    if schema_version != 1 or not isinstance(redaction_version, int) or redaction_version < 1:
        raise EventValidationError("event validation failed")
    for value in (event_id, event_type, producer, target_id):
        if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
            raise EventValidationError("event validation failed")
    if correlation_id is not None and (not isinstance(correlation_id, str) or not _SAFE_ID.fullmatch(correlation_id)):
        raise EventValidationError("event validation failed")
    if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
        raise EventValidationError("event validation failed")
    _validate_json(payload)
    if contains_secret(payload):
        raise EventValidationError("event validation failed")


class EventSpool:
    """Bounded event spool that is independent of any target or Gateway."""

    def __init__(self, root: Path, *, max_bytes: int = 256 * 1024 * 1024):
        self.root = Path(root)
        self.quarantine_dir = self.root / "quarantine"
        self.max_bytes = max_bytes

    def _ensure_directories(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
            os.chmod(self.quarantine_dir, 0o700)
        except OSError:
            pass

    def pending_paths(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(path for path in self.root.glob("*.json") if path.is_file())

    def depth(self) -> int:
        return len(self.pending_paths())

    def _size_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.pending_paths())

    def write(self, event: EventEnvelope) -> Path:
        payload = canonical_json(event.to_dict()).encode("utf-8")
        self._ensure_directories()
        destination = self.root / f"{event.event_id}.json"
        if destination.exists():
            if destination.read_bytes() == payload:
                return destination
            raise EventValidationError("event validation failed")
        if self._size_bytes() + len(payload) > self.max_bytes:
            raise SpoolCapacityError("event spool capacity reached")
        temporary = self.root / f".{event.event_id}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            return destination
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    def _quarantine(self, path: Path, raw: str, reason: str) -> None:
        self._ensure_directories()
        destination = self.quarantine_dir / path.name
        if contains_secret_blob(raw):
            content = canonical_json(
                {
                    "reason": reason,
                    "content_hash": "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                    "redacted": True,
                }
            )
            temporary = self.quarantine_dir / f".{path.name}.tmp"
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, destination)
            path.unlink(missing_ok=True)
            return
        os.replace(path, destination)

    def replay(self, store: EventStore) -> SpoolReplayResult:
        appended = duplicates = quarantined = 0
        for path in self.pending_paths():
            try:
                raw = path.read_text(encoding="utf-8")
                event = EventEnvelope.from_dict(json.loads(raw))
                result = store.append_event(event)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, EventValidationError, TypeError, AttributeError):
                try:
                    self._quarantine(path, locals().get("raw", ""), "event_invalid")
                    quarantined += 1
                except OSError:
                    pass
                continue
            path.unlink(missing_ok=True)
            if result.inserted:
                appended += 1
            else:
                duplicates += 1
        return SpoolReplayResult(appended=appended, duplicates=duplicates, quarantined=quarantined)
