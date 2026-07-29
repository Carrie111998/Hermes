"""Private durable addresses for the profile-gated Telegram check-in UI."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import fcntl


_MODE: Final[int] = 0o600
_DIR_MODE: Final[int] = 0o700


@dataclass(slots=True)
class WizardBinding:
    """No health values: only the exact Telegram address of a wizard prompt."""

    session_id: str
    owner_id: str
    chat_id: str
    topic_id: str
    step: str
    version: int
    message_id: str
    expires_at: int
    awaiting_text: bool = False

    @classmethod
    def from_dict(cls, value: object) -> WizardBinding | None:
        if not isinstance(value, dict):
            return None
        required = ("session_id", "owner_id", "chat_id", "topic_id", "step", "message_id")
        if any(not isinstance(value.get(key), str) or not value[key] for key in required):
            return None
        try:
            version = int(value.get("version"))
            expires_at = int(value.get("expires_at"))
        except (TypeError, ValueError):
            return None
        if version < 0 or expires_at <= 0:
            return None
        return cls(
            session_id=value["session_id"], owner_id=value["owner_id"], chat_id=value["chat_id"],
            topic_id=value["topic_id"], step=value["step"], version=version,
            message_id=value["message_id"], expires_at=expires_at,
            awaiting_text=bool(value.get("awaiting_text", False)),
        )

    def to_dict(self) -> dict[str, str | int | bool]:
        return {
            "session_id": self.session_id, "owner_id": self.owner_id, "chat_id": self.chat_id,
            "topic_id": self.topic_id, "step": self.step, "version": self.version,
            "message_id": self.message_id, "expires_at": self.expires_at,
            "awaiting_text": self.awaiting_text,
        }


class BindingStore:
    """Atomic owner-only persistence for prompt addresses across gateway restarts."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock_path = path.with_suffix(path.suffix + ".lock")

    def load(self, owner_id: str, chat_id: str, topic_id: str, now_epoch: int) -> tuple[dict[str, WizardBinding], str | None]:
        with self._locked():
            raw = self._read_unlocked()
            values = raw.get("bindings", []) if isinstance(raw, dict) else []
            bindings: dict[str, WizardBinding] = {}
            for item in values if isinstance(values, list) else []:
                binding = WizardBinding.from_dict(item)
                if binding is None or binding.expires_at <= now_epoch:
                    continue
                if (binding.owner_id, binding.chat_id, binding.topic_id) != (owner_id, chat_id, topic_id):
                    continue
                bindings[binding.session_id] = binding
            active = raw.get("active_session_id") if isinstance(raw, dict) else None
            active_id = active if isinstance(active, str) and active in bindings else None
            return bindings, active_id

    def save(self, bindings: dict[str, WizardBinding], active_session_id: str | None) -> None:
        with self._locked():
            payload = {
                "version": 1,
                "active_session_id": active_session_id,
                "bindings": [binding.to_dict() for binding in bindings.values()],
            }
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
            self._path.parent.chmod(_DIR_MODE)
            temporary = self._path.with_suffix(self._path.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                temporary.chmod(_MODE)
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            self._path.chmod(_MODE)

    def _read_unlocked(self) -> object:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _locked(self):
        self._lock_path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        self._lock_path.parent.chmod(_DIR_MODE)
        handle = self._lock_path.open("a", encoding="utf-8")
        self._lock_path.chmod(_MODE)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return _BindingLock(handle)


class _BindingLock:
    def __init__(self, handle) -> None:
        self._handle = handle

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, traceback) -> None:
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
