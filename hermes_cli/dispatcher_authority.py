"""Machine-global, fail-closed lifetime authority for Kanban dispatch mutation."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import secrets
import time
import weakref
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class AcquireState(str, Enum):
    ACQUIRED = "acquired"
    CONTENDED = "contended"
    UNAVAILABLE = "unavailable"


class DispatcherAuthorityError(RuntimeError):
    """Raised when dispatcher mutation is attempted without a live authority."""


@dataclass(frozen=True)
class AuthorityStatus:
    healthy: bool
    state: str
    owner_hint: Optional[str] = None
    error_class: Optional[str] = None
    mode: str = "machine-global"
    version: str = "dispatcher-authority-v1"
    freshness_seconds: Optional[int] = None


@dataclass(frozen=True)
class AcquireResult:
    state: AcquireState
    lease: Optional["DispatcherLease"] = None
    owner_hint: Optional[str] = None
    error_class: Optional[str] = None


_REGISTRY: "weakref.WeakValueDictionary[str, DispatcherLease]" = weakref.WeakValueDictionary()


def canonical_lock_path() -> Path:
    """Return one process/machine state-root lock, independent of profile/CWD."""
    raw = os.environ.get("HERMES_STATE_ROOT")
    if raw:
        root = Path(raw)
    else:
        # Profile activation may use arbitrary/nested layouts below the
        # machine installation root.  HERMES_HOME is profile-scoped, so it may
        # identify that root only by containing the conventional ``.hermes``
        # ancestor; its trailing shape must never participate in lock identity.
        home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
        installation = next(
            (candidate for candidate in (home, *home.parents) if candidate.name == ".hermes"),
            None,
        )
        if installation is not None:
            root = installation
        else:
            # Backward-compatible custom roots retain the conventional
            # <root>/profiles/<name> derivation, including nested wrappers.
            root = home
            while root.parent.name == "profiles":
                root = root.parent.parent
    return root.expanduser().resolve(strict=False) / "kanban" / ".dispatcher.lock"


def _classify_os_error(exc: BaseException) -> str:
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, FileNotFoundError):
        return "missing_parent"
    if isinstance(exc, NotADirectoryError):
        return "invalid_parent"
    if isinstance(exc, OSError):
        return f"os_error_{exc.errno or 'unknown'}"
    return "unknown_error"


class DispatcherLease:
    """Opaque process-bound capability. Instances are created only by acquire."""

    __slots__ = (
        "_fd", "_nonce", "_pid", "_path", "_released", "_start_identity", "__weakref__"
    )

    def __init__(self, token: object, fd: int, path: Path) -> None:
        if token is not _LEASE_FACTORY_TOKEN:
            raise DispatcherAuthorityError("DispatcherLease cannot be constructed by callers")
        self._fd = fd
        self._nonce = secrets.token_urlsafe(32)
        self._pid = os.getpid()
        self._path = path
        self._released = False
        self._start_identity = _process_identity()
        _REGISTRY[self._nonce] = self

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        _REGISTRY.pop(self._nonce, None)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)

    def __enter__(self) -> "DispatcherLease":
        return self

    def __exit__(self, *_args) -> None:
        self.release()

    def __copy__(self):
        raise DispatcherAuthorityError("DispatcherLease is not copyable")

    def __deepcopy__(self, _memo):
        raise DispatcherAuthorityError("DispatcherLease is not copyable")

    def __reduce__(self):
        raise DispatcherAuthorityError("DispatcherLease is not serializable")


_LEASE_FACTORY_TOKEN = object()


def _process_identity() -> tuple[int, Optional[str]]:
    marker = None
    try:
        marker = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8").split()[21]
    except Exception:
        pass
    return os.getpid(), marker


def require_dispatcher_lease(lease: object, operation: str) -> DispatcherLease:
    if not isinstance(lease, DispatcherLease):
        raise DispatcherAuthorityError(f"{operation}: live DispatcherLease required")
    valid = (
        not lease._released
        and lease._pid == os.getpid()
        and lease._start_identity == _process_identity()
        and lease._path == canonical_lock_path()
        and _REGISTRY.get(lease._nonce) is lease
    )
    if valid:
        try:
            os.fstat(lease._fd)
        except OSError:
            valid = False
    if not valid:
        raise DispatcherAuthorityError(f"{operation}: stale or foreign DispatcherLease")
    return lease


def acquire_machine_dispatcher(context: str) -> AcquireResult:
    """Acquire the canonical lock; every error is an UNAVAILABLE hard denial."""
    path = canonical_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except BaseException as exc:
        return AcquireResult(AcquireState.UNAVAILABLE, error_class=_classify_os_error(exc))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        owner = _read_owner(fd)
        os.close(fd)
        return AcquireResult(AcquireState.CONTENDED, owner_hint=owner)
    except BaseException as exc:
        os.close(fd)
        return AcquireResult(AcquireState.UNAVAILABLE, error_class=_classify_os_error(exc))
    try:
        payload = json.dumps(
            {"pid": os.getpid(), "mode": context, "version": "dispatcher-authority-v1", "at": int(time.time())},
            sort_keys=True,
        ).encode()
        os.ftruncate(fd, 0)
        os.write(fd, payload)
        os.fsync(fd)
        lease = DispatcherLease(_LEASE_FACTORY_TOKEN, fd, path)
        return AcquireResult(AcquireState.ACQUIRED, lease=lease, owner_hint=f"pid:{os.getpid()}")
    except BaseException as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        return AcquireResult(AcquireState.UNAVAILABLE, error_class=_classify_os_error(exc))


def _read_owner(fd: int) -> Optional[str]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        data = json.loads(os.read(fd, 4096).decode("utf-8"))
        pid = int(data.get("pid"))
        return f"pid:{pid}"
    except Exception:
        return None


def read_status_no_side_effects() -> AuthorityStatus:
    """Inspect an existing authority file without creating, locking, or signaling."""
    path = canonical_lock_path()
    if not path.parent.is_dir():
        return AuthorityStatus(False, "unavailable", error_class="missing_parent")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        return AuthorityStatus(False, "unavailable", error_class="missing_lock")
    except BaseException as exc:
        return AuthorityStatus(False, "unavailable", error_class=_classify_os_error(exc))
    try:
        raw = os.read(fd, 4096)
        data = json.loads(raw.decode("utf-8")) if raw else {}
        owner = f"pid:{int(data['pid'])}" if data.get("pid") else None
        age = max(0, int(time.time()) - int(data.get("at", 0))) if data.get("at") else None
        # A live PID in stale bytes is not authority.  Probe the existing inode
        # non-blockingly: successfully taking the lock proves it was unlocked,
        # while EWOULDBLOCK proves a process currently retains lifetime
        # authority.  The probe neither creates nor rewrites the file.
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            healthy = bool(owner and Path(f"/proc/{data['pid']}").exists())
        except BaseException as exc:
            return AuthorityStatus(
                False,
                "unavailable",
                owner_hint=owner,
                error_class=_classify_os_error(exc),
                freshness_seconds=age,
            )
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            healthy = False
        return AuthorityStatus(
            healthy,
            "held" if healthy else "unavailable",
            owner_hint=owner,
            error_class=None if healthy else "owner_unhealthy",
            freshness_seconds=age,
        )
    except BaseException:
        return AuthorityStatus(False, "unavailable", error_class="invalid_status")
    finally:
        os.close(fd)
