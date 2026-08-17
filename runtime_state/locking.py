"""Small cross-process maintenance lock used by the runtime-state journal.

The lock is deliberately file based: the kernel releases it when a writer
dies, so a stale holder cannot strand the database in a maintenance state.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import time
from typing import Iterator

LOCK_ACQUIRE_TIMEOUT = 5.0


class LockTimeout(RuntimeError):
    """The named maintenance lock could not be acquired in bounded time."""


class MaintenanceLock:
    def __init__(self, path: Path, *, exclusive: bool, timeout: float = LOCK_ACQUIRE_TIMEOUT):
        self.path = Path(path)
        self.exclusive = exclusive
        self.timeout = timeout
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        mode = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
                        fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
                    self._handle = handle
                    return
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise LockTimeout("runtime-state maintenance lock timed out")
                    time.sleep(0.05)
        except Exception:
            handle.close()
            raise

    def release(self) -> None:
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "MaintenanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@contextmanager
def maintenance_lock(path: Path, *, exclusive: bool = False) -> Iterator[MaintenanceLock]:
    lock = MaintenanceLock(path, exclusive=exclusive)
    with lock:
        yield lock
