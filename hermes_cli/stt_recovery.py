"""Bounded, profile-scoped recovery storage for failed STT recordings.

The desktop transcription endpoint receives the only durable copy of a
browser recording.  This module publishes that copy before STT starts, keeps
it when transcription fails, and removes it after a successful result.

Recovery entries deliberately live below ``.cache``: voice recordings are
private, short-lived runtime data and must not be copied into Hermes backups
or profile exports.  Callers identify entries only by opaque IDs; absolute
storage paths never cross an API boundary.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import errno
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
from typing import Any, Iterator, Mapping, Optional
import uuid

from hermes_constants import get_hermes_home
from utils import atomic_json_write

try:  # POSIX cross-process lock.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows cross-process byte-range lock.
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

RECOVERY_RELATIVE_DIR = Path(".cache") / "stt-recovery"

_SCHEMA_VERSION = 1
_RECOVERY_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_STAGING_DIR_RE = re.compile(r"^\.tmp-[0-9a-f]{32}$")
_SAFE_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,8}$")
_SAFE_PROVIDER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_ORPHAN_STALE_SECONDS = 60 * 60
_CLOCK_SKEW_SECONDS = 5 * 60

_DEFAULT_RETENTION_HOURS = 24.0
_DEFAULT_MAX_ENTRIES = 50
_DEFAULT_MAX_TOTAL_MB = 500.0
_MAX_RETENTION_HOURS = 24.0 * 7
_MAX_ENTRIES = 500
_MAX_TOTAL_MB = 2048.0

_PROCESS_LOCK = threading.RLock()


def _bounded_number(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not parsed >= 0:  # also rejects NaN
        return default
    return min(max(parsed, minimum), maximum)


@dataclass(frozen=True)
class RecoveryPolicy:
    """Validated limits for one profile's recovery cache."""

    enabled: bool = True
    retention_seconds: float = _DEFAULT_RETENTION_HOURS * 3600
    max_entries: int = _DEFAULT_MAX_ENTRIES
    max_total_bytes: int = int(_DEFAULT_MAX_TOTAL_MB * 1024 * 1024)

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "RecoveryPolicy":
        stt = config.get("stt")
        stt = stt if isinstance(stt, Mapping) else {}
        raw = stt.get("recovery")
        raw = raw if isinstance(raw, Mapping) else {}

        enabled_value = raw.get("enabled", True)
        enabled = enabled_value if isinstance(enabled_value, bool) else True
        retention_hours = _bounded_number(
            raw.get("retention_hours", _DEFAULT_RETENTION_HOURS),
            default=_DEFAULT_RETENTION_HOURS,
            minimum=0,
            maximum=_MAX_RETENTION_HOURS,
        )
        max_entries = int(
            _bounded_number(
                raw.get("max_entries", _DEFAULT_MAX_ENTRIES),
                default=_DEFAULT_MAX_ENTRIES,
                minimum=0,
                maximum=_MAX_ENTRIES,
            )
        )
        max_total_mb = _bounded_number(
            raw.get("max_total_mb", _DEFAULT_MAX_TOTAL_MB),
            default=_DEFAULT_MAX_TOTAL_MB,
            minimum=0,
            maximum=_MAX_TOTAL_MB,
        )

        # Any zero limit is an explicit, fail-closed way to disable retention.
        enabled = bool(
            enabled and retention_hours > 0 and max_entries > 0 and max_total_mb > 0
        )
        return cls(
            enabled=enabled,
            retention_seconds=retention_hours * 3600,
            max_entries=max_entries,
            max_total_bytes=int(max_total_mb * 1024 * 1024),
        )

    @property
    def retention_hours(self) -> float:
        return self.retention_seconds / 3600


@dataclass(frozen=True)
class RecoveryRecord:
    recovery_id: str
    directory: Path
    audio_path: Path
    status: str
    created_at: float
    expires_at: float
    byte_size: int
    mime_type: str
    attempts: int
    provider: Optional[str] = None
    failure_code: Optional[str] = None


