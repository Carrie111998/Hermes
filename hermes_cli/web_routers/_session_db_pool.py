"""Bounded shared read-only SessionDB handles for dashboard session listing.

Issue #141 root cause
---------------------
The desktop polls session-list endpoints (``/api/profiles/sessions``,
``/api/profiles/sessions/sidebar``, ``/api/sessions`` ...) every few seconds.
Each request used to open a FRESH read-only SQLite connection per profile
(``file:...?mode=ro``); in WAL mode every connection holds two file
descriptors (``state.db`` + ``state.db-wal``).  Queries on large profile DBs
are slow (tens of ms each), and a client disconnect does NOT free the
server-side worker — the thread keeps running its query and holding its
connections.  Under sustained polling the in-flight connections (plus the
socket fds of backlogged requests) could grow past the process fd limit,
surfacing as ``OSError: [Errno 24] Too many open files`` and finally
``socket.accept() out of system resource`` — the dashboard becomes
unreachable and every new file open in the process fails (the original
"SSL_CERT_FILE cannot be loaded" symptom was just the first open to fail).

Fix
---
Each profile gets at most one reusable read-only SessionDB handle, checked
out exclusively per request and checked back in.  While it is checked out,
concurrent requests open short-lived "borrow" connections, capped by a small
per-profile budget, so worst-case open connections per profile are bounded
(1 cached + ``_MAX_BORROWS``) instead of scaling with request concurrency.
Idle cached handles are closed after a TTL so a live profile's WAL file can
still be checkpointed/truncated, and stale/moved handles are re-opened
lazily.

Read-only SessionDB handles are not shared across threads for concurrent
*use* — the exclusive checkout guarantees one user at a time for the cached
handle, and borrows are owned by a single request.  ``sqlite3`` is opened
with ``check_same_thread=False`` by ``SessionDB``; the pool serialises
access, so no two threads ever execute on the same connection.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from hermes_state import SessionDB

logger = logging.getLogger(__name__)

# Handles idle longer than this are closed so a live profile's WAL file can
# be truncated (an idle WAL reader pins the -wal file).
_IDLE_TTL_S = 30.0

# How long an acquirer waits for a creator to install the cached handle
# before falling through to the borrow path.  Fresh opens take ~10-100ms;
# the wait just avoids a stampede of concurrent opens on a cold cache.
_WAIT_S = 2.0

# Short-lived borrows allowed while the cached handle is checked out.
# This is the hard bound on open SQLite connections per profile: requests
# beyond 1 cached + _MAX_BORROWS block on the pool condition instead of
# opening more connections (backpressure).  A holder always releases in a
# finally, so waiters always make progress; SQLite's own busy_timeout keeps
# queries from hanging forever.  There is deliberately NO timed overflow —
# an escape hatch would re-introduce unbounded fd growth under sustained
# client-disconnect polling, the exact failure mode of #141.
_MAX_BORROWS = 2

# Schema sanity probe for read-only handles: read-only opens skip
# ``_reconcile_columns()``, so an older store would otherwise 500 on every
# poll until something opened it writable.  Mirrors the probe that used to
# live in ``web_server._open_probed`` (kept here so the pool can probe its
# own fresh opens without a circular import).
_PROBE_SQL = (
    "SELECT (SELECT archived FROM sessions LIMIT 1), "
    "(SELECT pinned FROM sessions LIMIT 1), "
    "(SELECT active FROM messages LIMIT 1), "
    "(SELECT compacted FROM messages LIMIT 1)"
)


class _Entry:
    __slots__ = ("db", "db_in_use", "creating", "borrow_count", "last_released", "db_path")

    def __init__(self) -> None:
        self.db: Optional[SessionDB] = None
        self.db_in_use = False
        self.creating = False
        self.borrow_count = 0
        self.last_released = 0.0
        self.db_path: Optional[Path] = None


_pool: Dict[str, _Entry] = {}
_pool_lock = threading.Lock()
_pool_cond = threading.Condition(_pool_lock)


def _close_safely(db: Optional[SessionDB]) -> None:
    if db is None:
        return
    try:
        db.close()
    except Exception:
        logger.debug("read-only SessionDB close failed", exc_info=True)


def _open_probed(db_path: Path) -> SessionDB:
    """Open + probe a read-only SessionDB.  Closes + re-raises on failure."""
    db = SessionDB(db_path=db_path, read_only=True)
    try:
        conn = getattr(db, "_conn", None)
        if conn is not None:
            conn.execute(_PROBE_SQL).fetchone()
        return db
    except BaseException:
        _close_safely(db)
        raise


def acquire(profile: str, db_path: Path) -> Tuple[SessionDB, bool]:
    """Check out a read-only SessionDB for ``profile``.

    Returns ``(db, cached)``.  ``cached=True`` means the handle is (or just
    became) the profile's reusable connection — return it with
    ``release(..., cached=True)`` so it stays warm; ``cached=False`` means a
    short-lived borrow (return with ``cached=False``; it will be closed).
    The ONLY call that can raise is a fresh-open failure — callers already
    degrade per-profile.

    Every acquired handle MUST be paired with :func:`release`.
    """
    stale: Optional[SessionDB] = None
    with _pool_lock:
        entry = _pool.setdefault(profile, _Entry())
        now = time.monotonic()
        # TTL / home-moved stale-cache cleanup (close outside the lock).
        if entry.db is not None and (
            entry.db_path != db_path
            or (not entry.db_in_use and now - entry.last_released > _IDLE_TTL_S)
        ):
            stale = entry.db
            entry.db = None
        # Fast path: idle cached handle.
        if entry.db is not None and not entry.db_in_use:
            entry.db_in_use = True
            entry.last_released = 0.0
            return entry.db, True
        # A creator is opening the cached handle — wait for it briefly.
        if entry.db is None and entry.creating:
            deadline = time.monotonic() + _WAIT_S
            while entry.db is None and entry.creating:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                _pool_cond.wait(remaining)
            if entry.db is not None and not entry.db_in_use:
                entry.db_in_use = True
                entry.last_released = 0.0
                return entry.db, True
        if entry.db is None and not entry.creating:
            # We install the fresh open as the cached handle.
            entry.creating = True
            creating = True
        else:
            # Cached handle busy — borrow (bounded).  Wait for a slot; the
            # holder always releases in a finally, so waiters make progress.
            # No timed overflow: opening unbounded connections under client-
            # disconnect polling is exactly the #141 failure mode.
            creating = False
            while entry.borrow_count >= _MAX_BORROWS:
                _pool_cond.wait()
                if entry.db is not None and not entry.db_in_use:
                    entry.db_in_use = True
                    entry.last_released = 0.0
                    return entry.db, True
            entry.borrow_count += 1
    if stale is not None:
        _close_safely(stale)
    try:
        db = _open_probed(db_path)
    except BaseException:
        with _pool_lock:
            entry = _pool.get(profile)
            if entry is not None:
                if creating:
                    entry.creating = False
                elif entry.borrow_count > 0:
                    entry.borrow_count -= 1
            _pool_cond.notify_all()
        raise
    if creating:
        with _pool_lock:
            entry = _pool.get(profile)
            if entry is not None:
                entry.db = db
                entry.db_path = db_path
                entry.db_in_use = True
                entry.last_released = 0.0
                entry.creating = False
            _pool_cond.notify_all()
        return db, True
    return db, False


def release(profile: str, db: SessionDB, cached: bool, invalidate: bool = False) -> None:
    """Return a handle acquired from :func:`acquire`.

    ``cached`` must match what :func:`acquire` returned.  ``invalidate=True``
    drops a cached handle (e.g. a DatabaseError surfaced while using it) so
    the next acquire opens a fresh one; borrows are always closed on release.
    """
    to_close: Optional[SessionDB] = None
    with _pool_lock:
        entry = _pool.get(profile)
        if entry is not None and cached and entry.db is db:
            entry.db_in_use = False
            if invalidate:
                entry.db = None
                to_close = db
            else:
                entry.last_released = time.monotonic()
            _pool_cond.notify_all()
        elif entry is not None and not cached:
            # borrow accounting
            if entry.borrow_count > 0:
                entry.borrow_count -= 1
            _pool_cond.notify_all()
            to_close = db
        else:
            # Handle no longer belongs to the pool (replaced meanwhile).
            to_close = db
    _close_safely(to_close)


def drop_idle(profile: str) -> None:
    """Close the cached handle if idle; leave in-flight borrows untouched.

    Used after a writable heal so a stale read-only handle can't keep
    serving errors to a freshly-migrated store.
    """
    to_close: Optional[SessionDB] = None
    with _pool_lock:
        entry = _pool.get(profile)
        if entry is not None and entry.db is not None and not entry.db_in_use:
            to_close = entry.db
            entry.db = None
    _close_safely(to_close)


def stats() -> Dict[str, dict]:
    """Diagnostics: cached/in-use/creating/borrow state per profile."""
    with _pool_lock:
        return {
            name: {
                "cached": e.db is not None,
                "in_use": e.db_in_use,
                "creating": e.creating,
                "borrows": e.borrow_count,
            }
            for name, e in sorted(_pool.items())
        }
