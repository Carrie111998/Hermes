"""Profile-scoped persistent cooldowns for fallback model entries."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from hermes_constants import get_hermes_home
from utils import atomic_json_write


def _state_path() -> Path:
    return get_hermes_home() / "fallback_cooldowns.json"


def _entry_key(entry: dict[str, Any]) -> str:
    provider = str(entry.get("provider") or "").strip().lower()
    model = str(entry.get("model") or "").strip()
    base_url = str(entry.get("base_url") or "").strip().rstrip("/").lower()
    return "\n".join((provider, model, base_url))


@contextmanager
def _locked_state(path: Path) -> Iterator[None]:
    """Serialize read-modify-write updates across Hermes processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_state(path: Path) -> dict[str, float]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return {}
    result: dict[str, float] = {}
    for key, value in entries.items():
        try:
            result[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return result


def configured_cooldown_seconds(entry: dict[str, Any]) -> float:
    """Return an entry's opt-in cooldown duration, or zero when disabled."""

    try:
        value = float(entry.get("cooldown_seconds") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def cooldown_remaining(entry: dict[str, Any], *, now: float | None = None) -> float:
    """Return seconds remaining for an entry, reloading state on every call."""

    if configured_cooldown_seconds(entry) <= 0:
        return 0
    expires_at = _read_state(_state_path()).get(_entry_key(entry), 0)
    return max(0, expires_at - (time.time() if now is None else now))


def record_cooldown(entry: dict[str, Any], *, now: float | None = None) -> float:
    """Persist the configured cooldown for an entry and return its expiry."""

    duration = configured_cooldown_seconds(entry)
    if duration <= 0:
        return 0
    path = _state_path()
    timestamp = time.time() if now is None else now
    expires_at = timestamp + duration
    with _locked_state(path):
        entries = _read_state(path)
        entries = {key: expiry for key, expiry in entries.items() if expiry > timestamp}
        key = _entry_key(entry)
        entries[key] = max(entries.get(key, 0), expires_at)
        atomic_json_write(
            path, {"version": 1, "entries": entries}, indent=2, mode=0o600
        )
    return expires_at
