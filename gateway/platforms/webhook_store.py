"""Profile-scoped, crash-safe persistence for webhook route documents.

The store is a management authority, not a replacement for runtime intake's
content-hash and revocation publication gates.  It provides descriptor-stable
reads, an interprocess read/modify/write lock, deterministic serialization,
and an explicit runtime quarantine mode.  Ordinary callers fail closed on
corruption instead of silently treating a broken file as an empty route set.
"""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import secrets
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Literal, Mapping

from gateway.platforms.webhook_models import (
    WebhookRouteDocument,
    from_persisted_route,
    to_persisted_route,
)


_FILENAME = "webhook_subscriptions.json"
_LOCK_FILENAME = ".webhook_subscriptions.lock"
_FILE_MODE = 0o600
_DIRECTORY_MODE = 0o700
_MAX_STORE_BYTES = 4 * 1024 * 1024
_LOCK_TIMEOUT_SECONDS = 30.0
_WINDOWS_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_MOVE_REPLACE_EXISTING = 0x00000001
_WINDOWS_MOVE_WRITE_THROUGH = 0x00000008

try:
    import fcntl
except ImportError:  # pragma: no cover - native Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


class WebhookRouteStoreError(RuntimeError):
    """Base class for route-store failures."""


class WebhookRouteStoreUnsafePathError(WebhookRouteStoreError):
    """A store path contains traversal, a symlink, or a non-directory node."""


class WebhookRouteStoreCorruptError(WebhookRouteStoreError):
    """A route file exists but is not one complete valid store document."""

    def __init__(
        self,
        message: str,
        *,
        content_sha256: str | None = None,
        file_identity: tuple[int, ...] | None = None,
    ):
        super().__init__(message)
        self.content_sha256 = content_sha256
        # Oversize runtime files are deliberately not read in full merely to
        # hash them.  Retain the descriptor identity so quarantine can still
        # move only the exact inode that failed the bounded-read contract.
        self.file_identity = file_identity


@dataclass(frozen=True)
class WebhookRouteStoreSnapshot:
    """One descriptor-stable store view suitable for runtime publication."""

    routes: Mapping[str, WebhookRouteDocument]
    content_sha256: str | None
    file_identity: tuple[int, ...] | None
    path: Path
    quarantined_path: Path | None = None


@dataclass(frozen=True)
class _RawSnapshot:
    content: bytes
    identity: tuple[int, ...]


@dataclass
class _DirectoryAuthority:
    """A profile directory pinned for the lifetime of one store operation."""

    path: Path
    fd: int | None = None
    windows_handle: object | None = None


def _win32() -> tuple[Any, ...]:
    """Import the optional native-Windows API only when it is needed."""

    return (
        importlib.import_module("ntsecuritycon"),
        importlib.import_module("pywintypes"),
        importlib.import_module("win32api"),
        importlib.import_module("win32con"),
        importlib.import_module("win32file"),
        importlib.import_module("win32security"),
    )


def _windows_move_flags(*, replace_existing: bool) -> int:
    """Return durable Win32 publication flags as platform-independent data."""

    flags = _WINDOWS_MOVE_WRITE_THROUGH
    if replace_existing:
        flags |= _WINDOWS_MOVE_REPLACE_EXISTING
    return flags


def _windows_current_sid():
    _, _, win32api, win32con, _, win32security = _win32()
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32con.TOKEN_QUERY,
    )
    try:
        return win32security.GetTokenInformation(
            token,
            win32security.TokenUser,
        )[0]
    finally:
        close = getattr(token, "Close", None)
        if callable(close):
            close()


def _windows_allowed_sids() -> set[str]:
    *_, win32security = _win32()
    return {
        win32security.ConvertSidToStringSid(_windows_current_sid()),
        win32security.ConvertSidToStringSid(
            win32security.ConvertStringSidToSid("S-1-5-18")
        ),
        win32security.ConvertSidToStringSid(
            win32security.ConvertStringSidToSid("S-1-5-32-544")
        ),
    }


