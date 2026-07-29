"""Cross-process locks for mutations of a profile's skill library.

``flock`` is advisory, so every Hermes-owned writer must use this module.  A
shared namespace lock permits independent per-skill writes to proceed in
parallel.  Structural changes (create, delete, archive, restore, and hub
installs) take the namespace lock exclusively, which prevents a directory
from being moved while another process is reading or rewriting it.
"""

from __future__ import annotations

import errno
import os
import time
import contextvars
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Iterator

from hermes_constants import get_hermes_home

try:  # Unix: lock the directory inode, leaving no lock-file to clean up.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows only
    fcntl = None
    import msvcrt
else:
    msvcrt = None


DEFAULT_TIMEOUT = 30.0
_namespace_lock_mode: contextvars.ContextVar[tuple[int, str] | None] = contextvars.ContextVar(
    "skill_namespace_lock_mode", default=None
)


class SkillLockTimeout(TimeoutError):
    """Raised when another Hermes process holds a skill-library lock too long."""


def _skills_dir() -> Path:
    return get_hermes_home() / "skills"


@contextmanager
def _lock_path(path: Path, *, exclusive: bool, timeout: float) -> Iterator[None]:
    """Lock *path*, polling so callers receive a useful bounded failure."""
    deadline = time.monotonic() + timeout
    if fcntl is not None:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            while True:
                try:
                    fcntl.flock(fd, mode | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise SkillLockTimeout(f"timed out waiting for lock on {path}")
                    time.sleep(0.05)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return

    # Windows cannot lock directories with msvcrt.  A single profile-local
    # sentinel keeps the same correctness contract, although it serializes
    # independent skill writes on that platform.
    lock_file = path / ".skill-write.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "a+b") as handle:  # pragma: no cover - Windows only
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b" ")
            handle.flush()
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise SkillLockTimeout(f"timed out waiting for lock on {lock_file}")
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def skills_namespace_lock(*, exclusive: bool = True, timeout: float = DEFAULT_TIMEOUT) -> Iterator[None]:
    """Lock the profile skill namespace.

    Readers take this shared while locating and modifying an existing skill;
    structural writers take it exclusively before checking names or moving
    directories.  The lock therefore covers the lookup-to-mutation interval.
    """
    # ``flock`` is not re-entrant across distinct opens in one process.  A
    # structural operation may call another structural helper (delete →
    # archive), so inherit an already-held namespace lock.  No code path may
    # upgrade a shared lock to exclusive; that would defeat the protocol.
    held = _namespace_lock_mode.get()
    if held is not None and held[0] == os.getpid():
        if exclusive and held[1] != "exclusive":
            raise RuntimeError("cannot upgrade a shared skill namespace lock")
        yield
        return

    root = _skills_dir()
    root.mkdir(parents=True, exist_ok=True)
    with _lock_path(root, exclusive=exclusive, timeout=timeout):
        token = _namespace_lock_mode.set(
            (os.getpid(), "exclusive" if exclusive else "shared")
        )
        try:
            yield
        finally:
            _namespace_lock_mode.reset(token)


@contextmanager
def skill_write_lock(skill_dir: Path, *, timeout: float = DEFAULT_TIMEOUT) -> Iterator[None]:
    """Exclusively lock an already-resolved skill directory.

    Callers must hold a shared :func:`skills_namespace_lock` while resolving
    and using ``skill_dir``.  This keeps unrelated skill writes concurrent but
    prevents a structural writer from renaming the directory mid-transaction.
    """
    if not skill_dir.is_dir():
        raise FileNotFoundError(f"skill directory no longer exists: {skill_dir}")
    with _lock_path(skill_dir, exclusive=True, timeout=timeout):
        yield


def namespace_write_locked(func):
    """Decorator for a complete skill-library structural transaction."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        with skills_namespace_lock():
            return func(*args, **kwargs)
    return wrapped
