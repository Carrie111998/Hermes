"""RepositoryLock hardening: path normalization, operation scoping, shared
filesystem (foreign-host) locks, and privileged-owner fallback."""

from __future__ import annotations

import os
import time

import psutil
import pytest

# hermes_worker_bridge is a plugin living outside this repo
# (<hermes-home>/plugins/worker-bridge), so it is not importable from a plain
# checkout. Skip rather than fail collection for the whole suite.
pytest.importorskip(
    "hermes_worker_bridge",
    reason="worker-bridge plugin not installed on sys.path",
)

from hermes_constants import get_hermes_home  # noqa: E402
from hermes_worker_bridge import workspace  # noqa: E402
from hermes_worker_bridge.workspace import (
    RepositoryLock,
    WorkspaceError,
    _normalize_repository,
)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "Repo"
    path.mkdir()
    return path


# --- 4. Path normalization ------------------------------------------------


def test_trailing_slash_maps_to_same_lock(repo):
    a = RepositoryLock(str(repo))
    b = RepositoryLock(str(repo) + os.sep)
    assert a._path == b._path


def test_normalize_strips_trailing_and_resolves(repo):
    assert _normalize_repository(str(repo) + os.sep) == _normalize_repository(str(repo))


@pytest.mark.skipif(os.name != "nt", reason="case-folding only on Windows")
def test_case_insensitive_on_windows(repo):
    lower = RepositoryLock(str(repo).lower())
    upper = RepositoryLock(str(repo).upper())
    assert lower._path == upper._path


# --- 3. Operation-scoped sub-keys (starvation) ----------------------------


def test_operations_have_distinct_lock_paths(repo):
    alloc = RepositoryLock(repo, operation="allocate")
    integrate = RepositoryLock(repo, operation="integrate")
    assert alloc._path != integrate._path


def test_distinct_operations_do_not_serialize(repo):
    """Two different operations on one repo can be held simultaneously."""
    with RepositoryLock(repo, operation="allocate"):
        with RepositoryLock(repo, operation="integrate"):
            pass  # both held at once without deadlock/timeout


def test_same_operation_still_excludes(repo, monkeypatch):
    monkeypatch.setenv("HERMES_LOCK_WAIT_SECONDS", "0")
    monkeypatch.setattr(workspace, "_LOCK_WAIT_SECONDS", 0.0)
    with RepositoryLock(repo, operation="allocate"):
        # Distinct thread-lock object per identifier is avoided; same op/repo
        # shares one file. A second acquire from a foreign identifier must
        # observe the existing lock file and fail fast.
        other = RepositoryLock(repo, operation="allocate", identifier="other")
        other._thread_lock = workspace.threading.Lock()  # bypass in-process gate
        with pytest.raises(WorkspaceError):
            other.__enter__()


# --- 1. Shared filesystem: foreign-host locks -----------------------------


def _lock_path(repo, operation="default"):
    lock = RepositoryLock(repo, operation=operation)
    lock._path.parent.mkdir(parents=True, exist_ok=True)
    return lock


def test_foreign_host_lock_not_stolen_before_ttl(repo, monkeypatch):
    monkeypatch.setattr(workspace, "_LOCK_WAIT_SECONDS", 0.0)
    lock = _lock_path(repo)
    # A live lock from another host, written "now": PID is meaningless locally,
    # TTL has not elapsed, so it must be treated as alive (not reclaimable).
    lock._path.write_text(
        f"999999|0.0|{time.time()}|other-host|abc", encoding="ascii"
    )
    assert lock._is_stale() is False
    with pytest.raises(WorkspaceError):
        lock.__enter__()


def test_foreign_host_lock_reclaimed_after_ttl(repo):
    lock = _lock_path(repo)
    old = time.time() - workspace._LOCK_STALE - 60
    lock._path.write_text(f"999999|0.0|{old}|other-host|abc", encoding="ascii")
    assert lock._is_stale() is True


def test_local_host_dead_pid_reclaimed_after_ttl(repo):
    lock = _lock_path(repo)
    old = time.time() - workspace._LOCK_STALE - 60
    dead_pid = _find_free_pid()
    lock._path.write_text(
        f"{dead_pid}|0.0|{old}|{workspace._LOCAL_HOSTNAME}|abc", encoding="ascii"
    )
    assert lock._is_stale() is True


# --- 2. Privileged / inaccessible owner -----------------------------------


def test_access_denied_falls_back_to_mtime(repo, monkeypatch):
    lock = _lock_path(repo)
    old = time.time() - workspace._LOCK_STALE - 60
    lock._path.write_text(
        f"{os.getpid()}|0.0|{old}|{workspace._LOCAL_HOSTNAME}|abc", encoding="ascii"
    )
    # Age the file so mtime staleness triggers.
    os.utime(lock._path, (old, old))

    def _raise_access_denied(self):
        raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil.Process, "create_time", _raise_access_denied)
    # pid_exists true, create_time inaccessible -> fall back to mtime -> stale.
    assert lock._is_stale() is True


def test_access_denied_live_lock_not_stolen(repo, monkeypatch):
    lock = _lock_path(repo)
    lock._path.write_text(
        f"{os.getpid()}|0.0|{time.time()}|{workspace._LOCAL_HOSTNAME}|abc",
        encoding="ascii",
    )

    def _raise_access_denied(self):
        raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil.Process, "create_time", _raise_access_denied)
    # Fresh mtime + inaccessible owner: do not steal a live lock.
    assert lock._is_stale() is False


# --- basic behaviour ------------------------------------------------------


def test_acquire_and_release_roundtrip(repo):
    lock = RepositoryLock(repo)
    with lock:
        assert lock._path.exists()
    assert not lock._path.exists()


def _find_free_pid() -> int:
    """A PID that is (almost certainly) not a running process."""
    for candidate in range(2_000_000, 2_001_000):
        if not psutil.pid_exists(candidate):
            return candidate
    return 2_000_000