def _windows_security_attributes():
    ntsecuritycon, _, _, _, _, win32security = _win32()
    owner = _windows_current_sid()
    system = win32security.ConvertStringSidToSid("S-1-5-18")
    administrators = win32security.ConvertStringSidToSid("S-1-5-32-544")
    acl = win32security.ACL()
    for sid in (owner, system, administrators):
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION,
            0,
            ntsecuritycon.FILE_ALL_ACCESS,
            sid,
        )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(owner, False)
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    descriptor.SetSecurityDescriptorControl(
        win32security.SE_DACL_PROTECTED,
        win32security.SE_DACL_PROTECTED,
    )
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def _verify_windows_security(handle: object, *, label: str) -> None:
    """Require current-user ownership and a private Windows DACL."""

    *_, win32security = _win32()
    info = (
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
    )
    descriptor = win32security.GetSecurityInfo(
        handle,
        win32security.SE_FILE_OBJECT,
        info,
    )
    owner = descriptor.GetSecurityDescriptorOwner()
    current = win32security.ConvertSidToStringSid(_windows_current_sid())
    if win32security.ConvertSidToStringSid(owner) != current:
        raise WebhookRouteStoreUnsafePathError(
            f"{label} must be owned by the current account"
        )
    dacl = descriptor.GetSecurityDescriptorDacl()
    if dacl is None:
        raise WebhookRouteStoreUnsafePathError(f"{label} has a null DACL")
    allowed = _windows_allowed_sids()
    allow_types = {
        win32security.ACCESS_ALLOWED_ACE_TYPE,
        win32security.ACCESS_ALLOWED_OBJECT_ACE_TYPE,
        getattr(win32security, "ACCESS_ALLOWED_CALLBACK_ACE_TYPE", 9),
        getattr(win32security, "ACCESS_ALLOWED_CALLBACK_OBJECT_ACE_TYPE", 11),
    }
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        if ace[0][0] in allow_types and ace[1]:
            sid = win32security.ConvertSidToStringSid(ace[-1])
            if sid not in allowed:
                raise WebhookRouteStoreUnsafePathError(f"{label} has a permissive DACL")


def _windows_final_path(handle: object) -> Path:
    _, _, _, _, win32file, _ = _win32()
    value = win32file.GetFinalPathNameByHandle(handle, 0)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _verify_windows_handle(
    handle: object,
    expected: Path,
    *,
    label: str,
    require_directory: bool,
) -> Path:
    _, _, _, _, win32file, _ = _win32()
    actual = _windows_final_path(handle)
    if os.path.normcase(os.path.abspath(actual)) != os.path.normcase(
        os.path.abspath(expected)
    ):
        raise WebhookRouteStoreUnsafePathError(f"{label} escaped its expected path")
    attributes = int(win32file.GetFileInformationByHandle(handle)[0])
    directory_flag = int(getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if attributes & reparse_flag:
        raise WebhookRouteStoreUnsafePathError(f"{label} must not be a reparse point")
    if bool(attributes & directory_flag) != require_directory:
        kind = "directory" if require_directory else "regular file"
        raise WebhookRouteStoreUnsafePathError(f"{label} is not a {kind}")
    _verify_windows_security(handle, label=label)
    return actual


def _close_directory(authority: _DirectoryAuthority) -> None:
    if authority.fd is not None:
        os.close(authority.fd)
        authority.fd = None
    if authority.windows_handle is not None:
        _, _, _, _, win32file, _ = _win32()
        win32file.CloseHandle(authority.windows_handle)
        authority.windows_handle = None


def _directory_fd(authority: _DirectoryAuthority) -> int:
    if authority.fd is None:
        raise WebhookRouteStoreError("profile directory has no POSIX descriptor")
    return authority.fd


_thread_locks_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}


def _thread_lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path))
    with _thread_locks_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _thread_locks[key] = lock
        return lock


