"""Durable exact-room model profiles, independent of config.yaml."""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils import atomic_json_write

logger = logging.getLogger(__name__)
_VERSION = 1
_locks_guard = threading.Lock()
_locks: dict[Path, threading.RLock] = {}

@dataclass(frozen=True)
class RoomModelProfile:
    model: str
    provider: str
    reasoning: str


def exact_room_id(source: Any) -> str | None:
    thread = str(getattr(source, "thread_id", "") or "").strip()
    chat = str(getattr(source, "chat_id", "") or "").strip()
    return thread or chat or None


def _platform(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _lock_for(path: Path) -> threading.RLock:
    path = path.resolve()
    with _locks_guard:
        return _locks.setdefault(path, threading.RLock())

@contextmanager
def _sibling_lock(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as f:
        windows_lock = False
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Windows
            import msvcrt
            f.seek(0)
            if not f.read(1):
                f.seek(0); f.write(" "); f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            windows_lock = True
        try:
            yield
        finally:
            if windows_lock:
                import msvcrt
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

class RoomProfileStore:
    def __init__(self, path: Path | str | None = None):
        if path is None:
            from hermes_constants import get_hermes_home
            path = Path(get_hermes_home()) / "room_profiles.json"
        self.path = Path(path).expanduser()
        self._snapshot: dict[str, Any] | None = None
        self._invalid = False
        self.load()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": _VERSION, "profiles": {}}

    @staticmethod
    def _entry(value: Any) -> RoomModelProfile | None:
        if not isinstance(value, dict): return None
        vals = [value.get(k) for k in ("model", "provider", "reasoning")]
        if not all(isinstance(v, str) and v.strip() for v in vals): return None
        return RoomModelProfile(*(v.strip() for v in vals))

    def _read(self) -> dict[str, Any]:
        if not self.path.exists(): return self._empty()
        try:
            with self.path.open(encoding="utf-8") as f: raw = json.load(f)
        except Exception as exc:
            logger.warning("Cannot read room profile sidecar %s: %s", self.path, exc)
            raise ValueError("invalid room profile sidecar") from exc
        if not isinstance(raw, dict) or raw.get("version") != _VERSION or not isinstance(raw.get("profiles"), dict):
            logger.warning("Unsupported room profile sidecar schema: %s", self.path)
            raise ValueError("unsupported room profile sidecar")
        return raw

    def _valid_snapshot(self, raw: dict[str, Any]) -> dict[str, Any]:
        # Preserve unknown root/platform/entry fields for forward compatibility,
        # but only expose valid typed entries.
        result = copy.deepcopy(raw)
        result["profiles"] = {}
        for plat, rooms in raw["profiles"].items():
            if not isinstance(plat, str) or not isinstance(rooms, dict): continue
            valid = {}
            for room, value in rooms.items():
                if isinstance(room, str) and room.strip() and self._entry(value): valid[room] = copy.deepcopy(value)
                elif value is not None: logger.warning("Ignoring invalid room profile %s/%s", plat, room)
            if valid: result["profiles"][plat] = valid
        return result

    def load(self) -> None:
        try:
            self._snapshot = self._valid_snapshot(self._read())
            self._invalid = False
        except ValueError:
            self._snapshot = self._empty()
            self._invalid = True

    def get(self, platform: Any, room_id: str) -> RoomModelProfile | None:
        if self._invalid: return None
        room = str(room_id or "").strip()
        if not room: return None
        # Reloading here makes external edits visible and keeps fail-closed semantics.
        self.load()
        value = (self._snapshot or {}).get("profiles", {}).get(_platform(platform), {}).get(room)
        return self._entry(value)

    def upsert(self, platform: Any, room_id: str, profile: RoomModelProfile) -> RoomModelProfile:
        if not isinstance(profile, RoomModelProfile) or self._entry(profile.__dict__) is None:
            raise ValueError("invalid room model profile")
        plat, room = _platform(platform), str(room_id or "").strip()
        if not plat or not room: raise ValueError("platform and room_id are required")
        with _lock_for(self.path):
            with _sibling_lock(self.path):
                raw = self._read()  # authoritative read inside both locks
                updated = copy.deepcopy(raw)
                profiles = updated.setdefault("profiles", {})
                rooms = profiles.setdefault(plat, {})
                old = rooms.get(room)
                replacement = {"model": profile.model, "provider": profile.provider, "reasoning": profile.reasoning}
                if isinstance(old, dict):
                    replacement = {**old, **replacement}
                rooms[room] = replacement
                atomic_json_write(self.path, updated, indent=2)
                self._snapshot = self._valid_snapshot(updated)
                self._invalid = False
        return profile
