"""Owner-private filesystem primitives for TUI profile artifacts."""

from __future__ import annotations

import os
import stat
import threading
import uuid
from pathlib import Path
from typing import Union


_APPEND_LOCK = threading.RLock()


def _owner_ok(info: os.stat_result) -> bool:
    getuid = getattr(os, "geteuid", None)
    return getuid is None or info.st_uid == getuid()


def secure_private_directory(path: Union[str, os.PathLike[str]]) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = directory.lstat()
    if directory.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise OSError("unsafe private artifact directory")
    if not _owner_ok(info):
        raise OSError("unsafe private artifact directory owner")
    if os.name != "nt":
        os.chmod(directory, 0o700)
    return directory


def secure_private_file(path: Union[str, os.PathLike[str]]) -> os.stat_result | None:
    artifact = Path(path)
    try:
        info = artifact.lstat()
    except FileNotFoundError:
        return None
    if artifact.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise OSError("unsafe private artifact file")
    if not _owner_ok(info) or info.st_nlink != 1:
        raise OSError("unsafe private artifact owner or link count")
    if os.name != "nt":
        os.chmod(artifact, 0o600)
    return info


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short private artifact write")
        view = view[written:]


def _validate_open_file(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or not _owner_ok(info)
        or info.st_nlink != 1
    ):
        raise OSError("unsafe open private artifact")
    if os.name != "nt":
        os.fchmod(descriptor, 0o600)


def write_private_file_atomic_exclusive(
    path: Union[str, os.PathLike[str]],
    payload: bytes,
) -> Path:
    """Atomically publish a private file without replacing an existing name."""

    destination = Path(path)
    parent = secure_private_directory(destination.parent)
    if secure_private_file(destination) is not None:
        raise FileExistsError(destination)
    temporary = parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        _validate_open_file(descriptor)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        # Linking a fully-written inode is an atomic no-replace publication:
        # an attacker or concurrent writer that wins the destination causes
        # FileExistsError rather than data replacement or symlink traversal.
        os.link(temporary, destination, follow_symlinks=False)
        temporary.unlink()
        secure_private_file(destination)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return destination


def write_private_file_atomic(
    path: Union[str, os.PathLike[str]],
    payload: bytes,
) -> Path:
    destination = Path(path)
    parent = secure_private_directory(destination.parent)
    secure_private_file(destination)
    temporary = parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        _validate_open_file(descriptor)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, destination)
        if os.name != "nt":
            os.chmod(destination, 0o600)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return destination


def append_private_file(
    path: Union[str, os.PathLike[str]],
    payload: bytes,
) -> Path:
    destination = Path(path)
    with _APPEND_LOCK:
        secure_private_directory(destination.parent)
        secure_private_file(destination)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(destination, flags, 0o600)
        try:
            _validate_open_file(descriptor)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return destination


def read_private_file(
    path: Union[str, os.PathLike[str]],
    *,
    max_bytes: int = 8 * 1024 * 1024,
) -> bytes:
    artifact = Path(path)
    secure_private_directory(artifact.parent)
    expected = secure_private_file(artifact)
    if expected is None:
        raise FileNotFoundError(artifact)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(artifact, flags)
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or not _owner_ok(current)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise OSError("private artifact changed during open")
        if current.st_size > max_bytes:
            raise OSError("private artifact exceeds read limit")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            raise OSError("private artifact exceeds read limit")
        return payload
    finally:
        os.close(descriptor)
