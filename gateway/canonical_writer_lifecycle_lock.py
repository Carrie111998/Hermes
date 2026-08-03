"""Shared host lock for canonical writer release lifecycle mutations."""

from __future__ import annotations

import fcntl
import os
import stat
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


HOST_LIFECYCLE_LOCK_PATH = Path("/run/muncho-writer-activation.lock")

_LOCAL = threading.local()


def _effective_uid() -> int:
    getter = getattr(os, "geteuid", None)
    return int(getter()) if callable(getter) else -1


def _list_xattrs(path: Path) -> tuple[str, ...]:
    lister = getattr(os, "listxattr", None)
    if not callable(lister):
        if sys.platform == "linux":
            raise RuntimeError("lifecycle lock xattr inspection is unavailable")
        return ()
    return tuple(lister(path, follow_symlinks=False))


def _validate_root_parent_chain(path: Path) -> None:
    current = path
    while True:
        item = os.lstat(current)
        if (
            stat.S_ISLNK(item.st_mode)
            or not stat.S_ISDIR(item.st_mode)
            or item.st_uid != 0
            or item.st_gid != 0
            or stat.S_IMODE(item.st_mode) & 0o022
            or _list_xattrs(current)
        ):
            raise PermissionError("lifecycle lock parent is not root-controlled")
        if current == current.parent:
            return
        current = current.parent


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _lifecycle_file_lock(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
    validate_parent: bool,
) -> Iterator[int]:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("lifecycle lock path must be absolute and normalized")
    active = getattr(_LOCAL, "active", None)
    if active is not None and active[0] == os.getpid() and active[1] == path:
        _LOCAL.active = (active[0], active[1], active[2], active[3] + 1)
        try:
            yield active[2]
        finally:
            current = _LOCAL.active
            _LOCAL.active = (current[0], current[1], current[2], current[3] - 1)
        return

    if validate_parent:
        _validate_root_parent_chain(path.parent)
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        descriptor = os.open(path, flags)
    try:
        if created:
            os.fchown(descriptor, owner_uid, owner_gid)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            _fsync_directory(path.parent)
        opened = os.fstat(descriptor)
        reached = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != owner_uid
            or opened.st_gid != owner_gid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino) != (reached.st_dev, reached.st_ino)
            or _list_xattrs(path)
        ):
            raise PermissionError("lifecycle lock identity is invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another writer release lifecycle is running") from exc
        locked = os.fstat(descriptor)
        reached = os.lstat(path)
        if (
            not stat.S_ISREG(locked.st_mode)
            or locked.st_nlink != 1
            or locked.st_uid != owner_uid
            or locked.st_gid != owner_gid
            or stat.S_IMODE(locked.st_mode) != 0o600
            or (locked.st_dev, locked.st_ino) != (reached.st_dev, reached.st_ino)
            or _list_xattrs(path)
        ):
            raise PermissionError("lifecycle lock identity changed")
        _LOCAL.active = (os.getpid(), path, descriptor, 1)
        try:
            yield descriptor
        finally:
            del _LOCAL.active
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def host_release_lifecycle_lock(
    path: Path = HOST_LIFECYCLE_LOCK_PATH,
) -> Iterator[int]:
    """Serialize build, publication, activation, and GC on one host.

    The lock is re-entrant only within the same thread and process so a
    publication may call the independently-safe release builder without
    opening a second file description for the same flock.
    """

    if _effective_uid() != 0:
        raise PermissionError("canonical_writer_lifecycle_requires_uid_0")
    if sys.platform != "linux":
        raise RuntimeError("canonical_writer_lifecycle_requires_linux")
    with _lifecycle_file_lock(
        path,
        owner_uid=0,
        owner_gid=0,
        validate_parent=True,
    ) as descriptor:
        yield descriptor


__all__ = ["HOST_LIFECYCLE_LOCK_PATH", "host_release_lifecycle_lock"]
