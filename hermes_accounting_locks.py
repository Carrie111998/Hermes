"""Cross-process advisory locks for pending session accounting."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import stat
from typing import Iterator


class PendingSessionAccountingError(RuntimeError):
    """Raised when cold archive and pending accounting overlap."""


def _lock_directory(db_path: Path) -> Path:
    db_path = Path(os.path.abspath(os.fspath(db_path)))
    return db_path.parent / f".{db_path.name}.accounting-locks"


def _open_lock_file(db_path: Path, session_id: str) -> int:
    lock_dir = _lock_directory(db_path)
    lock_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
    directory_stat = lock_dir.lstat()
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != os.geteuid()
        or stat.S_IMODE(directory_stat.st_mode) & 0o077
    ):
        raise PendingSessionAccountingError(
            f"unsafe pending-accounting lock directory: {lock_dir}"
        )
    name = sha256(session_id.encode("utf-8")).hexdigest() + ".lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_fd = os.open(
        lock_dir,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except OSError as exc:
        raise PendingSessionAccountingError(
            f"could not open pending-accounting lock for session {session_id}"
        ) from exc
    finally:
        os.close(directory_fd)
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise PendingSessionAccountingError(
            f"unsafe pending-accounting lock for session {session_id}"
        )
    return descriptor


def acquire_pending_accounting_lock(db_path: Path, session_id: str) -> int | None:
    """Acquire a shared lock held while one owner has queued/in-flight deltas."""
    if os.name != "posix":
        return None
    import fcntl

    descriptor = _open_lock_file(db_path, session_id)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise PendingSessionAccountingError(
            f"session {session_id} is locked for cold archive"
        ) from exc
    return descriptor


@contextmanager
def exclusive_lineage_accounting_locks(
    db_path: Path,
    session_ids: tuple[str, ...],
) -> Iterator[None]:
    """Exclude queued/in-flight accounting for every physical lineage ID."""
    if os.name != "posix":
        raise PendingSessionAccountingError(
            "pending-accounting locks require a POSIX runtime"
        )
    import fcntl

    descriptors: list[int] = []
    try:
        for session_id in sorted(set(session_ids)):
            descriptor = _open_lock_file(db_path, session_id)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(descriptor)
                raise PendingSessionAccountingError(
                    f"pending token accounting exists for session {session_id}"
                ) from exc
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
