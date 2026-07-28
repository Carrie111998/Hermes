"""Staleness/normalization/scoping guarantees for RepositoryLock.

Covers the four hardening fixes:
  1. Shared-filesystem foreign-host locks are reclaimed only by TTL expiry.
  2. Privileged/inaccessible owners fall back to lock-file mtime staleness.
  3. Operation sub-keys keep unrelated ops on one repo from serializing.
  4. Path normalization (realpath + trailing slash + Windows case-fold).
"""

from __future__ import annotations

import os

import psutil
import pytest

# hermes_worker_bridge is a plugin living outside this repo
# (<hermes-home>/plugins/worker-bridge), so it is not importable from a plain
# checkout. Skip rather than fail collection for the whole suite.
pytest.importorskip(
    "hermes_worker_bridge",
    reason="worker-bridge plugin not installed on sys.path",
)

from hermes_worker_bridge import workspace  # noqa: E402
from hermes_worker_bridge.workspace import RepositoryLock, _normalize_repository


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_HOME_OVERRIDE", raising=False)
    return home


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


# --- 4. path normalization -------------------------------------------------

def test_trailing_slash_and_realpath_collide(hermes_home, tmp_path):
    repo = _repo(tmp_path)
    a = RepositoryLock(str(repo))
    b = RepositoryLock(str(repo) + os.sep)
    assert a._path == b._path
    assert a._thread_lock is b._thread_lock


@pytest.mark.skipif(os.name != "nt", reason="case-folding only on Windows filesystems")
def test_case_insensitive_collision_on_windows(hermes_home, tmp_path):
    repo = _repo(tmp_path)
    a = RepositoryLock(str(repo).upper())
    b = RepositoryLock(str(repo).lower())
    assert a._path == b._path
    assert a._thread_lock is b._thread_lock


def test_normalize_strips_trailing_separators():
    normalized = _normalize_repository(os.sep + "repo" + os.sep)
    assert not normalized.endswith(os.sep)


# --- 3. operation-scoped sub-keys ------------------------------------------

def test_distinct_operations_do_not_share_lock(hermes_home, tmp_path):
    repo = _repo(tmp_path)
    allocate = RepositoryLock(str(repo), operation="allocate")
    integrate = RepositoryLock(str(repo), operation="integrate")
    assert allocate._path != integrate._path
    assert allocate._thread_lock is not integrate._thread_lock


def test_same_operation_shares_lock(hermes_home, tmp_path):
    repo = _repo(tmp_path)
    a = RepositoryLock(str(repo), operation="allocate")
    b = RepositoryLock(str(repo), operation="allocate")
    assert a._path == b._path
    assert a._thread_lock is b._thread_lock


def test_default_operation_is_stable(hermes_home, tmp_path):
    repo = _repo(tmp_path)
    assert RepositoryLock(str(repo))._path == RepositoryLock(str(repo))._path


# --- 1. shared-filesystem foreign-host detection ---------------------------

def _write_lock(lock: RepositoryLock, content: str) -> None:
    lock._path.parent.mkdir(parents=True, exist_ok=True)
    lock._path.write_text(content, encoding="ascii")


def test_foreign_host_fresh_lock_is_not_stale(hermes_home, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    lock = RepositoryLock(str(repo))
    # PID 999999 almost certainly does not exist locally; if this were probed
    # locally the lock would look dead. Foreign host must skip that probe.
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)
    _write_lock(lock, f"999999|123.0|{workspace.time.time()}|other-host|abcd")
    assert lock._is_stale() is False


def test_foreign_host_expired_lock_is_stale(hermes_home, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    lock = RepositoryLock(str(repo))
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
    old = workspace.time.time() - (workspace._LOCK_STALE + 60)
    _write_lock(lock, f"999999|123.0|{old}|other-host|abcd")
    assert lock._is_stale() is True


# --- 2. privileged / inaccessible owner ------------------------------------

def test_access_denied_fresh_lock_is_not_stale(hermes_home, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    lock = RepositoryLock(str(repo))
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)

    def _boom(_pid):
        raise psutil.AccessDenied(_pid)

    monkeypatch.setattr(psutil, "Process", _boom)
    _write_lock(lock, f"4321|123.0|{workspace.time.time()}|{workspace._LOCAL_HOSTNAME}|abcd")
    # Process is alive-but-inaccessible and the lock file is fresh -> keep it.
    assert lock._is_stale() is False


def test_access_denied_stale_by_mtime(hermes_home, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    lock = RepositoryLock(str(repo))
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)

    def _boom(_pid):
        raise psutil.AccessDenied(_pid)

    monkeypatch.setattr(psutil, "Process", _boom)
    _write_lock(lock, f"4321|123.0|{workspace.time.time()}|{workspace._LOCAL_HOSTNAME}|abcd")
    old = workspace.time.time() - (workspace._LOCK_STALE + 60)
    os.utime(lock._path, (old, old))
    assert lock._is_stale() is True


def test_no_such_process_fresh_lock_not_reclaimed(hermes_home, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    lock = RepositoryLock(str(repo))
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)

    def _gone(_pid):
        raise psutil.NoSuchProcess(_pid)

    monkeypatch.setattr(psutil, "Process", _gone)
    # Owner vanished, but the lock is fresh: reclaim still waits for TTL so a
    # just-created lock is never yanked out from under a racing acquirer.
    _write_lock(lock, f"4321|123.0|{workspace.time.time()}|{workspace._LOCAL_HOSTNAME}|abcd")
    assert lock._is_stale() is False


def test_no_such_process_expired_is_stale(hermes_home, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    lock = RepositoryLock(str(repo))
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)

    def _gone(_pid):
        raise psutil.NoSuchProcess(_pid)

    monkeypatch.setattr(psutil, "Process", _gone)
    old = workspace.time.time() - (workspace._LOCK_STALE + 60)
    _write_lock(lock, f"4321|123.0|{old}|{workspace._LOCAL_HOSTNAME}|abcd")
    assert lock._is_stale() is True


# --- end-to-end: acquire / reacquire ---------------------------------------

def test_acquire_and_release_roundtrip(hermes_home, tmp_path):
    repo = _repo(tmp_path)
    with RepositoryLock(str(repo)):
        assert (RepositoryLock(str(repo))._path).exists()
    # Released -> file gone, reacquirable.
    with RepositoryLock(str(repo)):
        pass


def test_concurrent_different_operations_do_not_block(hermes_home, tmp_path):
    repo = _repo(tmp_path)
    with RepositoryLock(str(repo), operation="allocate"):
        # A different operation on the same repo must not deadlock/serialize.
        with RepositoryLock(str(repo), operation="integrate"):
            pass
