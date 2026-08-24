"""Cross-process ownership for mutations of ``hermes_cli/web_dist``.

Dashboard builds and update rollback both replace the generated bundle tree.
They must share one OS-held lock or a build can race checkpoint capture or an
artifact swap, producing a Git/venv/dashboard generation that never existed.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import Optional, Type


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


def _validate_no_reparse_topology(path: Path) -> None:
    """Reject links/reparse points in every existing path component."""

    current = Path(os.path.abspath(path))
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise WebDistLockError(
                f"cannot inspect dashboard lock path {current}: {exc}"
            ) from exc
        else:
            if stat.S_ISLNK(metadata.st_mode) or bool(
                getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
            ):
                raise WebDistLockError(
                    f"dashboard lock path contains a link or reparse point: {current}"
                )
        if current == current.parent:
            return
        current = current.parent


def _open_no_follow(path: Path) -> int:
    """Open/create the lock without following a final link or reparse point."""

    if sys.platform != "win32":
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(f"dashboard mutation lock is not a regular file: {path}")
            os.fchmod(fd, 0o600)
            return fd
        except BaseException:
            os.close(fd)
            raise

    import ctypes

    generic_read = 0x80000000
    generic_write = 0x40000000
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_always = 4
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    handle = create_file(
        str(path),
        generic_read | generic_write,
        share_all,
        None,
        open_always,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.WinError(error))
    fd: int | None = None
    transferred = False
    try:
        import msvcrt

        fd = msvcrt.open_osfhandle(int(handle), os.O_RDWR | os.O_BINARY)
        transferred = True
        metadata = os.fstat(fd)
        if bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT):
            raise OSError(f"dashboard mutation lock is a reparse point: {path}")
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"dashboard mutation lock is not a regular file: {path}")
        return fd
    except BaseException:
        if fd is not None:
            os.close(fd)
        elif not transferred:
            close_handle(handle)
        raise


class WebDistLockError(RuntimeError):
    """The dashboard mutation lock could not be opened or operated."""


class WebDistLockTimeout(WebDistLockError):
    """Another process retained the dashboard mutation lock past deadline."""


class WebDistLock:
    """Bounded, cross-platform exclusive file lock for dashboard mutations."""

    def __init__(self, project_root: Path, *, timeout_seconds: float) -> None:
        self.path = Path(project_root) / ".web_ui_build.lock"
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self._handle = None

    def __enter__(self) -> "WebDistLock":
        fd: int | None = None
        try:
            _validate_no_reparse_topology(self.path.parent)
            fd = _open_no_follow(self.path)
            self._handle = os.fdopen(fd, "r+b", buffering=0)
            fd = None
            _validate_no_reparse_topology(self.path)
            self._validate_path_identity()
            if os.name == "nt" and os.fstat(self._handle.fileno()).st_size == 0:
                self._handle.seek(0)
                self._handle.write(b"\0")
                self._handle.flush()
        except BaseException as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._close()
            if isinstance(exc, (OSError, WebDistLockError)):
                raise WebDistLockError(
                    f"cannot open dashboard mutation lock {self.path}: {exc}"
                ) from exc
            raise

        deadline = time.monotonic() + self.timeout_seconds
        try:
            while True:
                try:
                    self._try_acquire()
                    try:
                        # POSIX locks belong to inodes, not names. A contender
                        # may have replaced the path while we waited; accepting
                        # the now-orphaned inode would create two simultaneous
                        # lock domains.
                        _validate_no_reparse_topology(self.path)
                        self._validate_path_identity()
                    except (OSError, WebDistLockError) as exc:
                        self.__exit__(None, None, None)
                        raise WebDistLockError(
                            "dashboard mutation lock changed while waiting: "
                            f"{self.path}: {exc}"
                        ) from exc
                    return self
                except OSError as exc:
                    if exc.errno not in {
                        errno.EACCES,
                        errno.EAGAIN,
                        errno.EDEADLK,
                    }:
                        raise WebDistLockError(
                            f"cannot acquire dashboard mutation lock {self.path}: {exc}"
                        ) from exc
                    if time.monotonic() >= deadline:
                        raise WebDistLockTimeout(
                            f"timed out waiting for dashboard mutation lock {self.path}"
                        ) from exc
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        except BaseException:
            if self._handle is not None:
                self.__exit__(None, None, None)
            raise

    def _validate_path_identity(self) -> None:
        if self._handle is None:
            raise WebDistLockError("dashboard mutation lock is not open")
        path_metadata = self.path.lstat()
        handle_metadata = os.fstat(self._handle.fileno())
        if (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ) != (
            handle_metadata.st_dev,
            handle_metadata.st_ino,
        ):
            raise WebDistLockError(
                f"dashboard mutation lock path does not name the open file: {self.path}"
            )

    def _try_acquire(self) -> None:
        if self._handle is None:
            raise WebDistLockError("dashboard mutation lock is not open")
        if os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            # Closing the descriptor releases an OS-held lock even if the
            # explicit unlock reports an error.
            pass
        finally:
            self._close()

    def _close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None


def web_dist_lock(project_root: Path, *, timeout_seconds: float) -> WebDistLock:
    return WebDistLock(project_root, timeout_seconds=timeout_seconds)
