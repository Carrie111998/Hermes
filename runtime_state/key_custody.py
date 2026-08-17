"""Local-only custody for the runtime-state HMAC key.

The key is stored beside the profile's existing ``auth.json`` and is never
returned by the public runtime-state APIs or written to the journal.  Tests
can point this class at a temporary auth file without touching a real profile.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import secrets
import tempfile

from runtime_state.locking import MaintenanceLock


class KeyUnavailable(RuntimeError):
    """The local key store is missing, malformed, or cannot be updated."""


class AuthJsonKeyCustody:
    def __init__(self, auth_path: str | Path):
        self.auth_path = Path(auth_path)
        self.lock_path = self.auth_path.with_name(self.auth_path.name + ".lock")

    def _read(self) -> dict:
        try:
            with self.auth_path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise KeyUnavailable("local Hermes auth store is unreadable") from exc
        if not isinstance(value, dict):
            raise KeyUnavailable("local Hermes auth store has invalid shape")
        return value

    @staticmethod
    def _decode(value: object) -> bytes:
        if not isinstance(value, str):
            raise KeyUnavailable("runtime-state digest key is malformed")
        try:
            key = base64.urlsafe_b64decode(value.encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise KeyUnavailable("runtime-state digest key is malformed") from exc
        if len(key) < 32:
            raise KeyUnavailable("runtime-state digest key is too short")
        return key

    def load(self) -> bytes:
        with MaintenanceLock(self.lock_path, exclusive=False):
            data = self._read()
        try:
            return self._decode(data["runtime_state"]["digest_key_b64"])
        except (KeyError, TypeError) as exc:
            raise KeyUnavailable("runtime-state digest key is unavailable") from exc

    def ensure(self) -> bytes:
        self.auth_path.parent.mkdir(parents=True, exist_ok=True)
        with MaintenanceLock(self.lock_path, exclusive=True):
            data = self._read()
            section = data.get("runtime_state")
            if section is not None:
                try:
                    return self._decode(section["digest_key_b64"])
                except (KeyError, TypeError) as exc:
                    raise KeyUnavailable("runtime-state digest key is malformed") from exc
            key = secrets.token_bytes(32)
            data["runtime_state"] = {
                "digest_key_b64": base64.urlsafe_b64encode(key).decode("ascii")
            }
            fd, temporary = tempfile.mkstemp(
                prefix=self.auth_path.name + ".", dir=str(self.auth_path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, indent=2, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.auth_path)
                try:
                    os.chmod(self.auth_path, 0o600)
                except OSError:
                    pass
            except Exception as exc:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise KeyUnavailable("runtime-state auth store could not be written") from exc
            return key
