"""Persistence and recovery for Hermes authentication stores.

The callable bodies below are the byte-verbatim Auth Store persistence shard
from ``hermes_cli.auth``. Runtime dependencies that belong to the facade use
late bindings so existing imports and monkeypatch targets keep their authority.
"""

from __future__ import annotations

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


class AuthStoreCorruptionError(RuntimeError):
    """The auth store is corrupt and must remain read-only until recovery."""

    def __init__(
        self,
        auth_file: Path,
        corrupt_path: Optional[Path],
        *,
        preserved: bool,
    ) -> None:
        self.auth_file = auth_file
        self.path = auth_file
        self.corrupt_path = corrupt_path
        self.preserved = preserved
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


def _load_auth_store(auth_file: Optional[Path] = None) -> Dict[str, Any]:
    auth_file = auth_file or _auth_file_path()
    if not auth_file.exists():
        return {"version": AUTH_STORE_VERSION, "providers": {}}

    try:
        raw = json.loads(auth_file.read_text(encoding="utf-8-sig"))
    except OSError:
        # The file exists (checked above) but could not be READ: EMFILE under
        # fd exhaustion, EACCES, EIO, a stalled network mount. None of those
        # mean the contents are bad, and this module does read-modify-write in
        # ~15 places, so degrading to an empty store here is one
        # _save_auth_store() away from erasing every stored credential.
        # Fail loudly instead and leave the file on disk untouched.
        logger.warning(
            "auth: could not read %s, leaving the store on disk untouched "
            "rather than degrading to an empty one",
            auth_file, exc_info=True,
        )
        raise
    except Exception as exc:
        # Genuine corruption: unparseable JSON, or bytes that are not UTF-8.
        corrupt_path = auth_file.with_suffix(".json.corrupt")
        preserved = False
        try:
            import shutil
            shutil.copy2(auth_file, corrupt_path)
            preserved = True
        except Exception:
            logger.debug(
                "auth: could not preserve a copy of the corrupt store at %s",
                corrupt_path, exc_info=True,
            )
        if preserved:
            logger.warning(
                "auth: failed to parse %s (%s); store is read-only. "
                "Corrupt file preserved at %s",
                auth_file, exc, corrupt_path,
            )
            raise AuthStoreCorruptionError(
                auth_file, corrupt_path, preserved=True
            ) from exc
        # Do not advertise a backup that was never written.
        logger.warning(
            "auth: failed to parse %s (%s); store is read-only. "
            "A copy could NOT be preserved at %s",
            auth_file, exc, corrupt_path,
        )
        raise AuthStoreCorruptionError(
            auth_file, None, preserved=False
        ) from exc

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
        return {"version": AUTH_STORE_VERSION, "providers": providers,
                "active_provider": "nous" if providers else None}

    return {"version": AUTH_STORE_VERSION, "providers": {}}


def _save_auth_store(auth_store: Dict[str, Any], target_path: Optional[Path] = None) -> Path:
    # target_path=None preserves the existing contract (write the active
    # store at _auth_file_path()). An explicit path lets callers persist a
    # specific store — e.g. the global-root write-through for rotating xAI
    # OAuth grants (#43589) — reusing this function's atomic O_EXCL + 0o600
    # write so the root auth.json gets the same TOCTOU-safe treatment.
    auth_file = target_path if target_path is not None else _auth_file_path()
    if auth_file.exists() and not getattr(_recovery_state, "enabled", False):
        try:
            json.loads(auth_file.read_text(encoding="utf-8-sig"))
        except OSError:
            raise
        except Exception as exc:
            raise AuthStoreRecoveryRequired(auth_file) from exc
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    # Tighten parent dir to 0o700 so siblings can't traverse to creds.
    # No-op on Windows (POSIX mode bits not enforced); ignore failures.
    # secure_parent_dir refuses to chmod /, top-level dirs, or the
    # hermes-agent install tree (#25821, #93050).
    secure_parent_dir(auth_file)
    auth_store["version"] = AUTH_STORE_VERSION
    auth_store["updated_at"] = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(auth_store, indent=2) + "\n"
    tmp_path = auth_file.with_name(f"{auth_file.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        # Create with 0o600 atomically via os.open(O_EXCL) + fdopen to close
        # the TOCTOU window where default umask (often 0o644) briefly exposed
        # OAuth tokens to other local users between open() and chmod().
        # Mirrors agent/google_oauth.py (#19673) and tools/mcp_oauth.py (#21148).
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
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
    # Restrict file permissions to owner only
    try:
        auth_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return auth_file


def recover_auth_store(
    auth_store: Dict[str, Any], target_path: Optional[Path] = None
) -> Path:
    """Explicitly import a replacement after a corrupt store is reviewed."""
    auth_file = target_path if target_path is not None else _auth_file_path()
    previous = getattr(_recovery_state, "enabled", False)
    _recovery_state.enabled = True
    try:
        return _save_auth_store(auth_store, target_path=auth_file)
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
