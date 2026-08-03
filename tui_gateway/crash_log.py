"""Fail-closed, private crash-log writer for the TUI gateway.

The gateway may serve several profile-scoped sessions in one process.  Crash
records are therefore process-scoped and always land in the launch profile's
log directory; they never include session IDs.  Every detail crosses forced
secret redaction before durable storage.
"""

from __future__ import annotations

import os
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

from agent.redact import redact_sensitive_text


_DEFAULT_MAX_BYTES = 512 * 1024
_DEFAULT_BACKUP_COUNT = 2
_WRITE_LOCK = threading.RLock()
_REDACTION_FAILURE = "[redaction failed; crash detail omitted]"


def safe_crash_text(value: object, *, fallback: str = _REDACTION_FAILURE) -> str:
    """Force-redact crash text; never return raw input when redaction fails."""
    try:
        return redact_sensitive_text(str(value or ""), force=True)
    except Exception:
        return fallback


def _owned_by_current_user(st: os.stat_result) -> bool:
    getuid = getattr(os, "geteuid", None)
    return getuid is None or st.st_uid == getuid()


def _secure_log_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        st = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(st.st_mode):
            return False
        if not _owned_by_current_user(st):
            return False
        if os.name != "nt":
            os.chmod(path, 0o700)
        return True
    except OSError:
        return False


def _secure_existing_file(path: Path) -> os.stat_result | None:
    """Validate and privatise an existing crash artifact without following links."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink() or not stat.S_ISREG(st.st_mode):
        raise OSError("unsafe crash artifact")
    if not _owned_by_current_user(st) or st.st_nlink != 1:
        raise OSError("unsafe crash artifact ownership or link count")
    if os.name != "nt":
        os.chmod(path, 0o600)
    return st


def _rotate(path: Path, backup_count: int) -> None:
    if backup_count <= 0:
        path.unlink(missing_ok=True)
        return

    last = path.with_name(f"{path.name}.{backup_count}")
    if _secure_existing_file(last) is not None:
        last.unlink()

    for index in range(backup_count - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if _secure_existing_file(source) is None:
            continue
        destination = path.with_name(f"{path.name}.{index + 1}")
        if _secure_existing_file(destination) is not None:
            destination.unlink()
        os.replace(source, destination)
        if os.name != "nt":
            os.chmod(destination, 0o600)

    if _secure_existing_file(path) is not None:
        first = path.with_name(f"{path.name}.1")
        if _secure_existing_file(first) is not None:
            first.unlink()
        os.replace(path, first)
        if os.name != "nt":
            os.chmod(first, 0o600)


def _bounded_payload(kind: object, detail: object, max_bytes: int) -> bytes:
    safe_kind = safe_crash_text(kind).replace("\r", " ").replace("\n", " ").strip()
    safe_detail = safe_crash_text(detail)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = f"\n=== {safe_kind or 'gateway event'} · {timestamp} ===\n{safe_detail}\n"
    encoded = payload.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded
    marker = b"\n[crash record truncated]\n"
    keep = max(0, max_bytes - len(marker))
    return encoded[:keep].decode("utf-8", errors="ignore").encode("utf-8") + marker


def append_crash_record(
    log_path: Union[str, os.PathLike[str]],
    kind: object,
    detail: object = "",
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backup_count: int = _DEFAULT_BACKUP_COUNT,
) -> bool:
    """Append one redacted record to a bounded private log.

    Returns ``False`` rather than raising when any path, owner, link, or I/O
    invariant is unsafe.  This function is called from exception hooks and must
    never mask the original failure.
    """
    path = Path(log_path)
    cap = max(128, int(max_bytes))
    backups = max(0, int(backup_count))
    payload = _bounded_payload(kind, detail, cap)

    with _WRITE_LOCK:
        try:
            if not _secure_log_directory(path.parent):
                return False
            # Validate all artifacts before writing.  In particular, a planted
            # symlink backup fails closed instead of being followed or rotated.
            current = _secure_existing_file(path)
            for index in range(1, backups + 1):
                _secure_existing_file(path.with_name(f"{path.name}.{index}"))
            if current is not None and current.st_size + len(payload) > cap:
                _rotate(path, backups)

            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, 0o600)
            try:
                st = os.fstat(fd)
                if (
                    not stat.S_ISREG(st.st_mode)
                    or not _owned_by_current_user(st)
                    or st.st_nlink != 1
                ):
                    return False
                if os.name != "nt":
                    os.fchmod(fd, 0o600)
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        return False
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            return True
        except (OSError, ValueError, TypeError):
            return False
