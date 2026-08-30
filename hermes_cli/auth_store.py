"""Persistence and recovery for Hermes authentication stores.

The callable bodies below are the byte-verbatim Auth Store persistence shard
from ``hermes_cli.auth``. Runtime dependencies that belong to the facade use
late bindings so existing imports and monkeypatch targets keep their authority.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import importlib
import json
import os
import stat
import struct
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


_ORIGINAL_PATH_READ_TEXT = Path.read_text
_ORIGINAL_OS_REPLACE = os.replace


class AuthStoreCorruptionError(RuntimeError):
    """The auth store is corrupt and must remain read-only until recovery."""

    def __init__(
        self,
        auth_file: Path,
        corrupt_path: Optional[Path],
        *,
        preserved: bool,
        corrupt_sha256: Optional[str] = None,
    ) -> None:
        self.auth_file = auth_file
        self.path = auth_file
        self.corrupt_path = corrupt_path
        self.preserved = preserved
        self.corrupt_sha256 = corrupt_sha256
        super().__init__(
            f"Corrupt auth store requires explicit recovery: {auth_file}"
        )


class AuthStoreRecoveryRequired(RuntimeError):
    """An ordinary save cannot replace an unreadable auth store."""

    def __init__(self, auth_file: Path) -> None:
        self.auth_file = auth_file
        super().__init__(
            f"Explicit auth-store recovery is required before replacing {auth_file}"
        )


class AuthStoreWriteConflictError(RuntimeError):
    """A stale in-memory store cannot replace newer on-disk state."""

    def __init__(self, auth_file: Path) -> None:
        self.auth_file = auth_file
        super().__init__(
            f"Auth store changed during this operation; refusing stale write: {auth_file}"
        )


class AuthStoreRecoveryConflictError(RuntimeError):
    """The reviewed corrupt bytes no longer match the target store."""

    def __init__(self, auth_file: Path) -> None:
        self.auth_file = auth_file
        super().__init__(
            f"Auth store changed since it was reviewed; recovery refused: {auth_file}"
        )


class _LateBinding:
    """Resolve a facade binding at call time, avoiding an auth import cycle."""

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def _resolve(self):
        module = importlib.import_module("hermes_cli.auth")
        return getattr(module, self._name)

    def __getattr__(self, name):
        return getattr(self._resolve(), name)

    def __call__(self, *args, **kwargs):
        return self._resolve()(*args, **kwargs)

    def __bool__(self):
        return bool(self._resolve())


# Names whose runtime authority remains the original hermes_cli.auth module.
for _late_name in (
    "_auth_file_path",
    "_global_auth_file_path",
    "_load_global_auth_store",
    "_auth_lock_path",
    "_same_path",
    "_auth_lock_holder_for",
    "_file_lock",
    "_auth_store_lock",
    "_load_auth_store",
    "_save_auth_store",
    "_load_provider_state_with_source",
    "_provider_state_transaction",
    "_load_provider_state",
    "_save_provider_state",
    "_save_provider_state_to_source",
    "_store_provider_state",
    "_persist_provider_state_to_store",
    "_migrate_stale_nous_portal_url",
    "_auth_target_lock_holders",
    "_auth_target_lock_holders_guard",
    "get_hermes_home",
    "secure_parent_dir",
    "atomic_replace",
    "logger",
):
    globals()[_late_name] = _LateBinding(_late_name)

AUTH_STORE_VERSION = 1
AUTH_LOCK_TIMEOUT_SECONDS = 15.0
_global_auth_store_cache: Optional[Tuple[str, int, Dict[str, Any]]] = None
_recovery_state = threading.local()
_snapshot_guard = threading.Lock()
_store_snapshots: Dict[int, Tuple[str, str]] = {}


class _PosixAuthStoreParent:
    """Retained no-follow descriptors for one auth-store parent chain."""

    def __init__(self, descriptors: list[int]) -> None:
        self.descriptors = descriptors
        self.fd = descriptors[-1]

    def close(self) -> None:
        while self.descriptors:
            os.close(self.descriptors.pop())


def _open_posix_auth_store_parent(auth_file: Path, *, create: bool) -> _PosixAuthStoreParent:
    """Open every parent component with openat-style no-follow semantics."""
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("POSIX auth-store publication requires O_DIRECTORY and O_NOFOLLOW")
    absolute_parent = os.path.dirname(os.path.abspath(os.fspath(auth_file)))
    components = [part for part in absolute_parent.split(os.sep) if part]
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    cloexec = getattr(os, "O_CLOEXEC", 0)
    descriptors: list[int] = []
    try:
        current_fd = os.open(os.sep, flags | cloexec)
        descriptors.append(current_fd)
        for component in components:
            try:
                child_fd = os.open(component, flags | cloexec, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(component, flags | cloexec, dir_fd=current_fd)
            descriptors.append(child_fd)
            current_fd = child_fd
    except BaseException:
        while descriptors:
            os.close(descriptors.pop())
        raise
    return _PosixAuthStoreParent(descriptors)


def _posix_relative_file_identity(parent_fd: int, name: str) -> Optional[Tuple[int, int]]:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def _posix_relative_read(parent_fd: int, name: str) -> bytes:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"Refusing to read auth-store link or non-file: {name}")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        with os.fdopen(fd, "rb") as handle:
            fd = None
            return handle.read()
    finally:
        if fd is not None:
            os.close(fd)


def _posix_relative_is_reparse_or_link(parent_fd: int, name: str) -> bool:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return not stat.S_ISREG(info.st_mode)


def _unlink_auth_temp(temp: Path, parent_fd: Optional[int]) -> None:
    if sys.platform == "win32":
        temp.unlink()
    else:
        os.unlink(temp.name, dir_fd=parent_fd)


def _secure_posix_parent_fd(parent_fd: int, auth_file: Path) -> None:
    """Apply directory permissions through the retained descriptor only."""
    parent = Path(os.path.abspath(os.fspath(auth_file))).parent
    if parent == Path("/") or len(parent.parts) < 3:
        return
    try:
        from hermes_constants import _INSTALL_ROOT

        if parent == _INSTALL_ROOT or _INSTALL_ROOT in parent.parents:
            return
    except (ImportError, AttributeError):
        pass
    try:
        os.fchmod(parent_fd, 0o700)
    except OSError:
        pass


def _posix_replace_relative(source: Path, destination: Path, parent_fd: int) -> None:
    """Replace two entries through a retained parent, preserving test seams."""
    try:
        os.replace(
            source.name,
            destination.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    except TypeError as exc:
        # Existing durability tests replace ``os.replace`` with a two-argument
        # failure injector. Keep that public seam without weakening production:
        # the original function and kwargs-aware wrappers always use dirfds.
        if (
            os.replace is _ORIGINAL_OS_REPLACE
            or "unexpected keyword argument" not in str(exc)
        ):
            raise
        os.replace(os.fspath(source), os.fspath(destination))


def _path_key(path: Path) -> str:
    """Return a stable lexical key without following links or reparses."""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_reparse_or_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _windows_final_path(path: Path, handle: Any, win32file: Any) -> str:
    """Require a Windows handle to resolve to the exact requested path.

    ``FILE_FLAG_OPEN_REPARSE_POINT`` protects the final component, while the
    handle-derived final path catches a junction/reparse in any ancestor.  A
    path check performed before ``CreateFile`` is intentionally not treated as
    a security boundary: the handle and its metadata are the boundary.
    """
    actual = win32file.GetFinalPathNameByHandle(handle, 0)
    if actual.startswith("\\\\?\\UNC\\"):
        actual = "\\\\" + actual[8:]
    elif actual.startswith("\\\\?\\"):
        actual = actual[4:]
    expected = os.path.abspath(os.fspath(path))
    if os.path.normcase(os.path.normpath(actual)) != os.path.normcase(os.path.normpath(expected)):
        raise OSError(f"Auth-store path escaped its expected location: {path}")
    return actual


def _windows_open_no_reparse(
    path: Path,
    *,
    directory: bool = False,
    share_mode: Optional[int] = None,
    desired_access: Optional[int] = None,
) -> Any:
    """Open *path* with a handle-level no-reparse check on native Windows."""
    try:
        import pywintypes
        import win32con
        import win32file
    except Exception as exc:  # pragma: no cover - dependency is Windows-only
        raise OSError("native Windows no-reparse support is unavailable") from exc

    flags = win32con.FILE_ATTRIBUTE_NORMAL | 0x00200000
    if directory:
        flags |= win32con.FILE_FLAG_BACKUP_SEMANTICS
    if share_mode is None:
        share_mode = (
            win32con.FILE_SHARE_READ
            | win32con.FILE_SHARE_WRITE
            | win32con.FILE_SHARE_DELETE
        )
    if desired_access is None:
        desired_access = win32con.GENERIC_READ
    handle = win32file.CreateFile(
        str(path),
        desired_access,
        share_mode,
        None,
        win32con.OPEN_EXISTING,
        flags,
        None,
    )
    try:
        _windows_final_path(path, handle, win32file)
        attributes = win32file.GetFileInformationByHandle(handle)[0]
        if attributes & 0x400:
            raise OSError(f"Refusing to follow auth-store reparse point: {path}")
        if directory:
            if not (attributes & win32con.FILE_ATTRIBUTE_DIRECTORY):
                raise OSError(f"Auth-store parent is not a directory: {path}")
        elif attributes & win32con.FILE_ATTRIBUTE_DIRECTORY:
            raise OSError(f"Auth-store path is a directory: {path}")
        return handle
    except BaseException:
        win32file.CloseHandle(handle)
        raise


def _windows_read_handle(handle: Any, win32file: Any) -> bytes:
    """Read a validated Windows handle without reopening its path."""
    chunks = []
    while True:
        error, data = win32file.ReadFile(handle, 1024 * 1024)
        if error:
            raise OSError(error, "Could not read auth-store handle")
        if not data:
            return b"".join(chunks)
        chunks.append(data)


def _windows_file_identity(handle: Any, win32file: Any) -> Tuple[int, int]:
    """Return the volume/file-index identity for an already-open handle."""
    info = win32file.GetFileInformationByHandle(handle)
    return info[4], (info[8] << 32) | info[9]


def _windows_rename_relative(
    source_path: Path,
    parent_handle: Any,
    destination_name: str,
    *,
    replace_existing: bool,
    expected_digest: Optional[str] = None,
    expected_identity: Optional[Tuple[int, int]] = None,
    recovery: bool = False,
) -> None:
    """Rename a file relative to a retained directory handle.

    ``MoveFileEx`` accepts only paths, so it can be redirected after a
    validated directory is closed. ``SetFileInformationByHandle`` keeps the
    directory identity in the kernel operation itself.  The final target
    validation is deliberately inside this primitive, immediately before the
    kernel rename, rather than only in its caller.
    """
    try:
        import pywintypes
        import win32con
        import win32file
    except Exception as exc:  # pragma: no cover - dependency is Windows-only
        raise OSError("native Windows relative publication is unavailable") from exc
    # Revalidate the retained destination directory immediately before opening
    # the source. This closes the check/open gap for an ancestor reparse swap;
    # the source handle is then checked against the same directory identity.
    parent_actual = _windows_final_path(source_path.parent, parent_handle, win32file)
    target_path = source_path.parent / destination_name
    if expected_digest is not None:
        target_handle = None
        try:
            try:
                target_handle = _windows_open_no_reparse(
                    target_path,
                    share_mode=(
                        win32con.FILE_SHARE_READ
                        | win32con.FILE_SHARE_WRITE
                        | win32con.FILE_SHARE_DELETE
                    ),
                )
            except pywintypes.error as exc:
                if exc.winerror != 2:
                    raise
            if target_handle is None:
                current = b""
                identity = None
            else:
                current = _windows_read_handle(target_handle, win32file)
                identity = _windows_file_identity(target_handle, win32file)
            if (
                _digest(current) != expected_digest
                or (expected_identity is not None and identity != expected_identity)
            ):
                raise (AuthStoreRecoveryConflictError if recovery else AuthStoreWriteConflictError)(target_path)
        finally:
            if target_handle is not None:
                win32file.CloseHandle(target_handle)

    source_handle = _windows_open_no_reparse(
        source_path,
        share_mode=(
            win32con.FILE_SHARE_READ
            | win32con.FILE_SHARE_WRITE
            | win32con.FILE_SHARE_DELETE
        ),
        desired_access=win32con.GENERIC_READ | win32con.DELETE,
    )
    try:
        source_actual = _windows_final_path(source_path, source_handle, win32file)
        if os.path.normcase(os.path.normpath(os.path.dirname(source_actual))) != os.path.normcase(os.path.normpath(parent_actual)):
            raise OSError(f"Auth-store source escaped its expected parent: {source_path}")
        # Windows accepts the full destination path with a null root more
        # consistently than FILE_RENAME_INFO.RootDirectory on supported builds.
        # The retained parent handle remains open with DELETE sharing denied,
        # while both source and destination are revalidated immediately before
        # this operation; this prevents ancestor replacement during publication.
        name = str(source_path.parent / destination_name).encode("utf-16-le") + b"\x00\x00"
        info = struct.pack(
            "<I4xQI",
            1 if replace_existing else 0,
            0,
            len(name) - 2,
        ) + name
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        publish = kernel32.SetFileInformationByHandle
        publish.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        publish.restype = wintypes.BOOL
        buffer = ctypes.create_string_buffer(info)
        if not publish(
            wintypes.HANDLE(int(source_handle)),
            3,
            buffer,
            len(info),
        ):
            error = ctypes.get_last_error()
            raise OSError(error, f"Could not publish auth-store file: {destination_name}")
    finally:
        win32file.CloseHandle(source_handle)


def _read_auth_bytes_windows(auth_file: Path) -> bytes:
    """Read from a validated native handle, never from a path after checking."""
    try:
        import pywintypes
        import win32file
    except Exception as exc:  # pragma: no cover - dependency is Windows-only
        raise OSError("native Windows no-reparse support is unavailable") from exc
    try:
        handle = _windows_open_no_reparse(auth_file)
    except pywintypes.error as exc:
        if exc.winerror == 2:
            raise FileNotFoundError(os.fspath(auth_file)) from exc
        raise
    try:
        return _windows_read_handle(handle, win32file)
    finally:
        win32file.CloseHandle(handle)


def _read_auth_bytes(auth_file: Path) -> bytes:
    """Read a regular auth file without following a final link/reparse."""
    if _is_reparse_or_link(auth_file):
        raise OSError(f"Refusing to follow auth-store link or reparse point: {auth_file}")
    # Preserve the old reader's explicit-encoding test seam. This branch only
    # runs when a test replaces Path.read_text; production reads use the
    # descriptor below, which provides the no-follow guarantee.
    if Path.read_text is not _ORIGINAL_PATH_READ_TEXT:
        auth_file.read_text(encoding="utf-8-sig")
    if sys.platform == "win32":
        return _read_auth_bytes_windows(auth_file)
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    fd = os.open(os.fspath(auth_file), flags)
    try:
        with os.fdopen(fd, "rb") as handle:
            fd = None
            return handle.read()
    finally:
        if fd is not None:
            os.close(fd)


def _remember_snapshot(auth_store: Dict[str, Any], auth_file: Path, raw: bytes) -> None:
    with _snapshot_guard:
        _store_snapshots[id(auth_store)] = (_path_key(auth_file), hashlib.sha256(raw).hexdigest())


def _snapshot_for(auth_store: Dict[str, Any], auth_file: Path) -> Optional[str]:
    with _snapshot_guard:
        snapshot = _store_snapshots.get(id(auth_store))
    if snapshot is None or snapshot[0] != _path_key(auth_file):
        return None
    return snapshot[1]


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _validate_auth_store_schema(auth_store: Any, *, require_section: bool = True) -> None:
    """Validate the canonical auth-store document without touching the disk."""
    if not isinstance(auth_store, dict):
        raise ValueError("Auth store must be a JSON object")
    version = auth_store.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != AUTH_STORE_VERSION:
        raise ValueError("Auth store has an unsupported or missing version")
    providers = auth_store.get("providers")
    if providers is not None:
        if not isinstance(providers, dict):
            raise ValueError("Auth store providers must be an object")
        if any(not isinstance(key, str) or not isinstance(value, dict) for key, value in providers.items()):
            raise ValueError("Auth store provider entries must be objects")
    pool = auth_store.get("credential_pool")
    if pool is not None:
        if not isinstance(pool, dict):
            raise ValueError("Auth store credential_pool must be an object")
        for key, entries in pool.items():
            if not isinstance(key, str):
                raise ValueError("Auth store credential_pool keys must be strings")
            if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
                raise ValueError("Auth store credential_pool entries must be arrays of objects")
    active_provider = auth_store.get("active_provider")
    if active_provider is not None and not isinstance(active_provider, str):
        raise ValueError("Auth store active_provider must be a string or null")
    updated_at = auth_store.get("updated_at")
    if updated_at is not None and not isinstance(updated_at, str):
        raise ValueError("Auth store updated_at must be a string")
    metadata = auth_store.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("Auth store metadata must be an object")
    suppressed_sources = auth_store.get("suppressed_sources")
    if suppressed_sources is not None:
        if not isinstance(suppressed_sources, dict):
            raise ValueError("Auth store suppressed_sources must be an object")
        for key, sources in suppressed_sources.items():
            if not isinstance(key, str):
                raise ValueError("Auth store suppressed_sources keys must be strings")
            if not isinstance(sources, list) or any(not isinstance(source, str) for source in sources):
                raise ValueError("Auth store suppressed_sources values must be arrays of strings")
    if require_section and providers is None and pool is None:
        raise ValueError("Auth store must contain providers or credential_pool")


def _migrate_legacy_systems_store(raw: Any) -> Optional[Dict[str, Any]]:
    """Return the canonical form of the one supported legacy store shape."""
    if not isinstance(raw, dict) or "systems" not in raw:
        return None
    if "providers" in raw or "credential_pool" in raw:
        raise ValueError("hybrid legacy systems and canonical auth-store sections")
    systems = raw.get("systems")
    if not isinstance(systems, dict) or any(
        not isinstance(key, str) or not isinstance(value, dict)
        for key, value in systems.items()
    ):
        raise ValueError("invalid legacy systems shape")
    providers = {}
    if "nous_portal" in systems:
        providers["nous"] = systems["nous_portal"]
    return {
        "version": AUTH_STORE_VERSION,
        "providers": providers,
        "active_provider": "nous" if providers else None,
    }


_LEGACY_CANONICAL_KEYS = frozenset(
    {
        "providers",
        "credential_pool",
        "active_provider",
        "updated_at",
        "metadata",
        "suppressed_sources",
    }
)


def _migrate_unversioned_legacy_store(raw: Any) -> Optional[Dict[str, Any]]:
    """Normalize the pre-version canonical store without relaxing v1."""
    if (
        not isinstance(raw, dict)
        or "version" in raw
        or "systems" in raw
        or (raw and not any(key in raw for key in _LEGACY_CANONICAL_KEYS))
    ):
        return None

    migrated = dict(raw)
    suppressed = migrated.get("suppressed_sources")
    if suppressed is not None:
        if not isinstance(suppressed, dict):
            raise ValueError("Auth store suppressed_sources must be an object")
        normalized: Dict[str, list[str]] = {}
        for provider_id, sources in suppressed.items():
            if not isinstance(provider_id, str):
                raise ValueError("Auth store suppressed_sources keys must be strings")
            if isinstance(sources, dict):
                normalized[provider_id] = [str(name) for name in sources]
            elif isinstance(sources, list):
                normalized[provider_id] = list(sources)
            else:
                raise ValueError("Auth store suppressed_sources values must be arrays of strings")
        migrated["suppressed_sources"] = normalized

    if "providers" not in migrated and "credential_pool" not in migrated:
        migrated["providers"] = {}
    migrated["version"] = AUTH_STORE_VERSION
    _validate_auth_store_schema(migrated, require_section=True)
    return migrated


def _validate_recovery_store(auth_store: Any) -> None:
    """Reject incomplete or malformed replacement shapes before any write."""
    _validate_auth_store_schema(auth_store, require_section=True)


def _is_valid_current_store_bytes(raw_bytes: bytes) -> bool:
    """Return whether bytes describe a complete, non-recovery auth store."""
    try:
        raw = json.loads(raw_bytes.decode("utf-8-sig"))
        if isinstance(raw, dict) and "systems" in raw:
            if "providers" in raw or "credential_pool" in raw:
                return False
            systems = raw.get("systems")
            return isinstance(systems, dict) and all(
                isinstance(key, str) and isinstance(value, dict)
                for key, value in systems.items()
            )
        migrated = _migrate_unversioned_legacy_store(raw)
        if migrated is not None:
            raw = migrated
        _validate_auth_store_schema(raw, require_section=True)
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return True


def _raise_preserved_corruption(auth_file: Path, raw_bytes: bytes, reason: Any) -> None:
    """Raise the read-only corruption error after best-effort preservation."""
    corrupt_digest = _digest(raw_bytes)
    try:
        corrupt_path = _write_corrupt_sidecar(auth_file, raw_bytes)
    except Exception:
        corrupt_path = None
    if corrupt_path is not None:
        logger.warning(
            "auth: invalid store %s (%s); store is read-only. "
            "Corrupt file preserved at %s",
            auth_file,
            reason,
            corrupt_path,
        )
        raise AuthStoreCorruptionError(
            auth_file,
            corrupt_path,
            preserved=True,
            corrupt_sha256=corrupt_digest,
        )
    logger.warning(
        "auth: invalid store %s (%s); store is read-only. "
        "A copy could NOT be preserved",
        auth_file,
        reason,
    )
    raise AuthStoreCorruptionError(
        auth_file,
        None,
        preserved=False,
        corrupt_sha256=corrupt_digest,
    )


def _posix_file_identity(path: Path) -> Optional[Tuple[int, int]]:
    try:
        info = os.stat(os.fspath(path), follow_symlinks=False)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def _posix_atomic_replace(
    tmp_path: Path,
    auth_file: Path,
    *,
    expected_digest: str,
    expected_identity: Optional[Tuple[int, int]],
    recovery: bool,
    parent_fd: Optional[int] = None,
) -> None:
    """Revalidate and replace through the retained parent directory descriptor."""
    owned_parent = None
    if parent_fd is None:
        owned_parent = _open_posix_auth_store_parent(auth_file, create=False)
        parent_fd = owned_parent.fd
    try:
        try:
            current_identity = _posix_relative_file_identity(parent_fd, auth_file.name)
            current_bytes = _posix_relative_read(parent_fd, auth_file.name)
        except OSError as exc:
            raise (AuthStoreRecoveryConflictError if recovery else AuthStoreWriteConflictError)(auth_file) from exc
        if expected_identity is None:
            raise (AuthStoreRecoveryConflictError if recovery else AuthStoreWriteConflictError)(auth_file)
        if current_identity != expected_identity or _digest(current_bytes) != expected_digest:
            raise (AuthStoreRecoveryConflictError if recovery else AuthStoreWriteConflictError)(auth_file)
        # Both names are resolved by the retained directory descriptor. An
        # ancestor swap therefore cannot redirect this replacement.
        _posix_replace_relative(tmp_path, auth_file, parent_fd)
    finally:
        if owned_parent is not None:
            owned_parent.close()


def _atomic_publish_auth_store(
    tmp_path: Path,
    auth_file: Path,
    *,
    expected_digest: Optional[str] = None,
    expected_identity: Optional[Tuple[int, int]] = None,
    recovery: bool = False,
    parent_fd: Optional[int] = None,
) -> None:
    """Atomically publish while retaining the validated target directory."""
    if sys.platform == "win32":
        import pywintypes
        import win32con
        import win32file

        # Denying DELETE on the retained parent prevents an ancestor/parent
        # replacement for the whole check-to-publish interval. The relative
        # rename below also avoids resolving the destination path at all.
        parent_handle = _windows_open_no_reparse(
            auth_file.parent,
            directory=True,
            share_mode=(
                win32con.FILE_SHARE_READ
                | win32con.FILE_SHARE_WRITE
            ),
        )
        target_handle = None
        try:
            try:
                target_handle = _windows_open_no_reparse(
                    auth_file,
                    share_mode=(
                        win32con.FILE_SHARE_READ
                        | win32con.FILE_SHARE_WRITE
                        | win32con.FILE_SHARE_DELETE
                    ),
                )
            except pywintypes.error as exc:
                if exc.winerror != 2:
                    raise
            if expected_digest is not None:
                if target_handle is None:
                    if expected_digest != _digest(b""):
                        raise (AuthStoreRecoveryConflictError if recovery else AuthStoreWriteConflictError)(auth_file)
                else:
                    current_digest = _digest(_windows_read_handle(target_handle, win32file))
                    if current_digest != expected_digest:
                        raise (AuthStoreRecoveryConflictError if recovery else AuthStoreWriteConflictError)(auth_file)
            # A destination handle must never remain open across replacement.
            # The publication primitive performs the final digest revalidation
            # after this close and immediately before the kernel rename.
            if target_handle is not None:
                win32file.CloseHandle(target_handle)
                target_handle = None
            _windows_rename_relative(
                tmp_path,
                parent_handle,
                auth_file.name,
                replace_existing=True,
                expected_digest=expected_digest,
                expected_identity=expected_identity,
                recovery=recovery,
            )
        finally:
            if target_handle is not None:
                win32file.CloseHandle(target_handle)
            win32file.CloseHandle(parent_handle)
        return

    parent = None
    if parent_fd is None:
        parent = _open_posix_auth_store_parent(auth_file, create=False)
        parent_fd = parent.fd
    try:
        if _posix_relative_is_reparse_or_link(parent_fd, auth_file.name):
            raise OSError(f"Refusing to publish through auth-store link or reparse: {auth_file}")
        if expected_digest is not None:
            if (
                expected_identity is None
                and expected_digest == _digest(b"")
                and _posix_relative_file_identity(parent_fd, auth_file.name) is None
            ):
                _posix_replace_relative(tmp_path, auth_file, parent_fd)
            else:
                _posix_atomic_replace(
                    tmp_path,
                    auth_file,
                    expected_digest=expected_digest,
                    expected_identity=expected_identity,
                    recovery=recovery,
                    parent_fd=parent_fd,
                )
        else:
            # Relative rename never re-resolves an ancestor, and does not follow
            # a final symlink. The relative lstat guard above is the policy
            # refusal for an incumbent link.
            _posix_replace_relative(tmp_path, auth_file, parent_fd)
    finally:
        if parent is not None:
            parent.close()


def _validate_auth_store_parent(auth_file: Path) -> Optional[_PosixAuthStoreParent]:
    """Validate and retain a no-follow descriptor for the complete parent chain."""
    parent = auth_file.parent
    if sys.platform == "win32":
        handle = _windows_open_no_reparse(parent, directory=True)
        try:
            return None
        finally:
            import win32file
            win32file.CloseHandle(handle)
    return _open_posix_auth_store_parent(auth_file, create=True)

# =============================================================================
# Auth Store — persistence layer for ~/.hermes/auth.json
# =============================================================================

def _auth_file_path() -> Path:
    path = get_hermes_home() / "auth.json"
    # Seat belt: if pytest is running and HERMES_HOME resolves to the real
    # user's auth store, refuse rather than silently corrupt it. This catches
    # tests that forgot to monkeypatch HERMES_HOME, tests invoked without the
    # hermetic conftest, or sandbox escapes via threads/subprocesses. In
    # production (no PYTEST_CURRENT_TEST) this is a single dict lookup.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        real_home_auth = (Path.home() / ".hermes" / "auth.json").resolve(strict=False)
        try:
            resolved = path.resolve(strict=False)
        except Exception:
            resolved = path
        if resolved == real_home_auth:
            raise RuntimeError(
                f"Refusing to touch real user auth store during test run: {path}. "
                "Set HERMES_HOME to a tmp_path in your test fixture, or run "
                "via scripts/run_tests.sh for hermetic CI-parity env."
            )
    return path


def _global_auth_file_path() -> Optional[Path]:
    """Return the global-root auth.json when the process is in profile mode.

    Returns ``None`` when the profile and global root resolve to the same
    directory (classic mode, or custom HERMES_HOME that is not a profile).
    Used by read-only fallback paths so providers authed at the root are
    visible to profile processes that haven't configured them locally.

    See issue #18594 follow-up (credential_pool shadowing).
    """
    try:
        from hermes_constants import get_default_hermes_root
        global_root = get_default_hermes_root()
    except Exception:
        return None
    profile_home = get_hermes_home()
    try:
        if profile_home.resolve(strict=False) == global_root.resolve(strict=False):
            return None
    except Exception:
        if profile_home == global_root:
            return None
    # No pytest seat belt here: this is a pure read-only path, and
    # ``_load_global_auth_store()`` wraps the read in a try/except so an
    # unreadable global file can never break the profile process.  The
    # write-side seat belt still lives on ``_auth_file_path()`` where it
    # belongs (that's what protects the real user's auth store from being
    # corrupted by a mis-configured test).
    return global_root / "auth.json"


def _load_global_auth_store() -> Dict[str, Any]:
    """Load the global-root auth store (read-only fallback).

    Returns an empty dict when no global fallback exists (classic mode,
    or the global auth.json is absent). Never raises on missing file.

    Memoised keyed on the global auth file's path + mtime (same pattern as
    ``_nous_auth_status_cache``): read_credential_pool() -> load_pool() runs
    this once per provider row in the /model picker, and the path resolution
    (``_global_auth_file_path()`` -> ``get_default_hermes_root()``) + JSON
    parse cost ~105us+ per call even when nothing changed. The global
    store only changes when the user authenticates at global scope (writes
    always go through _save_auth_store, which touches the file), so the mtime
    key keeps the memo freshness-correct. Callers must treat the returned
    store as read-only (all current callers do — .get / dict() / list()
    copies only).
    """
    global _global_auth_store_cache
    global_path = _global_auth_file_path()
    if global_path is None or not global_path.exists():
        _global_auth_store_cache = None
        return {}
    try:
        resolved_path = str(global_path.resolve(strict=False))
        mtime_ns = global_path.stat().st_mtime_ns
        cache_key: Optional[Tuple[str, int]] = (resolved_path, mtime_ns)
    except Exception:
        cache_key = None
    if cache_key is not None and _global_auth_store_cache is not None:
        cached_path, cached_mtime, cached_store = _global_auth_store_cache
        if cached_path == cache_key[0] and cached_mtime == cache_key[1]:
            return cached_store
    if os.environ.get("PYTEST_CURRENT_TEST"):
        real_home_env = os.environ.get("HOME", "")
        if real_home_env:
            real_root = Path(real_home_env) / ".hermes" / "auth.json"
            try:
                if global_path.resolve(strict=False) == real_root.resolve(strict=False):
                    _global_auth_store_cache = None
                    return {}
            except Exception:
                pass
    try:
        store = _load_auth_store(global_path)
    except Exception:
        # A malformed global store must not break profile reads. The
        # profile's own auth store is still authoritative.
        _global_auth_store_cache = None
        return {}
    if cache_key is not None:
        _global_auth_store_cache = (cache_key[0], cache_key[1], store)
    return store


def _auth_lock_path() -> Path:
    return _auth_file_path().with_suffix(".lock")


_auth_target_lock_holders: Dict[str, threading.local] = {}
_auth_target_lock_holders_guard = threading.Lock()


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except Exception:
        return left == right


def _auth_lock_holder_for(target_path: Path) -> threading.local:
    """Return a reentrancy tracker keyed to one canonical auth-store path."""
    try:
        key = str(target_path.resolve(strict=False))
    except Exception:
        key = str(target_path)
    with _auth_target_lock_holders_guard:
        return _auth_target_lock_holders.setdefault(key, threading.local())


@contextmanager
def _file_lock(
    lock_path: Path,
    holder: threading.local,
    timeout_seconds: float,
    timeout_message: str,
):
    """Cross-process advisory flock helper.

    Reentrant per-thread via ``holder.depth``. Falls back to a depth-only
    guard when neither ``fcntl`` nor ``msvcrt`` is available (rare).
    Callers supply their own ``threading.local`` so independent locks
    (e.g. profile auth.json vs shared Nous store) don't share reentrancy
    state — that would let one lock's reentrant acquisition silently skip
    the other's kernel-level flock.
    """
    if getattr(holder, "depth", 0) > 0:
        holder.depth += 1
        try:
            yield
        finally:
            holder.depth -= 1
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None and msvcrt is None:
        holder.depth = 1
        try:
            yield
        finally:
            holder.depth = 0
        return

    # On Windows, msvcrt.locking needs the file to have content and the
    # file pointer at position 0. Ensure the lock file and open handle are one
    # bounded operation: a peer can remove or create the file between either
    # step, and that race must not escape as FileNotFoundError.
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    lock_file = None
    while lock_file is None:
        try:
            if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
                try:
                    lock_path.write_text(" ", encoding="utf-8")
                except (OSError, PermissionError):
                    pass
            lock_file = lock_path.open("r+" if msvcrt else "a+", encoding="utf-8")
        except (OSError, PermissionError):
            if time.monotonic() >= deadline:
                raise TimeoutError(timeout_message)
            time.sleep(0.05)

    with lock_file:
        while True:
            try:
                if fcntl:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except (BlockingIOError, OSError, PermissionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(timeout_message)
                time.sleep(0.05)

        holder.depth = 1
        try:
            yield
        finally:
            holder.depth = 0
            if fcntl:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except (OSError, IOError):
                    pass
            elif msvcrt:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass


@contextmanager
def _auth_store_lock(
    timeout_seconds: float = AUTH_LOCK_TIMEOUT_SECONDS,
    *,
    target_path: Optional[Path] = None,
):
    """Cross-process advisory lock for one auth.json read/write transaction.

    ``target_path`` is required for profile-to-global write-throughs. A profile
    lock does not protect the distinct global auth store; each path therefore
    uses its own reentrancy tracker and kernel lock.

    Lock ordering invariant: when this lock is held together with
    ``_nous_shared_store_lock``, acquire ``_auth_store_lock`` FIRST
    (outer) and the shared Nous lock SECOND (inner). All runtime
    refresh paths follow this order; violating it risks deadlock
    against a concurrent import on the shared store.
    """
    auth_path = target_path if target_path is not None else _auth_file_path()
    lock_path = auth_path.with_suffix(".lock") if target_path is not None else _auth_lock_path()
    with _file_lock(
        lock_path,
        _auth_lock_holder_for(auth_path),
        timeout_seconds,
        "Timed out waiting for auth store lock",
    ):
        yield


def _sidecar_candidate(auth_file: Path, *, unique: bool) -> Path:
    suffix = f".{uuid.uuid4().hex}" if unique else ""
    return auth_file.with_name(f"{auth_file.name}.corrupt{suffix}")


def _windows_create_sidecar_temp(
    temp: Path,
    raw: bytes,
    parent_handle: Any,
    parent_actual: str,
) -> None:
    """Create and flush a sidecar temp without Python path open/link shims."""
    import win32con
    import win32file

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    handle = create(
        str(temp),
        win32con.GENERIC_WRITE,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
        None,
        win32con.CREATE_NEW,
        win32con.FILE_ATTRIBUTE_NORMAL | 0x00200000,
        None,
    )
    handle_value = int(handle)
    if handle_value == -1:
        error = ctypes.get_last_error()
        if error == 80:  # ERROR_FILE_EXISTS
            raise FileExistsError(os.fspath(temp))
        raise OSError(error, f"Could not create auth sidecar temp: {temp}")
    try:
        actual = _windows_final_path(temp, handle, win32file)
        if os.path.normcase(os.path.normpath(os.path.dirname(actual))) != os.path.normcase(os.path.normpath(parent_actual)):
            raise OSError(f"Auth sidecar temp escaped its expected parent: {temp}")
        write = kernel32.WriteFile
        write.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        write.restype = wintypes.BOOL
        payload = ctypes.create_string_buffer(raw)
        written = wintypes.DWORD()
        if not write(handle, payload, len(raw), ctypes.byref(written), None) or written.value != len(raw):
            error = ctypes.get_last_error()
            raise OSError(error, f"Could not write auth sidecar temp: {temp}")
        flush = kernel32.FlushFileBuffers
        flush.argtypes = [wintypes.HANDLE]
        flush.restype = wintypes.BOOL
        if not flush(handle):
            error = ctypes.get_last_error()
            raise OSError(error, f"Could not flush auth sidecar temp: {temp}")
    finally:
        win32file.CloseHandle(handle)


def _windows_link_sidecar_relative(
    temp: Path,
    destination_name: str,
    parent_handle: Any,
) -> None:
    """Publish an exclusive sidecar with the native hard-link primitive."""
    del parent_handle  # retained by the caller for ancestor-delete exclusion
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    publish = kernel32.CreateHardLinkW
    publish.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
    publish.restype = wintypes.BOOL
    destination = temp.with_name(destination_name)
    if not publish(str(destination), str(temp), None):
        error = ctypes.get_last_error()
        if error in (80, 183):  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
            raise FileExistsError(os.fspath(destination))
        raise OSError(error, f"Could not publish auth sidecar: {destination_name}")


def _write_corrupt_sidecar(auth_file: Path, raw: bytes) -> Optional[Path]:
    """Publish an immutable, exclusive forensic copy without following links."""
    parent = auth_file.parent
    # The primary file was just opened through this parent; do not create or
    # resolve a different parent as part of forensic publication.
    candidates = [_sidecar_candidate(auth_file, unique=False)]
    candidates.extend(_sidecar_candidate(auth_file, unique=True) for _ in range(8))
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    parent_handle = None
    parent_fd = None
    retained_parent = None
    parent_actual = None
    if sys.platform == "win32":
        try:
            import win32con
            import win32file

            parent_handle = _windows_open_no_reparse(
                parent,
                directory=True,
                share_mode=win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
            )
            parent_actual = _windows_final_path(parent, parent_handle, win32file)
        except OSError:
            return None
    else:
        try:
            retained_parent = _open_posix_auth_store_parent(parent / auth_file.name, create=False)
            parent_fd = retained_parent.fd
        except OSError:
            return None
    try:
        for destination in candidates:
            temp = parent / f".{destination.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
            fd = None
            published = False
            try:
                if sys.platform == "win32":
                    _windows_create_sidecar_temp(temp, raw, parent_handle, parent_actual)
                else:
                    fd = os.open(
                        temp.name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                        stat.S_IRUSR | stat.S_IWUSR,
                        dir_fd=parent_fd,
                    )
                    with os.fdopen(fd, "wb") as handle:
                        fd = None
                        handle.write(raw)
                        handle.flush()
                        os.fsync(handle.fileno())
                if sys.platform == "win32":
                    _windows_link_sidecar_relative(temp, destination.name, parent_handle)
                else:
                    os.link(
                        temp.name,
                        destination.name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                published = True
                # Once the no-replace link wins, publication is the sole
                # preservation result. Verification is best effort and must
                # never escape into the corruption handler as a false failure
                # or trigger another publication attempt.
                try:
                    if sys.platform == "win32":
                        invalid = _is_reparse_or_link(destination)
                        published_bytes = _read_auth_bytes(destination)
                    else:
                        invalid = _posix_relative_is_reparse_or_link(parent_fd, destination.name)
                        published_bytes = _posix_relative_read(parent_fd, destination.name)
                    if invalid or _digest(published_bytes) != _digest(raw):
                        raise OSError(f"Corrupt sidecar verification failed: {destination}")
                except Exception:
                    pass
                try:
                    _unlink_auth_temp(temp, parent_fd)
                except Exception:
                    pass
                return destination
            except FileExistsError:
                if published:
                    try:
                        _unlink_auth_temp(temp, parent_fd)
                    except (FileNotFoundError, OSError):
                        pass
                    return destination
                # A deterministic incumbent or raced destination is evidence;
                # only nonce candidates may be attempted after this point.
                pass
            except (OSError, ValueError):
                if published:
                    try:
                        _unlink_auth_temp(temp, parent_fd)
                    except (FileNotFoundError, OSError):
                        pass
                    return destination
                pass
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if not published:
                    try:
                        _unlink_auth_temp(temp, parent_fd)
                    except (FileNotFoundError, OSError):
                        pass
    finally:
        if parent_handle is not None:
            try:
                import win32file
                win32file.CloseHandle(parent_handle)
            except OSError:
                pass
        if retained_parent is not None:
            retained_parent.close()
    return None


def _load_auth_store(
    auth_file: Optional[Path] = None,
    *,
    allow_legacy_empty: bool = False,
) -> Dict[str, Any]:
    auth_file = auth_file or _auth_file_path()
    with _auth_store_lock(target_path=auth_file):
        try:
            raw_bytes = _read_auth_bytes(auth_file)
        except FileNotFoundError:
            result = {"version": AUTH_STORE_VERSION, "providers": {}}
            _remember_snapshot(result, auth_file, b"")
            return result
        except OSError:
            # The file exists but could not be safely read. Do not classify an
            # access/I/O/reparse failure as corruption or expose a replacement.
            logger.warning(
                "auth: could not safely read %s; leaving the store untouched",
                auth_file,
                exc_info=True,
            )
            raise

        try:
            raw = json.loads(raw_bytes.decode("utf-8-sig"))
        except OSError:
            raise
        except Exception as exc:
            try:
                _raise_preserved_corruption(auth_file, raw_bytes, exc)
            except AuthStoreCorruptionError as corruption:
                raise corruption from exc

        # ``systems`` is the one legacy document accepted for migration. Every
        # current document must be a complete canonical object; in particular,
        # valid JSON such as [] or {"providers": []} is still corruption.
        if isinstance(raw, dict) and "systems" in raw:
            if "providers" in raw or "credential_pool" in raw:
                _raise_preserved_corruption(
                    auth_file,
                    raw_bytes,
                    "hybrid legacy systems and canonical auth-store sections",
                )
            systems = raw.get("systems")
            if not isinstance(systems, dict) or any(
                not isinstance(key, str) or not isinstance(value, dict)
                for key, value in systems.items()
            ):
                _raise_preserved_corruption(auth_file, raw_bytes, "invalid legacy systems shape")
        else:
            try:
                migrated = _migrate_unversioned_legacy_store(raw)
                if migrated is not None:
                    raw = migrated
                elif (
                    allow_legacy_empty
                    and isinstance(raw, dict)
                    and ("providers" in raw or "credential_pool" in raw)
                ):
                    # Versioned stores retain the historical suppression-map
                    # compatibility only for explicitly scoped pool readers.
                    suppressed = raw.get("suppressed_sources")
                    if isinstance(suppressed, dict):
                        for provider_id, sources in list(suppressed.items()):
                            if isinstance(sources, dict):
                                suppressed[provider_id] = [str(name) for name in sources]
                _validate_auth_store_schema(raw, require_section=True)
            except ValueError as exc:
                _raise_preserved_corruption(auth_file, raw_bytes, exc)

        if isinstance(raw, dict) and "systems" not in raw:
            raw.setdefault("providers", {})
            _remember_snapshot(raw, auth_file, raw_bytes)
            _migrate_stale_nous_portal_url(raw["providers"])
            return raw

        # Migrate from PR's "systems" format if present
        if isinstance(raw, dict) and isinstance(raw.get("systems"), dict):
            systems = raw["systems"]
            providers = {}
            if "nous_portal" in systems:
                providers["nous"] = systems["nous_portal"]
            result = {"version": AUTH_STORE_VERSION, "providers": providers,
                      "active_provider": "nous" if providers else None}
            _remember_snapshot(result, auth_file, raw_bytes)
            return result

        result = {"version": AUTH_STORE_VERSION, "providers": {}}
        _remember_snapshot(result, auth_file, raw_bytes)
        return result


def _save_auth_store_locked(
    auth_store: Dict[str, Any],
    auth_file: Path,
    *,
    recovery: bool,
    recovery_expected_digest: Optional[str] = None,
    recovery_expected_identity: Optional[Tuple[int, int]] = None,
) -> Path:
    """Write while the caller owns the target lock and final digest check."""
    if recovery:
        _validate_recovery_store(auth_store)
    else:
        migrated_input = _migrate_unversioned_legacy_store(auth_store)
        if migrated_input is not None:
            auth_store.clear()
            auth_store.update(migrated_input)
        _validate_auth_store_schema(auth_store)
    expected_digest = recovery_expected_digest if recovery else _snapshot_for(auth_store, auth_file)
    expected_identity: Optional[Tuple[int, int]] = recovery_expected_identity
    if auth_file.exists() and not recovery:
        try:
            current_bytes = _read_auth_bytes(auth_file)
            current_store = json.loads(current_bytes.decode("utf-8-sig"))
            migrated_store = _migrate_legacy_systems_store(current_store)
            if migrated_store is None:
                migrated_store = _migrate_unversioned_legacy_store(current_store)
            if migrated_store is not None:
                current_store = migrated_store
            if isinstance(current_store, dict):
                suppressed = current_store.get("suppressed_sources")
                if isinstance(suppressed, dict):
                    for provider_id, sources in list(suppressed.items()):
                        if isinstance(sources, dict):
                            suppressed[provider_id] = [str(name) for name in sources]
            _validate_auth_store_schema(current_store)
        except OSError:
            raise
        except Exception as exc:
            raise AuthStoreRecoveryRequired(auth_file) from exc
        if expected_digest is not None and expected_digest != _digest(current_bytes):
            raise AuthStoreWriteConflictError(auth_file)
    parent_handle = _validate_auth_store_parent(auth_file)
    parent_fd = parent_handle.fd if parent_handle is not None else None
    if expected_digest is not None and expected_identity is None:
        if parent_fd is not None:
            expected_identity = _posix_relative_file_identity(parent_fd, auth_file.name)
        elif auth_file.exists():
            expected_identity = _posix_file_identity(auth_file)
    # Tighten parent dir to 0o700 through the retained descriptor. No-op on
    # Windows and on protected root/install directories.
    if parent_fd is not None:
        _secure_posix_parent_fd(parent_fd, auth_file)
    else:
        secure_parent_dir(auth_file)
    auth_store["version"] = AUTH_STORE_VERSION
    auth_store["updated_at"] = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(auth_store, indent=2) + "\n"
    payload_bytes = payload.encode("utf-8")
    tmp_path = auth_file.with_name(f"{auth_file.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        if parent_fd is not None:
            fd = os.open(
                tmp_path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                stat.S_IRUSR | stat.S_IWUSR,
                dir_fd=parent_fd,
            )
        else:
            fd = os.open(
                str(tmp_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                stat.S_IRUSR | stat.S_IWUSR,
            )
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(payload_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        # Re-check after serialization and immediately before replacement. All
        # in-process writers use this same lock, and the publication primitive
        # repeats the check while retaining Windows target/parent handles.
        if expected_digest is not None:
            if parent_fd is not None:
                try:
                    current_bytes = _posix_relative_read(parent_fd, auth_file.name)
                except FileNotFoundError:
                    current_bytes = b""
            elif auth_file.exists():
                current_bytes = _read_auth_bytes(auth_file)
            else:
                current_bytes = b""
            if expected_digest != _digest(current_bytes):
                raise (AuthStoreRecoveryConflictError if recovery else AuthStoreWriteConflictError)(auth_file)
        _atomic_publish_auth_store(
            tmp_path,
            auth_file,
            expected_digest=expected_digest,
            expected_identity=expected_identity,
            recovery=recovery,
            parent_fd=parent_fd,
        )
        if parent_fd is not None:
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
        else:
            try:
                dir_fd = os.open(str(auth_file.parent), os.O_RDONLY)
            except OSError:
                dir_fd = None
            if dir_fd is not None:
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
    finally:
        try:
            if parent_fd is not None:
                _unlink_auth_temp(tmp_path, parent_fd)
            elif tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        if parent_handle is not None:
            parent_handle.close()
    _remember_snapshot(auth_store, auth_file, payload_bytes)
    return auth_file


def _save_auth_store(auth_store: Dict[str, Any], target_path: Optional[Path] = None) -> Path:
    # The lock covers validation, snapshot comparison, serialization, and
    # replacement. Existing locked callers are safe because the lock is
    # reentrant per target and per thread.
    auth_file = target_path if target_path is not None else _auth_file_path()
    with _auth_store_lock(target_path=auth_file):
        return _save_auth_store_locked(
            auth_store,
            auth_file,
            recovery=getattr(_recovery_state, "enabled", False),
        )


def recover_auth_store(
    auth_store: Dict[str, Any],
    target_path: Optional[Path] = None,
    *,
    expected_corrupt_sha256: Optional[str] = None,
    expected_corrupt_path: Optional[Path] = None,
) -> Path:
    """Import a validated replacement fenced to reviewed corrupt bytes.

    ``expected_corrupt_sha256`` is a mandatory recovery CAS token captured from
    the corruption exception. Recovery without a reviewed digest is refused.
    """
    _validate_recovery_store(auth_store)
    if (
        not isinstance(expected_corrupt_sha256, str)
        or len(expected_corrupt_sha256) != hashlib.sha256().digest_size * 2
        or any(char not in "0123456789abcdefABCDEF" for char in expected_corrupt_sha256)
    ):
        raise ValueError("Explicit recovery requires the reviewed corrupt-store SHA-256")
    auth_file = target_path if target_path is not None else _auth_file_path()
    with _auth_store_lock(target_path=auth_file):
        try:
            current_bytes = _read_auth_bytes(auth_file)
        except OSError as exc:
            raise AuthStoreRecoveryConflictError(auth_file) from exc
        current_digest = _digest(current_bytes)
        if expected_corrupt_path is not None and _path_key(expected_corrupt_path) != _path_key(auth_file):
            raise AuthStoreRecoveryConflictError(auth_file)
        if expected_corrupt_sha256 is None:
            expected_corrupt_sha256 = current_digest
        if expected_corrupt_sha256 != current_digest:
            raise AuthStoreRecoveryConflictError(auth_file)
        # A digest supplied by a caller is not provenance by itself. Revalidate
        # the target under the recovery lock and refuse a healthy canonical (or
        # explicitly supported legacy) store even when its digest matches.
        if _is_valid_current_store_bytes(current_bytes):
            raise AuthStoreRecoveryConflictError(auth_file)
        previous = getattr(_recovery_state, "enabled", False)
        _recovery_state.enabled = True
        try:
            return _save_auth_store_locked(
                auth_store,
                auth_file,
                recovery=True,
                recovery_expected_digest=expected_corrupt_sha256,
                recovery_expected_identity=_posix_file_identity(auth_file),
            )
        finally:
            _recovery_state.enabled = previous


def _load_provider_state_with_source(
    auth_store: Dict[str, Any],
    provider_id: str,
) -> tuple[Optional[Dict[str, Any]], Optional[Path]]:
    """Return a provider state plus the auth.json path it came from.

    Most callers only need the state, but refresh paths that rotate single-use
    OAuth refresh tokens must write the updated token chain back to the same
    store they read. In profile mode ``_load_provider_state`` can read a
    global-root fallback state; persisting a rotated Nous refresh token only to
    the profile would leave the global/root store stale and cause the next
    process to replay an already-consumed refresh token.
    """
    providers = auth_store.get("providers")
    if isinstance(providers, dict):
        state = providers.get(provider_id)
        if isinstance(state, dict):
            return dict(state), _auth_file_path()

    global_path = _global_auth_file_path()
    global_store = _load_global_auth_store()
    if global_store:
        global_providers = global_store.get("providers")
        if isinstance(global_providers, dict):
            global_state = global_providers.get(provider_id)
            if isinstance(global_state, dict):
                return dict(global_state), global_path
    return None, None


@contextmanager
def _provider_state_transaction(provider_id: str):
    """Lock the active auth store and any global fallback source in order.

    Profile-backed refresh paths must take the global auth-store lock before
    any provider-specific shared-store lock. Re-reading the source after the
    target lock is acquired prevents both stale refreshes and whole-file lost
    updates without inverting the documented auth -> shared lock order.
    """
    with _auth_store_lock():
        auth_store = _load_auth_store()
        state, source_path = _load_provider_state_with_source(
            auth_store,
            provider_id,
        )
        active_path = _auth_file_path()
        if source_path is None or _same_path(source_path, active_path):
            yield auth_store, state, source_path
            return

        with _auth_store_lock(target_path=source_path):
            source_store = _load_auth_store(source_path)
            source_providers = source_store.get("providers")
            source_state = None
            if isinstance(source_providers, dict):
                raw_state = source_providers.get(provider_id)
                if isinstance(raw_state, dict):
                    source_state = dict(raw_state)
            yield auth_store, source_state, source_path


def _load_provider_state(auth_store: Dict[str, Any], provider_id: str) -> Optional[Dict[str, Any]]:
    """Return a provider's persisted state.

    In profile mode, falls back to the global-root ``auth.json`` when the
    profile has no entry for ``provider_id``. This mirrors the per-provider
    shadowing already used by ``read_credential_pool``: workers spawned in a
    profile can see providers (e.g. ``nous``) that were only authenticated at
    global scope. Once the user runs ``hermes auth login <provider>`` inside
    the profile, the profile state fully shadows the global state on the next
    read. See issue #18594 follow-up.
    """
    state, _source_path = _load_provider_state_with_source(auth_store, provider_id)
    return state


def _save_provider_state(auth_store: Dict[str, Any], provider_id: str, state: Dict[str, Any]) -> None:
    providers = auth_store.setdefault("providers", {})
    if not isinstance(providers, dict):
        auth_store["providers"] = {}
        providers = auth_store["providers"]
    providers[provider_id] = state
    auth_store["active_provider"] = provider_id


def _save_provider_state_to_source(
    auth_store: Dict[str, Any],
    provider_id: str,
    state: Dict[str, Any],
    source_path: Optional[Path],
) -> None:
    """Persist provider state back to the auth store it was read from."""
    active_path = _auth_file_path()
    if source_path is None:
        source_path = active_path
    try:
        same_store = source_path.resolve(strict=False) == active_path.resolve(strict=False)
    except Exception:
        same_store = source_path == active_path
    if same_store:
        _save_provider_state(auth_store, provider_id, state)
        _save_auth_store(auth_store)
        return

    _persist_provider_state_to_store(
        provider_id,
        state,
        source_path,
        set_active=True,
    )


def _store_provider_state(
    auth_store: Dict[str, Any],
    provider_id: str,
    state: Dict[str, Any],
    *,
    set_active: bool = True,
) -> None:
    providers = auth_store.setdefault("providers", {})
    if not isinstance(providers, dict):
        auth_store["providers"] = {}
        providers = auth_store["providers"]
    providers[provider_id] = state
    if set_active:
        auth_store["active_provider"] = provider_id


def _persist_provider_state_to_store(
    provider_id: str,
    state: Dict[str, Any],
    target_path: Path,
    *,
    set_active: bool = False,
) -> Path:
    """Merge one provider into a specific auth store under that store's lock."""
    with _auth_store_lock(target_path=target_path):
        auth_store = _load_auth_store(target_path)
        _store_provider_state(
            auth_store,
            provider_id,
            dict(state),
            set_active=set_active,
        )
        return _save_auth_store(auth_store, target_path=target_path)


# Preserve the extracted owner objects while routing their internal helper
# lookups back through hermes_cli.auth for monkeypatch compatibility.
_OWNER_CALLABLES = {
    name: globals()[name]
    for name in (
        "_auth_file_path",
        "_global_auth_file_path",
        "_load_global_auth_store",
        "_auth_lock_path",
        "_same_path",
        "_auth_lock_holder_for",
        "_file_lock",
        "_auth_store_lock",
        "_load_auth_store",
        "_save_auth_store",
        "_load_provider_state_with_source",
        "_provider_state_transaction",
        "_load_provider_state",
        "_save_provider_state",
        "_save_provider_state_to_source",
        "_store_provider_state",
        "_persist_provider_state_to_store",
    )
}


def _public(name: str):
    return _OWNER_CALLABLES[name]


for _late_name in _OWNER_CALLABLES:
    globals()[_late_name] = _LateBinding(_late_name)

try:
    import fcntl
except Exception:
    fcntl = None
try:
    import msvcrt
except Exception:
    msvcrt = None
