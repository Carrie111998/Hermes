"""Atomic, profile-scoped ownership for in-process cron schedulers.

A scheduler ownership lease decides which runtime may inspect and dispatch one
profile's cron store. It is separate from the per-tick lock and per-job fire
claims. The ownership mutex is held from the final token check through dispatch
submission, closing the check-then-execute race between competing runtimes.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Callable, Iterator, Optional
import uuid

from utils import atomic_json_write

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)

_OWNERSHIP_VERSION = 1
_DEFAULT_LEASE_SECONDS = 180.0
_LOCK_TIMEOUT_SECONDS = 1.0
_OWNER_PRIORITIES = {
    "desktop-fallback": 10,
    "gateway-dedicated": 100,
    "gateway-multiplex": 100,
}


class SchedulerOwnershipLease:
    """Renewable ownership token for one profile's cron scheduler."""

    def __init__(
        self,
        *,
        profile_home: Path | str,
        profile: str,
        runtime_id: str,
        owner_kind: str,
        lease_seconds: float = _DEFAULT_LEASE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if owner_kind not in _OWNER_PRIORITIES:
            raise ValueError(f"Unsupported cron scheduler owner kind: {owner_kind}")
        if not profile or not runtime_id:
            raise ValueError("profile and runtime_id are required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        self.profile_home = Path(profile_home).expanduser().resolve(strict=False)
        self.profile = profile
        self.runtime_id = runtime_id
        self.owner_kind = owner_kind
        self.lease_seconds = float(lease_seconds)
        self._clock = clock
        self.token = uuid.uuid4().hex
        self._cron_dir = self.profile_home / "cron"
        self._record_path = self._cron_dir / "scheduler_ownership.json"
        self._lock_path = self._cron_dir / ".scheduler_ownership.lock"

    @property
    def priority(self) -> int:
        return _OWNER_PRIORITIES[self.owner_kind]

    def _record(self, *, now: float, acquired_at: Optional[float] = None) -> dict:
        return {
            "version": _OWNERSHIP_VERSION,
            "profile": self.profile,
            "profile_home": str(self.profile_home),
            "runtime_id": self.runtime_id,
            "owner_kind": self.owner_kind,
            "priority": self.priority,
            "pid": os.getpid(),
            "token": self.token,
            "acquired_at": now if acquired_at is None else acquired_at,
            "renewed_at": now,
            "expires_at": now + self.lease_seconds,
        }

    def _read_record_locked(self) -> Optional[dict]:
        if not self._record_path.exists():
            return None
        raw = json.loads(self._record_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("cron scheduler ownership record is not an object")
        required = {
            "version",
            "profile",
            "profile_home",
            "runtime_id",
            "owner_kind",
            "priority",
            "token",
            "expires_at",
        }
        if not required <= raw.keys():
            raise ValueError("cron scheduler ownership record is incomplete")
        if raw.get("version") != _OWNERSHIP_VERSION:
            raise ValueError("unsupported cron scheduler ownership record version")
        if raw.get("profile") != self.profile:
            raise ValueError("cron scheduler ownership profile mismatch")
        persisted_home = Path(str(raw.get("profile_home"))).expanduser().resolve(strict=False)
        if persisted_home != self.profile_home:
            raise ValueError("cron scheduler ownership home mismatch")
        if raw.get("owner_kind") not in _OWNER_PRIORITIES:
            raise ValueError("cron scheduler ownership kind is invalid")
        float(raw["expires_at"])
        int(raw["priority"])
        return raw

    def _write_record_locked(self, record: dict) -> None:
        atomic_json_write(self._record_path, record, mode=0o600, sort_keys=True)

    @contextmanager
    def _ownership_lock(self) -> Iterator[None]:
        self._cron_dir.mkdir(parents=True, exist_ok=True)
        handle = self._lock_path.open("a+b")
        acquired = False
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        try:
            if sys.platform == "win32":
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                while True:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        acquired = True
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EDEADLK):
                            raise
                        if time.monotonic() >= deadline:
                            raise TimeoutError("cron scheduler ownership lock is busy") from exc
                        time.sleep(0.01)
            else:
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                            raise
                        if time.monotonic() >= deadline:
                            raise TimeoutError("cron scheduler ownership lock is busy") from exc
                        time.sleep(0.01)
            yield
        finally:
            if acquired:
                try:
                    if sys.platform == "win32":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()

    def _owned_record(self, record: Optional[dict], now: float) -> bool:
        if not record:
            return False
        return record.get("token") == self.token and float(record["expires_at"]) > now

    def claim(self) -> bool:
        """Atomically claim, renew, or preempt a lower-priority owner."""
        try:
            with self._ownership_lock():
                now = self._clock()
                current = self._read_record_locked()
                if self._owned_record(current, now):
                    assert current is not None
                    self._write_record_locked(
                        self._record(now=now, acquired_at=float(current["acquired_at"]))
                    )
                    return True
                expired = current is None or float(current["expires_at"]) <= now
                lower_priority = current is not None and int(current["priority"]) < self.priority
                if not expired and not lower_priority:
                    return False
                self._write_record_locked(self._record(now=now))
                return True
        except Exception as exc:
            logger.warning(
                "Cron scheduler ownership claim failed closed for profile %s: %s",
                self.profile,
                type(exc).__name__,
            )
            return False

    def renew(self) -> bool:
        """Renew only if this exact token still owns the profile."""
        try:
            with self._ownership_lock():
                now = self._clock()
                current = self._read_record_locked()
                if not self._owned_record(current, now):
                    return False
                assert current is not None
                self._write_record_locked(
                    self._record(now=now, acquired_at=float(current["acquired_at"]))
                )
                return True
        except Exception as exc:
            logger.warning(
                "Cron scheduler ownership renewal failed closed for profile %s: %s",
                self.profile,
                type(exc).__name__,
            )
            return False

    def is_owner(self) -> bool:
        """Return whether this exact, unexpired token owns the profile."""
        try:
            with self._ownership_lock():
                return self._owned_record(self._read_record_locked(), self._clock())
        except Exception:
            return False

    @contextmanager
    def dispatch_guard(self) -> Iterator[bool]:
        """Hold the ownership mutex across the final check and dispatch submit."""
        lock_context = self._ownership_lock()
        try:
            lock_context.__enter__()
        except Exception as exc:
            logger.warning(
                "Cron scheduler dispatch ownership check failed closed for profile %s: %s",
                self.profile,
                type(exc).__name__,
            )
            yield False
            return

        try:
            now = self._clock()
            current = self._read_record_locked()
            allowed = self._owned_record(current, now)
            if allowed:
                assert current is not None
                self._write_record_locked(
                    self._record(now=now, acquired_at=float(current["acquired_at"]))
                )
        except Exception as exc:
            lock_context.__exit__(None, None, None)
            logger.warning(
                "Cron scheduler dispatch ownership check failed closed for profile %s: %s",
                self.profile,
                type(exc).__name__,
            )
            yield False
            return

        try:
            yield allowed
        finally:
            lock_context.__exit__(None, None, None)

    def release(self) -> bool:
        """Release only if this exact token still owns the persisted lease."""
        try:
            with self._ownership_lock():
                current = self._read_record_locked()
                if not current or current.get("token") != self.token:
                    return False
                try:
                    self._record_path.unlink()
                except FileNotFoundError:
                    return False
                return True
        except Exception as exc:
            logger.warning(
                "Cron scheduler ownership release failed for profile %s: %s",
                self.profile,
                type(exc).__name__,
            )
            return False


def read_scheduler_ownership(profile_home: Path | str) -> Optional[dict]:
    """Return the persisted owner record for diagnostics, without prompts/jobs."""
    path = Path(profile_home).expanduser().resolve(strict=False) / "cron" / "scheduler_ownership.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None
