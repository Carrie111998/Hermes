"""Exact current-boot identity for hardened Canonical runtime services.

``ProcSubset=pid`` deliberately hides the kernel-wide ``/proc`` APIs from a
service.  Canonical services still need the public Linux boot UUID to bind
their short-lived startup and liveness receipts to the current boot.  Their
systemd units therefore copy only that public file into the service's private
read-only credential mount.  Processes outside those units continue to read
the kernel file directly.

This module performs only exact filesystem and UUID validation.  It contains
no user-text interpretation or semantic routing.
"""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path
from typing import Mapping


PROC_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
SYSTEMD_CREDENTIALS_ROOT = Path("/run/credentials")
SYSTEMD_BOOT_ID_CREDENTIAL_NAME = "host-boot-id"
SYSTEMD_BOOT_ID_CREDENTIAL_DIRECTIVE = (
    f"LoadCredential={SYSTEMD_BOOT_ID_CREDENTIAL_NAME}:{PROC_BOOT_ID_PATH}"
)
_MAX_BOOT_ID_BYTES = 128


def _normalized_absolute_path(value: str) -> Path:
    path = Path(value)
    if not value or not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise RuntimeError("runtime boot identity credential path is invalid")
    return path


def _credential_path(environ: Mapping[str, str]) -> Path | None:
    raw_directory = environ.get("CREDENTIALS_DIRECTORY")
    if raw_directory is None:
        return None
    directory = _normalized_absolute_path(raw_directory)
    if directory.parent != SYSTEMD_CREDENTIALS_ROOT or not directory.name.endswith(
        ".service"
    ):
        raise RuntimeError("runtime boot identity credential path is invalid")
    path = directory / SYSTEMD_BOOT_ID_CREDENTIAL_NAME
    try:
        os.lstat(path)
    except FileNotFoundError:
        # A service may use unrelated systemd credentials while retaining a
        # visible full procfs.  Only the exact host-boot credential selects the
        # private credential path.
        return None
    except OSError as exc:
        raise RuntimeError("runtime boot identity credential is unavailable") from exc
    return path


def _read_bounded_file(path: Path, *, systemd_credential: bool) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("runtime boot identity is unavailable") from exc
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise RuntimeError("runtime boot identity file is invalid")
        if systemd_credential:
            try:
                directory = os.stat(path.parent, follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(
                    "runtime boot identity credential directory is unavailable"
                ) from exc
            effective_uid = os.geteuid()
            effective_gid = os.getegid()
            directory_owner = (directory.st_uid, directory.st_gid)
            file_owner = (observed.st_uid, observed.st_gid)
            allowed_owners = {
                (0, 0),
                (effective_uid, 0),
                (effective_uid, effective_gid),
            }
            if (
                not stat.S_ISDIR(directory.st_mode)
                or directory_owner != file_owner
                or directory_owner not in allowed_owners
                or stat.S_IMODE(directory.st_mode) != 0o500
                or stat.S_IMODE(observed.st_mode) != 0o400
            ):
                raise RuntimeError("runtime boot identity credential metadata is invalid")
        raw = os.read(descriptor, _MAX_BOOT_ID_BYTES + 1)
    finally:
        os.close(descriptor)
    if not raw or len(raw) > _MAX_BOOT_ID_BYTES:
        raise RuntimeError("runtime boot identity file is invalid")
    return raw


def current_boot_id(*, environ: Mapping[str, str] | None = None) -> str:
    """Return the canonical current Linux boot UUID or fail closed."""

    effective_environment = os.environ if environ is None else environ
    credential = _credential_path(effective_environment)
    raw = _read_bounded_file(
        PROC_BOOT_ID_PATH if credential is None else credential,
        systemd_credential=credential is not None,
    )
    try:
        value = raw.decode("ascii", errors="strict").strip()
        parsed = uuid.UUID(value)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("runtime boot identity is invalid") from exc
    if str(parsed) != value:
        raise RuntimeError("runtime boot identity is not canonical")
    return value


__all__ = [
    "PROC_BOOT_ID_PATH",
    "SYSTEMD_BOOT_ID_CREDENTIAL_DIRECTIVE",
    "SYSTEMD_BOOT_ID_CREDENTIAL_NAME",
    "current_boot_id",
]
