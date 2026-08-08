"""Tests for hermes_cli.web_routers._session_db_pool (#141).

Regression: the dashboard session-list endpoints used to open a fresh
read-only SQLite connection per profile per request; under desktop polling
plus client disconnects the open fds piled up past the process limit
(Errno 24).  The pool bounds open connections to (1 cached + borrow cap)
per profile and reuses the cached handle across requests.

Invariants under test:
- sequential acquires reuse ONE cached connection (no growth)
- concurrent acquire/use/release never exceeds 1 + _MAX_BORROWS (+ overflow)
- after all work, live tracked connections settle back to the single cached
  handle (no leak)
- TTL expiry closes the idle cached handle
- invalidate drops a poisoned cached handle
- a fresh-open failure restores borrow accounting and caches nothing
"""

import threading
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB
from hermes_cli.sqlite_safe_read import _live_connections
from hermes_cli.web_routers import _session_db_pool
from hermes_cli.web_routers._session_db_pool import (
    _IDLE_TTL_S,
    _MAX_BORROWS,
    acquire,
    drop_idle,
    release,
    stats,
)


@pytest.fixture()
def profile_db(tmp_path: Path) -> Path:
    """A real, initialised state.db for a fake profile."""
    db_path = tmp_path / "state.db"
    # Writable open initialises the schema (probe columns included).
    SessionDB(db_path=db_path, read_only=False).close()
    return db_path


def _live_count(db_path: Path) -> int:
    needle = str(db_path)
    return sum(1 for key in _live_connections if key.endswith(needle))


def _reset_pool() -> None:
    with _session_db_pool._pool_lock:
        for name, entry in list(_session_db_pool._pool.items()):
            db, entry.db = entry.db, None
            if db is not None:
                _session_db_pool._close_safely(db)
        _session_db_pool._pool.clear()


@pytest.fixture(autouse=True)
def _clean_pool():
    _reset_pool()
    yield
    _reset_pool()


def test_sequential_acquire_reuses_one_connection(profile_db: Path) -> None:
    first, cached1 = acquire("dev", profile_db)
    assert cached1 is True  # first acquire installs + checks out the cached handle
    release("dev", first, cached=True)

    handles = set()
    for _ in range(5):
        db, cached = acquire("dev", profile_db)
        assert cached is True  # reused the cached handle
        handles.add(id(db))
        release("dev", db, cached=True)

    assert len(handles) == 1, "all sequential acquires must reuse one handle"
    assert _live_count(profile_db) == 1, "exactly the cached handle stays open"


def test_concurrent_use_is_bounded(profile_db: Path) -> None:
    peak = {"value": 0}
    stop = threading.Event()

    def sampler() -> None:
        while not stop.is_set():
            v = _live_count(profile_db)
            if v > peak["value"]:
                peak["value"] = v
            time.sleep(0.002)

    sampler_thread = threading.Thread(target=sampler, daemon=True)
    sampler_thread.start()

    def worker() -> None:
        for _ in range(5):
            db, cached = acquire("dev", profile_db)
            try:
                # Hold the handle briefly so concurrent workers contend for
                # the cached slot and exercise the borrow path.
                time.sleep(0.02)
                db.list_sessions_rich(limit=1, compact_rows=True)
            finally:
                release("dev", db, cached=cached)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    sampler_thread.join()

    # Cached (1) + borrow cap (_MAX_BORROWS); overflow only after _WAIT_S,
    # which 20ms holds never reach.
    assert peak["value"] <= 1 + _MAX_BORROWS + 1, (
        f"connection count {peak['value']} exceeded bound "
        f"1 + {_MAX_BORROWS} + 1"
    )
    # No leak: after everything settles, only the single cached handle lives.
    assert _live_count(profile_db) == 1


def test_ttl_evicts_idle_cached_handle(profile_db: Path, monkeypatch) -> None:
    monkeypatch.setattr(_session_db_pool, "_IDLE_TTL_S", 0.05)

    db1, cached1 = acquire("dev", profile_db)
    release("dev", db1, cached=True)
    assert _live_count(profile_db) == 1

    time.sleep(0.08)  # idle past TTL

    db2, cached2 = acquire("dev", profile_db)
    release("dev", db2, cached=True)

    assert db2 is not db1, "TTL-expired handle must be replaced"
    assert _live_count(profile_db) == 1, "old handle closed; one cached remains"


def test_invalidate_drops_poisoned_handle(profile_db: Path) -> None:
    db1, cached1 = acquire("dev", profile_db)
    release("dev", db1, cached=True, invalidate=True)
    assert _live_count(profile_db) == 0, "invalidated handle closed immediately"

    db2, _ = acquire("dev", profile_db)
    release("dev", db2, cached=True)
    assert _live_count(profile_db) == 1


def test_open_failure_restores_accounting(profile_db: Path, monkeypatch) -> None:
    calls = {"n": 0}

    def _boom(_db_path):
        calls["n"] += 1
        raise RuntimeError("simulated open failure")

    monkeypatch.setattr(_session_db_pool, "_open_probed", _boom)

    with pytest.raises(RuntimeError):
        acquire("dev", profile_db)

    assert calls["n"] == 1
    assert stats()["dev"]["borrows"] == 0, "borrow slot must be restored"
    assert stats()["dev"]["cached"] is False, "failed open must cache nothing"


def test_drop_idle_closes_but_keeps_inflight(profile_db: Path) -> None:
    db, cached = acquire("dev", profile_db)
    assert cached is True
    drop_idle("dev")  # handle is in use — must stay alive
    assert _live_count(profile_db) == 1
    release("dev", db, cached=True)
    drop_idle("dev")  # now idle — closed
    assert _live_count(profile_db) == 0
