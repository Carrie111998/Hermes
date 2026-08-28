"""Persistence and recovery for Hermes authentication stores.

The callable bodies below are the byte-verbatim Auth Store persistence shard
from ``hermes_cli.auth``. Runtime dependencies that belong to the facade use
late bindings so existing imports and monkeypatch targets keep their authority.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


_ORIGINAL_PATH_READ_TEXT = Path.read_text


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


def _read_auth_bytes(auth_file: Path) -> bytes:
    """Read a regular auth file without following a final link/reparse."""
    if _is_reparse_or_link(auth_file):
        raise OSError(f"Refusing to follow auth-store link or reparse point: {auth_file}")
    # Preserve the old reader's explicit-encoding test seam. This branch only
    # runs when a test replaces Path.read_text; production reads use the
    # descriptor below, which provides the no-follow guarantee.
    if Path.read_text is not _ORIGINAL_PATH_READ_TEXT:
        auth_file.read_text(encoding="utf-8-sig")
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


def _validate_recovery_store(auth_store: Any) -> None:
    """Reject malformed replacement shapes before any primary write."""
    if not isinstance(auth_store, dict):
        raise ValueError("Recovery replacement must be a JSON object")
    providers = auth_store.get("providers")
    pool = auth_store.get("credential_pool")
    if providers is not None:
        if not isinstance(providers, dict):
            raise ValueError("Recovery replacement providers must be an object")
        if any(not isinstance(value, dict) for value in providers.values()):
            raise ValueError("Recovery replacement provider entries must be objects")
    if pool is not None:
        if not isinstance(pool, dict):
            raise ValueError("Recovery replacement credential_pool must be an object")
        for entries in pool.values():
            if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
                raise ValueError("Recovery replacement credential_pool entries must be arrays of objects")
    if providers is None and pool is None:
        raise ValueError("Recovery replacement must contain providers or credential_pool")

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
    # file pointer at position 0. Ensure the lock file has at least 1 byte.
    if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
        lock_path.write_text(" ", encoding="utf-8")

    with lock_path.open("r+" if msvcrt else "a+", encoding="utf-8") as lock_file:
        deadline = time.monotonic() + max(1.0, timeout_seconds)
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


def _write_corrupt_sidecar(auth_file: Path, raw: bytes) -> Optional[Path]:
    """Publish an immutable, exclusive forensic copy without following links."""
    parent = auth_file.parent
    parent.mkdir(parents=True, exist_ok=True)
    # Keep the historical name for the first incident. Any collision, including
    # a symlink/reparse at that name, goes to a fresh nonce path and never
    # overwrites the incumbent evidence.
    candidates = [_sidecar_candidate(auth_file, unique=False)]
    candidates.extend(_sidecar_candidate(auth_file, unique=True) for _ in range(8))
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    for destination in candidates:
        temp = parent / f".{destination.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        fd = None
        try:
            fd = os.open(
                os.fspath(temp),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                stat.S_IRUSR | stat.S_IWUSR,
            )
            with os.fdopen(fd, "wb") as handle:
                fd = None
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            # Hard-link publication is atomic and no-replace: unlike rename it
            # cannot redirect through a destination symlink or overwrite an
            # incumbent sidecar. NTFS and POSIX both support this primitive.
            os.link(os.fspath(temp), os.fspath(destination), follow_symlinks=False)
            temp.unlink()
            if _is_reparse_or_link(destination) or _digest(_read_auth_bytes(destination)) != _digest(raw):
                raise OSError(f"Corrupt sidecar verification failed: {destination}")
            return destination
        except FileExistsError:
            # A deterministic incumbent or a raced destination is evidence, not
            # an error. Try only a new nonce path; never write through it.
            pass
        except (OSError, ValueError):
            # A partial write, link/reparse, or platform publication failure is
            # fail-closed. A later nonce may still be safe to use.
            pass
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return None


def _load_auth_store(auth_file: Optional[Path] = None) -> Dict[str, Any]:
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
            corrupt_digest = _digest(raw_bytes)
            try:
                corrupt_path = _write_corrupt_sidecar(auth_file, raw_bytes)
            except Exception:
                corrupt_path = None
            preserved = corrupt_path is not None
            if preserved:
                logger.warning(
                    "auth: failed to parse %s (%s); store is read-only. "
                    "Corrupt file preserved at %s",
                    auth_file, exc, corrupt_path,
                )
                raise AuthStoreCorruptionError(
                    auth_file,
                    corrupt_path,
                    preserved=True,
                    corrupt_sha256=corrupt_digest,
                ) from exc
            logger.warning(
                "auth: failed to parse %s (%s); store is read-only. "
                "A copy could NOT be preserved",
                auth_file, exc,
            )
            raise AuthStoreCorruptionError(
                auth_file, None, preserved=False, corrupt_sha256=corrupt_digest
            ) from exc

        _remember_snapshot(raw if isinstance(raw, dict) else {}, auth_file, raw_bytes)
        if isinstance(raw, dict) and (
            isinstance(raw.get("providers"), dict)
            or isinstance(raw.get("credential_pool"), dict)
        ):
            raw.setdefault("providers", {})
            if isinstance(raw.get("providers"), dict):
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


def _save_auth_store_locked(auth_store: Dict[str, Any], auth_file: Path, *, recovery: bool) -> Path:
    """Write while the caller owns the target lock and final digest check."""
    expected_digest = None if recovery else _snapshot_for(auth_store, auth_file)
    if auth_file.exists() and not recovery:
        try:
            current_bytes = _read_auth_bytes(auth_file)
            json.loads(current_bytes.decode("utf-8-sig"))
        except OSError:
            raise
        except Exception as exc:
            raise AuthStoreRecoveryRequired(auth_file) from exc
        if expected_digest is not None and expected_digest != _digest(current_bytes):
            raise AuthStoreWriteConflictError(auth_file)
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    # Tighten parent dir to 0o700 so siblings can't traverse to creds.
    # No-op on Windows (POSIX mode bits not enforced); ignore failures.
    secure_parent_dir(auth_file)
    auth_store["version"] = AUTH_STORE_VERSION
    auth_store["updated_at"] = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(auth_store, indent=2) + "\n"
    payload_bytes = payload.encode("utf-8")
    tmp_path = auth_file.with_name(f"{auth_file.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        # Re-check after serialization and immediately before replacement. All
        # in-process writers use this same lock, so stale saves cannot erase a
        # recovery that just became visible.
        if auth_file.exists() and not recovery:
            current_bytes = _read_auth_bytes(auth_file)
            if expected_digest is not None and expected_digest != _digest(current_bytes):
                raise AuthStoreWriteConflictError(auth_file)
        atomic_replace(tmp_path, auth_file)
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
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
    try:
        auth_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
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

    ``expected_corrupt_sha256`` is the recovery CAS token. The optional
    omission preserves the historical callable API by taking an atomic digest
    snapshot under the target lock immediately before the write; CLI/operator
    flows always pass the digest captured from the corruption exception.
    """
    _validate_recovery_store(auth_store)
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
        previous = getattr(_recovery_state, "enabled", False)
        _recovery_state.enabled = True
        try:
            return _save_auth_store_locked(auth_store, auth_file, recovery=True)
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