class SttRecoveryCache:
    """Atomic, bounded storage for one profile's failed STT uploads."""

    def __init__(
        self,
        policy: RecoveryPolicy,
        *,
        hermes_home: Optional[Path] = None,
        now: Any = time.time,
    ) -> None:
        self.policy = policy
        self.root = (hermes_home or get_hermes_home()) / RECOVERY_RELATIVE_DIR
        self._now = now
        self._attempt_leases: dict[tuple[str, int], int] = {}

    def close(self) -> None:
        """Release any live-attempt leases owned by this cache instance."""
        for fd in tuple(self._attempt_leases.values()):
            self._close_attempt_lease(fd)
        self._attempt_leases.clear()

    def __del__(self) -> None:  # pragma: no cover - deterministic in callers
        self.close()

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        hermes_home: Optional[Path] = None,
    ) -> "SttRecoveryCache":
        return cls(RecoveryPolicy.from_config(config), hermes_home=hermes_home)

    def stage_audio(
        self,
        audio: bytes,
        *,
        suffix: str,
        mime_type: str,
        source: str = "desktop",
    ) -> Optional[RecoveryRecord]:
        """Publish an immutable recording before STT begins.

        Returns ``None`` when retention is disabled, the configured byte cap
        cannot fit the recording, or recovery storage is unavailable.  A
        caller may still transcribe from an ordinary temporary file in that
        case; recovery must never make working STT unavailable.
        """
        if not self.policy.enabled or not audio:
            return None
        if len(audio) > self.policy.max_total_bytes:
            return None
        if not _SAFE_SUFFIX_RE.fullmatch(suffix):
            raise ValueError("Unsafe STT recovery suffix")

        recovery_id = uuid.uuid4().hex
        created_at = float(self._now())
        expires_at = created_at + self.policy.retention_seconds
        audio_name = f"audio{suffix}"
        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "recovery_id": recovery_id,
            "status": "transcribing",
            "created_at": created_at,
            "updated_at": created_at,
            "expires_at": expires_at,
            "audio_file": audio_name,
            "byte_size": len(audio),
            "mime_type": str(mime_type)[:128],
            "source": str(source)[:32],
            "attempts": 1,
            "provider": None,
            "failure_code": None,
        }

        staging: Optional[Path] = None
        published: Optional[Path] = None
        lease_fd: Optional[int] = None
        try:
            self._ensure_root()
            with self._locked():
                self._prune_locked(reserve_entries=1, reserve_bytes=len(audio))
                records = self._records_locked()
                orphan_entries, orphan_bytes = self._owned_orphan_usage_locked()
                if len(records) + orphan_entries + 1 > self.policy.max_entries:
                    return None
                if (
                    sum(record.byte_size for record in records)
                    + orphan_bytes
                    + len(audio)
                    > self.policy.max_total_bytes
                ):
                    return None

                staging = self.root / f".tmp-{recovery_id}"
                final_dir = self.root / recovery_id
                os.mkdir(staging, 0o700)
                if os.name == "posix":
                    os.chmod(staging, 0o700)
                self._write_private_bytes(staging / audio_name, audio)
                atomic_json_write(staging / "manifest.json", manifest, mode=0o600)
                self._fsync_directory(staging)
                os.replace(staging, final_dir)
                staging = None
                published = final_dir
                # The root lock prevents prune/claim from observing this
                # record before its live-attempt lease is held. Acquiring only
                # after rename also avoids Windows rename failures caused by
                # an open file inside the staging directory.
                lease_fd = self._acquire_attempt_lease(final_dir)
                if lease_fd is None:
                    raise OSError("Could not acquire STT recovery attempt lease")
                record = self._record_from_directory(final_dir)
                if record is None:
                    raise OSError("Could not validate staged STT recovery record")
                self._attempt_leases[(recovery_id, 1)] = lease_fd
                lease_fd = None
                published = None
                self._fsync_directory(self.root)
                return record
        except (OSError, ValueError, TypeError):
            logger.warning("Could not stage STT recovery audio", exc_info=True)
            return None
        finally:
            if lease_fd is not None:
                self._close_attempt_lease(lease_fd)
            if staging is not None:
                self._remove_directory(staging)
            if published is not None:
                self._remove_directory(published)

    def mark_failed_attempt(
        self,
        recovery_id: str,
        *,
        attempts: int,
        failure_code: str,
        provider: Optional[str] = None,
        expected_status: str = "transcribing",
    ) -> Optional[RecoveryRecord]:
        """Retain one exact attempt as failed without storing raw error text.

        The state and attempt checks are a compare-and-swap guard: a delayed
        worker must never overwrite or delete a newer manual retry.
        """
        if (
            expected_status not in {"transcribing", "delivering", "failed"}
            or attempts < 1
        ):
            return None
        if (
            expected_status in {"transcribing", "delivering"}
            and (recovery_id, attempts) not in self._attempt_leases
        ):
            return None
        try:
            with self._locked_existing_root() as available:
                if not available:
                    return None
                return self._mark_failed_locked(
                    recovery_id,
                    attempts=attempts,
                    expected_status=expected_status,
                    failure_code=failure_code,
                    provider=provider,
                )
        except OSError:
            logger.warning(
                "Could not retain failed STT recovery id=%s",
                recovery_id,
                exc_info=True,
            )
            return None

    def claim_retry(self, recovery_id: str) -> Optional[RecoveryRecord]:
        """Atomically claim a failed recording for a manual retry."""
        try:
            with self._locked_existing_root() as available:
                if not available:
                    return None
                self._prune_locked()
                record = self._get_record_locked(recovery_id)
                if (
                    record is None
                    or record.status != "failed"
                    or record.expires_at <= float(self._now())
                ):
                    return None
                lease_fd = self._acquire_attempt_lease(record.directory)
                if lease_fd is None:
                    return None
                manifest = self._read_manifest(record.directory)
                if manifest is None:
                    self._close_attempt_lease(lease_fd)
                    return None
                manifest.update({
                    "status": "transcribing",
                    "updated_at": float(self._now()),
                    "attempts": record.attempts + 1,
                    "failure_code": None,
                })
                try:
                    atomic_json_write(
                        record.directory / "manifest.json",
                        manifest,
                        mode=0o600,
                    )
                except OSError:
                    self._close_attempt_lease(lease_fd)
                    return None
                updated = self._record_from_directory(record.directory)
                if updated is None or updated.status != "transcribing":
                    self._close_attempt_lease(lease_fd)
                    return None
                self._attempt_leases[(updated.recovery_id, updated.attempts)] = lease_fd
                return updated
        except OSError:
            return None

    def mark_delivering_attempt(
        self,
        recovery_id: str,
        *,
        attempts: int,
    ) -> Optional[RecoveryRecord]:
        """Protect an attempt while its transcript is being delivered."""
        if (recovery_id, attempts) not in self._attempt_leases:
            return None
        try:
            with self._locked_existing_root() as available:
                if not available:
                    return None
                record = self._get_record_locked(recovery_id)
                if (
                    record is None
                    or record.status != "transcribing"
                    or record.attempts != attempts
                ):
                    return None
                manifest = self._read_manifest(record.directory)
                if manifest is None:
                    return None
                manifest.update({
                    "status": "delivering",
                    "updated_at": float(self._now()),
                    "failure_code": None,
                })
                try:
                    atomic_json_write(
                        record.directory / "manifest.json",
                        manifest,
                        mode=0o600,
                    )
                except OSError:
                    return None
                updated = self._record_from_directory(record.directory)
                if (
                    updated is None
                    or updated.status != "delivering"
                    or updated.attempts != attempts
                ):
                    return None
                return updated
        except OSError:
            return None

    def discard_failed(self, recovery_id: str) -> bool:
        """Delete one retryable failure; never unlink an active worker input."""
        try:
            with self._locked_existing_root() as available:
                if not available:
                    return False
                self._prune_locked()
                record = self._get_record_locked(recovery_id)
                if record is None or record.status != "failed":
                    return False
                return self._remove_directory(record.directory)
        except OSError:
            return False

    def discard_attempt(
        self,
        recovery_id: str,
        *,
        attempts: int,
        expected_status: str,
    ) -> bool:
        """Finalize only the exact transcription attempt owned by the caller."""
        if expected_status not in {"transcribing", "delivering", "failed"}:
            return False
        if expected_status in {"transcribing", "delivering"}:
            return self.complete_attempt(
                recovery_id,
                attempts=attempts,
                expected_status=expected_status,
            )
        try:
            with self._locked_existing_root() as available:
                if not available:
                    return False
                record = self._get_record_locked(recovery_id)
                if (
                    record is None
                    or record.status != expected_status
                    or record.attempts != attempts
                ):
                    return False
                return self._remove_directory(record.directory)
        except OSError:
            return False

    def complete_attempt(
        self,
        recovery_id: str,
        *,
        attempts: int,
        expected_status: str,
    ) -> bool:
        """Delete a successful attempt or queue deletion for the next access."""
        if expected_status not in {"transcribing", "delivering"}:
            return False
        if (recovery_id, attempts) not in self._attempt_leases:
            return False
        try:
            with self._locked_existing_root() as available:
                if not available:
                    return False
                record = self._get_record_locked(recovery_id)
                if (
                    record is None
                    or record.status != expected_status
                    or record.attempts != attempts
                ):
                    return False
                # Commit the non-retryable state while the attempt lease is
                # still held. A crash after transcript delivery can now only
                # leave cleanup_pending on disk, never an abandoned active
                # entry that pruning could turn back into a retryable failure.
                committed = self._mark_cleanup_pending_locked(record)
                if committed is None:
                    return False
                self._release_attempt_lease(recovery_id, attempts)
                # Antivirus/indexer locks on Windows can make deletion
                # transiently fail. cleanup_pending is pruned on every future
                # cache access, and is never listable/claimable for retry.
                self._remove_directory(record.directory)
                return True
        except OSError:
            return False

    def get_record(self, recovery_id: str) -> Optional[RecoveryRecord]:
        try:
            with self._locked_existing_root() as available:
                if not available:
                    return None
                self._prune_locked()
                record = self._get_record_locked(recovery_id)
                if record is None or not self._record_is_publicly_available(
                    record,
                    float(self._now()),
                ):
                    return None
                return record
        except OSError:
            return None

    def list_records(self) -> list[RecoveryRecord]:
        if not self.root.is_dir() or self.root.is_symlink():
            return []
        try:
            with self._locked():
                self._prune_locked()
                now = float(self._now())
                return sorted(
                    (
                        record
                        for record in self._records_locked()
                        if self._record_is_publicly_available(record, now)
                    ),
                    key=lambda item: item.created_at,
                )
        except OSError:
            return []

    def prune(self) -> None:
        if not self.root.is_dir() or self.root.is_symlink():
            return
        try:
            with self._locked():
                self._prune_locked()
        except OSError:
            logger.warning("Could not prune STT recovery cache", exc_info=True)

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        root_stat = self.root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            raise OSError("STT recovery root is not a private directory")
        if os.name == "posix":
            os.chmod(self.root, 0o700)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self._ensure_root()
        with _PROCESS_LOCK:
            if fcntl is None and msvcrt is None:  # pragma: no cover - exotic fallback
                yield
                return

            lock_path = self.root / ".lock"
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(lock_path, flags, 0o600)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                else:  # pragma: no cover - Windows
                    assert msvcrt is not None
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"\0")
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(fd)

    @contextlib.contextmanager
    def _locked_existing_root(self) -> Iterator[bool]:
        try:
            root_stat = self.root.lstat()
        except OSError:
            yield False
            return
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            yield False
            return
        with self._locked():
            yield True

    def _prune_locked(
        self, *, reserve_entries: int = 0, reserve_bytes: int = 0
    ) -> None:
        now = float(self._now())
        self._remove_orphan_staging_dirs()
        self._remove_stale_owned_orphans(now)
        records = self._records_locked()

        for record in records:
            if record.status == "cleanup_pending":
                self._remove_directory(record.directory)
        records = self._records_locked()

        # New records acquire their OS lease under this same root lock before
        # becoming observable. Therefore an active schema-v1 record without a
        # held lease is authoritatively abandoned and can be retried at once.
        for record in records:
            if record.status not in {"transcribing", "delivering"}:
                continue
            if not self._attempt_lease_is_held(record):
                self._mark_failed_locked(
                    record.recovery_id,
                    attempts=record.attempts,
                    expected_status=record.status,
                    failure_code="interrupted",
                    provider=record.provider,
                )

        records = self._records_locked()
        for record in records:
            if record.status == "failed" and record.expires_at <= now:
                if self._mark_cleanup_pending_locked(record) is not None:
                    self._remove_directory(record.directory)

        records = self._records_locked()
        orphan_entries, orphan_bytes = self._owned_orphan_usage_locked()
        target_entries = max(
            self.policy.max_entries - reserve_entries - orphan_entries,
            0,
        )
        target_bytes = max(
            self.policy.max_total_bytes - reserve_bytes - orphan_bytes,
            0,
        )
        total_bytes = sum(record.byte_size for record in records)
        failed_oldest = sorted(
            (record for record in records if record.status == "failed"),
            key=lambda item: item.created_at,
        )
        while failed_oldest and (
            len(records) > target_entries or total_bytes > target_bytes
        ):
            victim = failed_oldest.pop(0)
            if self._remove_directory(victim.directory):
                records = [
                    record
                    for record in records
                    if record.recovery_id != victim.recovery_id
                ]
                total_bytes -= victim.byte_size

    def _mark_cleanup_pending_locked(
        self,
        record: RecoveryRecord,
    ) -> Optional[RecoveryRecord]:
        """Atomically make a record inaccessible before best-effort deletion."""
        manifest = self._read_manifest(record.directory)
        if manifest is None:
            return None
        try:
            manifest_attempts = int(manifest.get("attempts") or 1)
        except (TypeError, ValueError):
            return None
        if (
            manifest.get("status") != record.status
            or manifest_attempts != record.attempts
        ):
            return None
        manifest.update({
            "status": "cleanup_pending",
            "updated_at": float(self._now()),
            "provider": None,
            "failure_code": None,
        })
        try:
            atomic_json_write(
                record.directory / "manifest.json",
                manifest,
                mode=0o600,
            )
        except (OSError, TypeError, ValueError):
            return None
        updated = self._record_from_directory(record.directory)
        if (
            updated is None
            or updated.status != "cleanup_pending"
            or updated.attempts != record.attempts
        ):
            return None
        return updated

    def _mark_failed_locked(
        self,
        recovery_id: str,
        *,
        attempts: int,
        expected_status: str,
        failure_code: str,
        provider: Optional[str],
    ) -> Optional[RecoveryRecord]:
        record = self._get_record_locked(recovery_id)
        if (
            record is None
            or record.attempts != attempts
            or record.status != expected_status
        ):
            return None
        manifest = self._read_manifest(record.directory)
        if manifest is None:
            return None
        failed_at = float(self._now())
        manifest.update({
            "status": "failed",
            "updated_at": failed_at,
            # Retention starts when an attempt fails, not when a potentially
            # long transcription began.
            "expires_at": failed_at + self.policy.retention_seconds,
            "provider": self._safe_provider(provider),
            "failure_code": self._safe_failure_code(failure_code),
        })
        try:
            atomic_json_write(
                record.directory / "manifest.json",
                manifest,
                mode=0o600,
            )
        except OSError:
            logger.warning(
                "Could not update STT recovery manifest id=%s",
                recovery_id,
                exc_info=True,
            )
            return None
        self._release_attempt_lease(recovery_id, attempts)
        updated = self._record_from_directory(record.directory)
        return updated if updated is not None and updated.status == "failed" else None

    def _records_locked(self) -> list[RecoveryRecord]:
        records: list[RecoveryRecord] = []
        try:
            children = list(self.root.iterdir())
        except OSError:
            return records
        for child in children:
            if not _RECOVERY_ID_RE.fullmatch(child.name):
                continue
            record = self._record_from_directory(child)
            if record is not None:
                records.append(record)
        return records

    def _get_record_locked(self, recovery_id: str) -> Optional[RecoveryRecord]:
        if not _RECOVERY_ID_RE.fullmatch(str(recovery_id)):
            return None
        return self._record_from_directory(self.root / recovery_id)

    def _record_from_directory(self, directory: Path) -> Optional[RecoveryRecord]:
        try:
            directory_stat = directory.lstat()
            if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_ISLNK(
                directory_stat.st_mode
            ):
                return None
            manifest = self._read_manifest(directory)
            if manifest is None:
                return None
            recovery_id = str(manifest.get("recovery_id") or "")
            if recovery_id != directory.name or not _RECOVERY_ID_RE.fullmatch(
                recovery_id
            ):
                return None
            if manifest.get("schema_version") != _SCHEMA_VERSION:
                return None
            audio_name = str(manifest.get("audio_file") or "")
            if Path(audio_name).name != audio_name or not audio_name.startswith(
                "audio."
            ):
                return None
            audio_path = directory / audio_name
            audio_stat = audio_path.lstat()
            if not stat.S_ISREG(audio_stat.st_mode) or stat.S_ISLNK(audio_stat.st_mode):
                return None
            status_value = str(manifest.get("status") or "")
            if status_value not in {
                "transcribing",
                "delivering",
                "failed",
                "cleanup_pending",
            }:
                return None
            created_at = float(manifest["created_at"])
            updated_at = float(manifest["updated_at"])
            expires_at = float(manifest["expires_at"])
            now = float(self._now())
            if any(
                not math.isfinite(value) or value < 0
                for value in (created_at, updated_at, expires_at, now)
            ):
                return None
            # Reject corrupt-but-finite epochs that could crash datetime
            # formatting or keep an active entry alive forever. The small
            # skew allowance avoids losing recovery after a minor clock step.
            if created_at > now + _CLOCK_SKEW_SECONDS:
                return None
            if updated_at > now + _CLOCK_SKEW_SECONDS:
                return None
            if updated_at + _CLOCK_SKEW_SECONDS < created_at:
                return None
            if expires_at + _CLOCK_SKEW_SECONDS < created_at:
                return None
            latest_reasonable_base = max(created_at, updated_at, now)
            if expires_at > (
                latest_reasonable_base
                + (_MAX_RETENTION_HOURS * 3600)
                + _CLOCK_SKEW_SECONDS
            ):
                return None
            return RecoveryRecord(
                recovery_id=recovery_id,
                directory=directory,
                audio_path=audio_path,
                status=status_value,
                created_at=created_at,
                expires_at=expires_at,
                byte_size=int(audio_stat.st_size),
                mime_type=str(manifest.get("mime_type") or "application/octet-stream"),
                attempts=max(int(manifest.get("attempts") or 1), 1),
                provider=self._safe_provider(manifest.get("provider")),
                failure_code=self._safe_failure_code(manifest.get("failure_code")),
            )
        except (OSError, KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _read_manifest(directory: Path) -> Optional[dict[str, Any]]:
        path = directory / "manifest.json"
        try:
            path_stat = path.lstat()
            if not stat.S_ISREG(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode):
                return None
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_private_bytes(path: Path, data: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd >= 0:
                os.close(fd)

    def _acquire_attempt_lease(self, directory: Path) -> Optional[int]:
        """Acquire a non-blocking OS lease for one live transcription."""
        path = directory / ".lease"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("STT recovery lease is not a regular file")
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:  # pragma: no cover - Windows
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - exotic fallback
                os.close(fd)
                return None
            return fd
        except OSError as exc:
            if "fd" in locals():
                os.close(fd)
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                logger.warning(
                    "Could not acquire STT recovery attempt lease", exc_info=True
                )
            return None

    @staticmethod
    def _close_attempt_lease(fd: int) -> None:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _release_attempt_lease(self, recovery_id: str, attempts: int) -> None:
        fd = self._attempt_leases.pop((recovery_id, attempts), None)
        if fd is not None:
            self._close_attempt_lease(fd)

    def _attempt_lease_is_held(self, record: RecoveryRecord) -> bool:
        if (record.recovery_id, record.attempts) in self._attempt_leases:
            return True
        fd = self._acquire_attempt_lease(record.directory)
        if fd is None:
            # Contention or an unreadable lease must fail closed: pruning a
            # live worker's immutable input is worse than delayed cleanup.
            return True
        self._close_attempt_lease(fd)
        return False

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name != "posix":  # pragma: no cover - not supported on Windows
            return
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    @staticmethod
    def _remove_directory(path: Path) -> bool:
        try:
            path_stat = path.lstat()
            if not stat.S_ISDIR(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode):
                return False
            shutil.rmtree(path)
            return True
        except OSError:
            return False

    def _remove_orphan_staging_dirs(self) -> None:
        """Remove crash leftovers while holding the cross-process lock.

        A live stager holds this same lock from directory creation through
        atomic publish, so every matching staging directory visible here is
        necessarily orphaned and can be removed immediately.
        """
        try:
            children = list(self.root.iterdir())
        except OSError:
            return
        for child in children:
            if not _STAGING_DIR_RE.fullmatch(child.name):
                continue
            try:
                child_stat = child.lstat()
                if stat.S_ISDIR(child_stat.st_mode) and not stat.S_ISLNK(
                    child_stat.st_mode
                ):
                    self._remove_directory(child)
            except OSError:
                continue

    def _remove_stale_owned_orphans(self, now: float) -> None:
        """Bound corrupt/unsupported UUID directories by a hard privacy TTL."""
        try:
            children = list(self.root.iterdir())
        except OSError:
            return
        for child in children:
            if not _RECOVERY_ID_RE.fullmatch(child.name):
                continue
            if self._record_from_directory(child) is not None:
                continue
            try:
                child_stat = child.lstat()
                if (
                    stat.S_ISDIR(child_stat.st_mode)
                    and not stat.S_ISLNK(child_stat.st_mode)
                    and now - child_stat.st_mtime >= _ORPHAN_STALE_SECONDS
                ):
                    self._remove_directory(child)
            except OSError:
                continue

    def _owned_orphan_usage_locked(self) -> tuple[int, int]:
        """Account non-symlink UUID directories that cannot be parsed."""
        entries = 0
        total_bytes = 0
        try:
            children = list(self.root.iterdir())
        except OSError:
            return entries, total_bytes
        for child in children:
            if not _RECOVERY_ID_RE.fullmatch(child.name):
                continue
            if self._record_from_directory(child) is not None:
                continue
            try:
                child_stat = child.lstat()
            except OSError:
                continue
            if not stat.S_ISDIR(child_stat.st_mode) or stat.S_ISLNK(child_stat.st_mode):
                continue
            entries += 1
            total_bytes += self._regular_file_bytes(child)
        return entries, total_bytes

    @staticmethod
    def _regular_file_bytes(directory: Path) -> int:
        total = 0
        for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
            base = Path(dirpath)
            safe_dirs: list[str] = []
            for name in dirnames:
                try:
                    mode = (base / name).lstat().st_mode
                except OSError:
                    continue
                if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
                    safe_dirs.append(name)
            dirnames[:] = safe_dirs
            for name in filenames:
                try:
                    file_stat = (base / name).lstat()
                except OSError:
                    continue
                if stat.S_ISREG(file_stat.st_mode) and not stat.S_ISLNK(
                    file_stat.st_mode
                ):
                    total += int(file_stat.st_size)
        return total

    @staticmethod
    def _safe_provider(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text if _SAFE_PROVIDER_RE.fullmatch(text) else None

    @staticmethod
    def _record_is_publicly_available(
        record: RecoveryRecord,
        now: float,
    ) -> bool:
        if record.status == "cleanup_pending":
            return False
        return not (record.status == "failed" and record.expires_at <= now)

    @staticmethod
    def _safe_failure_code(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip().lower().replace("-", "_")
        return text if re.fullmatch(r"[a-z0-9_]{1,64}", text) else "unknown_error"


def recovery_expiry_iso(record: RecoveryRecord) -> str:
    """Return a stable UTC timestamp suitable for CLI/API messages."""
    from datetime import datetime, timezone

    return (
        datetime
        .fromtimestamp(record.expires_at, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