def _absolute_without_traversal(path: str | os.PathLike[str]) -> Path:
    try:
        raw = Path(path).expanduser()
        if "\x00" in os.fspath(raw):
            raise ValueError("embedded null byte")
        if ".." in raw.parts:
            raise WebhookRouteStoreUnsafePathError(
                "webhook store root contains traversal"
            )
        return Path(os.path.abspath(os.fspath(raw)))
    except WebhookRouteStoreUnsafePathError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise WebhookRouteStoreUnsafePathError(
            "webhook store root is not a valid path"
        ) from exc


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _is_reparse_point(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _assert_owned_single_link(value: os.stat_result, *, label: str) -> None:
    """Reject hard-linked or foreign-owned store authority before mutation."""

    if int(value.st_nlink) != 1:
        raise WebhookRouteStoreUnsafePathError(
            f"{label} must have exactly one filesystem link"
        )
    get_effective_uid = getattr(os, "geteuid", None)
    if callable(get_effective_uid) and int(value.st_uid) != int(get_effective_uid()):
        raise WebhookRouteStoreUnsafePathError(
            f"{label} must be owned by the current account"
        )


def _assert_safe_profile_directory(fd: int) -> None:
    """Reject a profile directory another local account can replace entries in."""

    observed = os.fstat(fd)
    if not stat.S_ISDIR(observed.st_mode):
        raise WebhookRouteStoreUnsafePathError(
            "webhook store parent is not a directory"
        )
    get_effective_uid = getattr(os, "geteuid", None)
    if callable(get_effective_uid):
        if int(observed.st_uid) != int(get_effective_uid()):
            raise WebhookRouteStoreUnsafePathError(
                "webhook profile directory must be owned by the current account"
            )
        if stat.S_IMODE(observed.st_mode) & 0o022:
            raise WebhookRouteStoreUnsafePathError(
                "webhook profile directory must not be group- or world-writable"
            )


def _assert_no_link_components(path: Path, *, allow_missing: bool) -> None:
    """Best available non-POSIX containment check.

    POSIX operations use a descriptor walk with ``O_NOFOLLOW`` below.  This
    fallback also rejects Windows junctions/reparse points, not just Python
    symlinks.
    """

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            node = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                continue
            raise WebhookRouteStoreUnsafePathError(
                "webhook store path does not exist"
            ) from None
        except OSError as exc:
            raise WebhookRouteStoreUnsafePathError(
                "webhook store path cannot be inspected"
            ) from exc
        if stat.S_ISLNK(node.st_mode) or _is_reparse_point(node):
            raise WebhookRouteStoreUnsafePathError(
                "webhook store path must not contain links"
            )
        if not stat.S_ISDIR(node.st_mode):
            raise WebhookRouteStoreUnsafePathError(
                "webhook store parent is not a directory"
            )


def _open_windows_directory_tree(path: Path) -> _DirectoryAuthority:
    _, pywintypes, _, win32con, win32file, _ = _win32()
    _assert_no_link_components(path, allow_missing=True)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            win32file.CreateDirectory(
                str(current),
                _windows_security_attributes(),
            )
        except pywintypes.error as exc:
            if getattr(exc, "winerror", None) not in {80, 183}:
                raise WebhookRouteStoreUnsafePathError(
                    "webhook store directory cannot be created"
                ) from exc
    _assert_no_link_components(path, allow_missing=False)
    handle = win32file.CreateFile(
        str(path),
        win32con.GENERIC_READ | win32con.READ_CONTROL,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
        None,
        win32con.OPEN_EXISTING,
        win32con.FILE_FLAG_BACKUP_SEMANTICS | _WINDOWS_OPEN_REPARSE_POINT,
        None,
    )
    try:
        actual = _verify_windows_handle(
            handle,
            path,
            label="webhook profile directory",
            require_directory=True,
        )
        return _DirectoryAuthority(actual, windows_handle=handle)
    except BaseException:
        win32file.CloseHandle(handle)
        raise


def _open_directory_tree(path: Path) -> _DirectoryAuthority:
    """Open/create an absolute directory tree without following symlinks."""

    if os.name == "nt":
        return _open_windows_directory_tree(path)

    secure_posix = (
        os.name == "posix"
        and os.open in getattr(os, "supports_dir_fd", set())
        and bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NOFOLLOW", 0))
    )
    if not secure_posix:
        _assert_no_link_components(path, allow_missing=True)
        path.mkdir(parents=True, exist_ok=True, mode=_DIRECTORY_MODE)
        _assert_no_link_components(path, allow_missing=False)
        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        descriptor = os.open(path, flags)
        try:
            _assert_safe_profile_directory(descriptor)
            return _DirectoryAuthority(path, fd=descriptor)
        except BaseException:
            os.close(descriptor)
            raise

    flags = (
        os.O_RDONLY
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_DIRECTORY", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
    )
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            if part in {"", ".", ".."}:
                raise WebhookRouteStoreUnsafePathError(
                    "webhook store path contains an unsafe component"
                )
            try:
                os.mkdir(part, _DIRECTORY_MODE, dir_fd=descriptor)
            except FileExistsError:
                pass
            except OSError as exc:
                raise WebhookRouteStoreUnsafePathError(
                    "webhook store directory cannot be created"
                ) from exc
            try:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                raise WebhookRouteStoreUnsafePathError(
                    "webhook store path must contain only real directories"
                ) from exc
            os.close(descriptor)
            descriptor = next_descriptor
        _assert_safe_profile_directory(descriptor)
        return _DirectoryAuthority(path, fd=descriptor)
    except BaseException:
        os.close(descriptor)
        raise


def _read_all(fd: int, max_bytes: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining:
        chunk = os.read(fd, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    result = b"".join(chunks)
    if len(result) > max_bytes:
        raise WebhookRouteStoreCorruptError(
            "webhook route store exceeds its size limit"
        )
    return result


def _open_windows_file(
    authority: _DirectoryAuthority,
    name: str,
    flags: int,
    *,
    create_new: bool = False,
    share_delete: bool,
) -> int:
    _, _, _, win32con, win32file, _ = _win32()
    if authority.windows_handle is None:
        raise WebhookRouteStoreError("profile directory has no Windows handle")
    candidate = authority.path / name
    readable = not bool(flags & os.O_WRONLY) or bool(flags & os.O_RDWR)
    writable = bool(flags & (os.O_WRONLY | os.O_RDWR))
    access = win32con.READ_CONTROL
    if readable:
        access |= win32con.GENERIC_READ
    if writable:
        access |= win32con.GENERIC_WRITE
    share = win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE
    if share_delete:
        share |= win32con.FILE_SHARE_DELETE
    creation = win32con.CREATE_NEW if create_new else win32con.OPEN_EXISTING
    security = _windows_security_attributes() if create_new else None
    try:
        handle = win32file.CreateFile(
            str(candidate),
            access,
            share,
            security,
            creation,
            win32con.FILE_ATTRIBUTE_NORMAL | _WINDOWS_OPEN_REPARSE_POINT,
            None,
        )
    except Exception as exc:
        winerror = getattr(exc, "winerror", None)
        if winerror in {2, 3}:
            raise FileNotFoundError(
                errno.ENOENT, os.strerror(errno.ENOENT), candidate
            ) from None
        if winerror in {80, 183}:
            raise FileExistsError(
                errno.EEXIST, os.strerror(errno.EEXIST), candidate
            ) from None
        raise
    try:
        _verify_windows_handle(
            handle,
            candidate,
            label="webhook store file",
            require_directory=False,
        )
        raw_handle = handle.Detach()
    except BaseException:
        win32file.CloseHandle(handle)
        raise
    descriptor_flags = flags | int(getattr(os, "O_BINARY", 0))
    descriptor_flags |= int(getattr(os, "O_NOINHERIT", 0))
    try:
        open_osfhandle = getattr(msvcrt, "open_osfhandle", None)
        if not callable(open_osfhandle):  # pragma: no cover - native Windows has it
            raise WebhookRouteStoreError(
                "Windows CRT descriptor support is unavailable"
            )
        return open_osfhandle(raw_handle, descriptor_flags)
    except BaseException:
        win32file.CloseHandle(raw_handle)
        raise


def _open_relative_file(
    authority: _DirectoryAuthority,
    directory: Path,
    name: str,
    flags: int,
) -> int:
    if authority.windows_handle is not None:
        return _open_windows_file(
            authority,
            name,
            flags,
            share_delete=name != _LOCK_FILENAME,
        )
    directory_fd = _directory_fd(authority)
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    if os.open in getattr(os, "supports_dir_fd", set()):
        return os.open(name, flags, dir_fd=directory_fd)
    candidate = directory / name
    try:
        node = os.lstat(candidate)
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(node.st_mode) or _is_reparse_point(node):
        raise WebhookRouteStoreUnsafePathError("webhook store file must not be a link")
    return os.open(candidate, flags)


def _replace_relative(
    authority: _DirectoryAuthority,
    source: str,
    destination: str,
    *,
    replace_existing: bool,
) -> None:
    if authority.windows_handle is not None:
        _, _, _, _, win32file, _ = _win32()
        _verify_windows_handle(
            authority.windows_handle,
            authority.path,
            label="webhook profile directory",
            require_directory=True,
        )
        win32file.MoveFileEx(
            str(authority.path / source),
            str(authority.path / destination),
            _windows_move_flags(replace_existing=replace_existing),
        )
        return
    directory_fd = _directory_fd(authority)
    if os.replace in getattr(os, "supports_dir_fd", set()):
        os.replace(
            source,
            destination,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        return
    target = authority.path / destination
    if not replace_existing and target.exists():
        raise WebhookRouteStoreError("webhook quarantine target already exists")
    os.replace(authority.path / source, target)


def _sync_directory(authority: _DirectoryAuthority) -> None:
    # Windows has no supported equivalent of fsync on a directory handle.
    # Every namespace publication above instead uses MOVEFILE_WRITE_THROUGH.
    if authority.windows_handle is None:
        os.fsync(_directory_fd(authority))


def _unlink_relative(authority: _DirectoryAuthority, name: str) -> None:
    if authority.windows_handle is not None:
        (authority.path / name).unlink()
        return
    directory_fd = _directory_fd(authority)
    if os.unlink in getattr(os, "supports_dir_fd", set()):
        os.unlink(name, dir_fd=directory_fd)
    else:
        (authority.path / name).unlink()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("webhook route store contains a duplicate key")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> object:
    raise ValueError("webhook route store contains a non-finite number")


def _parse_finite_float(value: str) -> float:
    """Reject exponent overflow as well as JSON's named NaN/Infinity values."""

    parsed = float(value)
    if parsed == float("inf") or parsed == float("-inf"):
        raise ValueError("webhook route store contains a non-finite number")
    return parsed


class WebhookRouteStore:
    """Persist one profile's route documents beneath a shared Hermes root."""

    def __init__(self, root: str | os.PathLike[str], profile: str = "default"):
        from hermes_cli.profiles import normalize_profile_name, validate_profile_name

        canonical_profile = normalize_profile_name(profile)
        validate_profile_name(canonical_profile)
        if canonical_profile != profile:
            raise ValueError("webhook store profile must already be canonical")
        self.root = _absolute_without_traversal(root)
        self.profile = canonical_profile
        self._thread_lock = _thread_lock_for(self.lock_path)

    @classmethod
    def for_profile_home(
        cls,
        profile: str,
        home: str | os.PathLike[str],
    ) -> "WebhookRouteStore":
        """Construct from an exact profile home returned by admission."""

        home_path = _absolute_without_traversal(home)
        if profile == "default":
            return cls(home_path, profile="default")
        if home_path.name != profile or home_path.parent.name != "profiles":
            raise WebhookRouteStoreUnsafePathError(
                "served profile home does not match its canonical profile id"
            )
        return cls(home_path.parent.parent, profile=profile)

    @property
    def profile_root(self) -> Path:
        if self.profile == "default":
            return self.root
        return self.root / "profiles" / self.profile

    @property
    def path(self) -> Path:
        return self.profile_root / _FILENAME

    @property
    def lock_path(self) -> Path:
        return self.profile_root / _LOCK_FILENAME

    @contextmanager
    def _lock(
        self,
        *,
        timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
    ) -> Iterator[_DirectoryAuthority]:
        """Yield a pinned profile-directory descriptor under both locks."""

        if timeout_seconds < 0:
            raise ValueError("webhook route lock timeout must be non-negative")
        deadline = time.monotonic() + timeout_seconds
        acquired = self._thread_lock.acquire(timeout=timeout_seconds)
        if not acquired:
            raise TimeoutError("webhook route store is busy")
        try:
            directory_fd = _open_directory_tree(self.profile_root)
            lock_fd = -1
            directory_locked = False
            sidecar_locked = False
            try:
                # POSIX exclusion belongs to the pinned profile-directory
                # inode. A sidecar pathname can be atomically replaced after a
                # writer opens it, splitting writers across unrelated flock
                # inodes. The already-open directory descriptor cannot be
                # swapped out from under this operation.
                if fcntl is not None:
                    while True:
                        try:
                            fcntl.flock(
                                _directory_fd(directory_fd),
                                fcntl.LOCK_EX | fcntl.LOCK_NB,
                            )
                            directory_locked = True
                            break
                        except OSError as exc:
                            if exc.errno not in {
                                errno.EACCES,
                                errno.EAGAIN,
                                errno.EWOULDBLOCK,
                            }:
                                raise
                            if time.monotonic() >= deadline:
                                raise TimeoutError(
                                    "webhook route store is busy"
                                ) from exc
                            time.sleep(0.01)

                if fcntl is not None:
                    # A legacy sidecar is metadata only on POSIX. Validate it
                    # if present, but never mutate or lock it; directory flock
                    # above is the stable exclusion authority.
                    flags = os.O_RDONLY | int(getattr(os, "O_NONBLOCK", 0))
                    try:
                        lock_fd = _open_relative_file(
                            directory_fd,
                            self.profile_root,
                            _LOCK_FILENAME,
                            flags,
                        )
                    except FileNotFoundError:
                        lock_fd = -1
                    except OSError as exc:
                        if exc.errno in {errno.ELOOP, errno.EMLINK}:
                            raise WebhookRouteStoreUnsafePathError(
                                "webhook route lock must not be a link"
                            ) from exc
                        raise
                    if lock_fd >= 0:
                        lock_stat = os.fstat(lock_fd)
                        if not stat.S_ISREG(lock_stat.st_mode):
                            raise WebhookRouteStoreUnsafePathError(
                                "webhook route lock is not a regular file"
                            )
                        _assert_owned_single_link(
                            lock_stat,
                            label="webhook route lock",
                        )
                        if (
                            stat.S_IMODE(lock_stat.st_mode) != _FILE_MODE
                            or lock_stat.st_size < 1
                        ):
                            raise WebhookRouteStoreUnsafePathError(
                                "webhook route lock has unsafe metadata"
                            )
                else:
                    # Native Windows needs a byte-range sidecar lock. Create
                    # it once with exclusive creation, or validate the exact
                    # descriptor opened without following reparse points.
                    flags = (
                        os.O_RDWR
                        | int(getattr(os, "O_CLOEXEC", 0))
                        | int(getattr(os, "O_NOFOLLOW", 0))
                    )
                    created_lock = False
                    try:
                        if directory_fd.windows_handle is not None:
                            lock_fd = _open_windows_file(
                                directory_fd,
                                _LOCK_FILENAME,
                                flags,
                                create_new=True,
                                share_delete=False,
                            )
                        else:
                            lock_fd = os.open(
                                self.profile_root / _LOCK_FILENAME,
                                flags | os.O_CREAT | os.O_EXCL,
                                _FILE_MODE,
                            )
                        created_lock = True
                    except FileExistsError:
                        try:
                            lock_fd = _open_relative_file(
                                directory_fd,
                                self.profile_root,
                                _LOCK_FILENAME,
                                flags,
                            )
                        except OSError as exc:
                            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                                raise WebhookRouteStoreUnsafePathError(
                                    "webhook route lock must not be a link"
                                ) from exc
                            raise
                    lock_stat = os.fstat(lock_fd)
                    if not stat.S_ISREG(lock_stat.st_mode):
                        raise WebhookRouteStoreUnsafePathError(
                            "webhook route lock is not a regular file"
                        )
                    _assert_owned_single_link(
                        lock_stat,
                        label="webhook route lock",
                    )
                    if created_lock or lock_stat.st_size < 1:
                        os.lseek(lock_fd, 0, os.SEEK_SET)
                        os.write(lock_fd, b"0")
                        os.fsync(lock_fd)
                    os.lseek(lock_fd, 0, os.SEEK_SET)
                    while True:
                        try:
                            if msvcrt is not None:
                                msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
                                sidecar_locked = True
                            else:  # pragma: no cover - unsupported interpreter
                                raise WebhookRouteStoreError(
                                    "no interprocess webhook lock is available"
                                )
                            break
                        except OSError as exc:
                            if exc.errno not in {
                                errno.EACCES,
                                errno.EAGAIN,
                                errno.EDEADLK,
                                errno.EWOULDBLOCK,
                            }:
                                raise
                            if time.monotonic() >= deadline:
                                raise TimeoutError(
                                    "webhook route store is busy"
                                ) from exc
                            time.sleep(0.01)
                try:
                    yield directory_fd
                finally:
                    if sidecar_locked and msvcrt is not None:
                        os.lseek(lock_fd, 0, os.SEEK_SET)
                        msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
                    if directory_locked and fcntl is not None:
                        fcntl.flock(_directory_fd(directory_fd), fcntl.LOCK_UN)
            finally:
                if lock_fd >= 0:
                    os.close(lock_fd)
                _close_directory(directory_fd)
        finally:
            self._thread_lock.release()

    def _raw_snapshot_unlocked(
        self,
        directory_fd: _DirectoryAuthority,
    ) -> _RawSnapshot | None:
        try:
            fd = _open_relative_file(
                directory_fd,
                self.profile_root,
                _FILENAME,
                os.O_RDONLY | int(getattr(os, "O_NONBLOCK", 0)),
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise WebhookRouteStoreUnsafePathError(
                    "webhook route store must not be a link"
                ) from exc
            raise
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise WebhookRouteStoreUnsafePathError(
                    "webhook route store is not a regular file"
                )
            _assert_owned_single_link(
                opened,
                label="webhook route store",
            )
            if hasattr(os, "fchmod"):
                os.fchmod(fd, _FILE_MODE)
            # Tightening legacy permissions changes ctime; capture the stable
            # identity only after that one authorized metadata mutation.
            before = os.fstat(fd)
            if before.st_size > _MAX_STORE_BYTES:
                raise WebhookRouteStoreCorruptError(
                    "webhook route store exceeds its size limit",
                    file_identity=_stat_identity(before),
                )
            first = _read_all(fd, _MAX_STORE_BYTES)
            middle = os.fstat(fd)
            second = _read_all(fd, _MAX_STORE_BYTES)
            after = os.fstat(fd)
            if (
                _stat_identity(before) != _stat_identity(middle)
                or _stat_identity(before) != _stat_identity(after)
                or len(first) != before.st_size
                or first != second
            ):
                raise WebhookRouteStoreError(
                    "webhook route store changed while being read"
                )
            return _RawSnapshot(second, _stat_identity(after))
        finally:
            os.close(fd)

    def _parse(self, raw: bytes) -> dict[str, WebhookRouteDocument]:
        digest = hashlib.sha256(raw).hexdigest()
        try:
            decoded = raw.decode("utf-8")
            data = json.loads(
                decoded,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
                parse_float=_parse_finite_float,
            )
            if not isinstance(data, dict):
                raise ValueError("webhook route store root must be an object")
            # Python's JSON decoder accepts escaped lone UTF-16 surrogates,
            # but they cannot be encoded as canonical UTF-8 on the next save.
            # Validate the complete tree now so every successful load is also
            # a serializable store document.
            json.dumps(
                data,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            routes: dict[str, WebhookRouteDocument] = {}
            for name, value in data.items():
                if not isinstance(name, str) or not isinstance(value, Mapping):
                    raise ValueError("webhook route entries must be named objects")
                document = from_persisted_route(
                    name,
                    value,
                    profile=self.profile,
                )
                if document.name != name:
                    raise ValueError(
                        "embedded webhook route name does not match its key"
                    )
                routes[name] = document
            return dict(sorted(routes.items()))
        except WebhookRouteStoreCorruptError:
            raise
        except Exception:
            raise WebhookRouteStoreCorruptError(
                "webhook route store is not a valid canonical document",
                content_sha256=digest,
            ) from None

    def _quarantine_unlocked(
        self,
        directory_fd: _DirectoryAuthority,
        identity: tuple[int, ...],
    ) -> Path:
        """Move only the inode whose exact bytes failed validation."""

        if directory_fd.windows_handle is not None:
            current_fd = _open_relative_file(
                directory_fd,
                self.profile_root,
                _FILENAME,
                os.O_RDONLY,
            )
            try:
                current = os.fstat(current_fd)
            finally:
                os.close(current_fd)
        elif os.stat in getattr(os, "supports_dir_fd", set()):
            current = os.stat(
                _FILENAME,
                dir_fd=_directory_fd(directory_fd),
                follow_symlinks=False,
            )
        else:
            current = os.lstat(self.path)
        if not stat.S_ISREG(current.st_mode) or _stat_identity(current) != identity:
            raise WebhookRouteStoreError(
                "webhook route store changed before corrupt quarantine"
            )
        _assert_owned_single_link(
            current,
            label="webhook route store",
        )
        quarantine_name = (
            f"{_FILENAME}.corrupt-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-"
            f"{secrets.token_hex(8)}"
        )
        _replace_relative(
            directory_fd,
            _FILENAME,
            quarantine_name,
            replace_existing=False,
        )
        try:
            _sync_directory(directory_fd)
        except OSError:
            pass
        return self.profile_root / quarantine_name

    def load_snapshot(
        self,
        *,
        on_corrupt: Literal["raise", "quarantine"] = "raise",
        lock_timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
    ) -> WebhookRouteStoreSnapshot:
        """Load one stable snapshot; corruption handling is always explicit."""

        if on_corrupt not in {"raise", "quarantine"}:
            raise ValueError("on_corrupt must be 'raise' or 'quarantine'")
        with self._lock(timeout_seconds=lock_timeout_seconds) as directory_fd:
            try:
                raw = self._raw_snapshot_unlocked(directory_fd)
            except WebhookRouteStoreCorruptError as exc:
                if on_corrupt == "raise" or exc.file_identity is None:
                    raise
                quarantined = self._quarantine_unlocked(
                    directory_fd,
                    exc.file_identity,
                )
                return WebhookRouteStoreSnapshot(
                    {},
                    exc.content_sha256,
                    exc.file_identity,
                    self.path,
                    quarantined_path=quarantined,
                )
            if raw is None:
                return WebhookRouteStoreSnapshot({}, None, None, self.path)
            digest = hashlib.sha256(raw.content).hexdigest()
            try:
                routes = self._parse(raw.content)
            except WebhookRouteStoreCorruptError:
                if on_corrupt == "raise":
                    raise
                quarantined = self._quarantine_unlocked(directory_fd, raw.identity)
                return WebhookRouteStoreSnapshot(
                    {},
                    digest,
                    raw.identity,
                    self.path,
                    quarantined_path=quarantined,
                )
            return WebhookRouteStoreSnapshot(
                routes,
                digest,
                raw.identity,
                self.path,
            )

    def load(self) -> dict[str, WebhookRouteDocument]:
        """CLI/management read: malformed data raises and is left untouched."""

        return dict(self.load_snapshot(on_corrupt="raise").routes)

    def probe_identity(self) -> tuple[int, ...] | None:
        """Return a cheap descriptor-safe file identity without taking the lock.

        Management writes publish by atomic rename, so a reader either opens
        the previous complete inode or the next complete inode. Runtime uses
        this probe to avoid re-reading every admitted profile on every reload
        interval. A separately budgeted integrity pass still re-hashes one
        unchanged inode at a time to detect metadata-preserving writes.
        """

        directory_fd = _open_directory_tree(self.profile_root)
        try:
            try:
                fd = _open_relative_file(
                    directory_fd,
                    self.profile_root,
                    _FILENAME,
                    os.O_RDONLY | int(getattr(os, "O_NONBLOCK", 0)),
                )
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.EMLINK}:
                    raise WebhookRouteStoreUnsafePathError(
                        "webhook route store must not be a link"
                    ) from exc
                raise
            try:
                observed = os.fstat(fd)
                if not stat.S_ISREG(observed.st_mode):
                    raise WebhookRouteStoreUnsafePathError(
                        "webhook route store is not a regular file"
                    )
                _assert_owned_single_link(
                    observed,
                    label="webhook route store",
                )
                return _stat_identity(observed)
            finally:
                os.close(fd)
        finally:
            _close_directory(directory_fd)

    def load_runtime(self) -> WebhookRouteStoreSnapshot:
        """Runtime read: quarantine exact corrupt bytes and expose revocation."""

        # Request-path reloads must never wait behind a management writer.
        # The adapter retains the prior exact published snapshot on this busy
        # signal and retries after its amplification gate.
        return self.load_snapshot(
            on_corrupt="quarantine",
            lock_timeout_seconds=0.0,
        )

    def _normalize(
        self,
        routes: Mapping[str, WebhookRouteDocument | Mapping[str, object]],
    ) -> dict[str, WebhookRouteDocument]:
        normalized: dict[str, WebhookRouteDocument] = {}
        for name, value in routes.items():
            if not isinstance(name, str):
                raise TypeError("webhook route names must be strings")
            if isinstance(value, WebhookRouteDocument):
                declared_name = value.name
                raw_document = to_persisted_route(value)
            elif isinstance(value, Mapping):
                raw = dict(value)
                embedded_name = raw.pop("name", name)
                raw.setdefault("profile", self.profile)
                initial = WebhookRouteDocument.model_validate({
                    "name": embedded_name,
                    **raw,
                })
                declared_name = initial.name
                raw_document = to_persisted_route(initial)
            else:
                raise TypeError("webhook routes must be route documents or objects")
            if declared_name != name:
                raise ValueError("webhook route key does not match document name")

            # A Pydantic model's nested dict/list members remain mutable after
            # construction. Canonical JSON round-tripping detaches the store
            # snapshot, rejects non-finite/non-JSON mutations, and then runs
            # every model + canonical-contract validator again.
            try:
                detached = json.loads(
                    json.dumps(
                        raw_document,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ),
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_nonfinite,
                )
            except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
                raise WebhookRouteStoreError(
                    "webhook route mutation is not canonical JSON"
                ) from exc
            document = from_persisted_route(
                name,
                detached,
                profile=self.profile,
            )
            if document.name != name:
                raise ValueError("webhook route key does not match document name")
            if document.profile != self.profile:
                raise ValueError("webhook route belongs to a different profile store")
            normalized[name] = document
        return dict(sorted(normalized.items()))

    def _serialize(self, routes: Mapping[str, WebhookRouteDocument]) -> bytes:
        data = {
            name: to_persisted_route(route) for name, route in sorted(routes.items())
        }
        try:
            payload = (
                json.dumps(
                    data,
                    ensure_ascii=False,
                    allow_nan=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
            raise WebhookRouteStoreError(
                "webhook route documents are not canonical JSON"
            ) from exc
        if len(payload) > _MAX_STORE_BYTES:
            raise WebhookRouteStoreError("webhook route store exceeds its size limit")
        return payload

    def _save_unlocked(
        self,
        directory_fd: _DirectoryAuthority,
        routes: Mapping[str, WebhookRouteDocument],
    ) -> None:
        payload = self._serialize(routes)
        temporary_name = f".{_FILENAME}.{secrets.token_hex(16)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        temporary_fd = -1
        try:
            if directory_fd.windows_handle is not None:
                temporary_fd = _open_windows_file(
                    directory_fd,
                    temporary_name,
                    os.O_WRONLY,
                    create_new=True,
                    share_delete=False,
                )
            elif os.open in getattr(os, "supports_dir_fd", set()):
                temporary_fd = os.open(
                    temporary_name,
                    flags,
                    _FILE_MODE,
                    dir_fd=_directory_fd(directory_fd),
                )
            else:
                temporary_fd = os.open(
                    self.profile_root / temporary_name,
                    flags,
                    _FILE_MODE,
                )
            if hasattr(os, "fchmod"):
                os.fchmod(temporary_fd, _FILE_MODE)
            view = memoryview(payload)
            offset = 0
            while offset < len(view):
                written = os.write(temporary_fd, view[offset:])
                if written <= 0:
                    raise OSError("short webhook route store write")
                offset += written
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = -1

            # Reject a pre-existing link/non-regular target. A replacement
            # race cannot redirect the rename because both names are relative
            # to the already-open directory descriptor.
            try:
                if directory_fd.windows_handle is not None:
                    target_fd = _open_relative_file(
                        directory_fd,
                        self.profile_root,
                        _FILENAME,
                        os.O_RDONLY,
                    )
                    try:
                        target_stat = os.fstat(target_fd)
                    finally:
                        os.close(target_fd)
                elif os.stat in getattr(os, "supports_dir_fd", set()):
                    target_stat = os.stat(
                        _FILENAME,
                        dir_fd=_directory_fd(directory_fd),
                        follow_symlinks=False,
                    )
                else:
                    target_stat = os.lstat(self.path)
            except FileNotFoundError:
                target_stat = None
            if target_stat is not None and not stat.S_ISREG(target_stat.st_mode):
                raise WebhookRouteStoreUnsafePathError(
                    "webhook route store target is not a regular file"
                )
            if target_stat is not None:
                _assert_owned_single_link(
                    target_stat,
                    label="webhook route store target",
                )

            _replace_relative(
                directory_fd,
                temporary_name,
                _FILENAME,
                replace_existing=True,
            )
            _sync_directory(directory_fd)
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            try:
                _unlink_relative(directory_fd, temporary_name)
            except FileNotFoundError:
                pass

    def save(
        self,
        routes: Mapping[str, WebhookRouteDocument | Mapping[str, object]],
    ) -> None:
        normalized = self._normalize(routes)
        with self._lock() as directory_fd:
            self._save_unlocked(directory_fd, normalized)

    def update(
        self,
        mutator: Callable[
            [dict[str, WebhookRouteDocument]],
            Mapping[str, WebhookRouteDocument | Mapping[str, object]],
        ],
    ) -> dict[str, WebhookRouteDocument]:
        """Apply one lossless read/modify/write under the interprocess lock."""

        with self._lock() as directory_fd:
            raw = self._raw_snapshot_unlocked(directory_fd)
            current = {} if raw is None else self._parse(raw.content)
            updated = self._normalize(mutator(dict(current)))
            self._save_unlocked(directory_fd, updated)
            return updated


def stores_for_served_profiles(
    served_profiles: Iterable[tuple[str, str | os.PathLike[str]]],
) -> dict[str, WebhookRouteStore]:
    """Build stores from the exact profile homes admitted by the listener."""

    result: dict[str, WebhookRouteStore] = {}
    for profile, home in served_profiles:
        if profile in result:
            raise ValueError("served webhook profile list contains a duplicate")
        result[profile] = WebhookRouteStore.for_profile_home(profile, home)
    return result


__all__ = [
    "WebhookRouteStore",
    "WebhookRouteStoreCorruptError",
    "WebhookRouteStoreError",
    "WebhookRouteStoreSnapshot",
    "WebhookRouteStoreUnsafePathError",
    "stores_for_served_profiles",
]
