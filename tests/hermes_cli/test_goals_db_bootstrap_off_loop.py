"""SessionDB bootstrap must never run schema init on an event-loop thread.

Enterprise field report (2026-08-14): after an unclean shutdown plus a
double-instance startup race, the v25 schema migration inside
``SessionDB.__init__`` blocked the gateway's event-loop thread (reached via
``_post_turn_goal_continuation`` → ``GoalManager()`` → ``load_goal`` →
``_get_session_db``). The loop-liveness watchdog missed 3 probes and killed
the process with exit 75; the supervisor restarted into the same state —
an unbounded crash loop.

Contract fixed here, at the shared boundary (goals.py ``_get_session_db``,
which the heartbeat module reuses):

- Called on a thread with a RUNNING event loop and no cached DB, it must
  NOT construct SessionDB inline. It returns None immediately (callers
  already degrade gracefully on None) and populates the cache from a
  background thread.
- Called on a plain worker thread, it constructs inline as before.
- Once cached, every thread gets the cached instance.
"""

import asyncio
import threading
import time

import pytest

import hermes_cli.goals as goals


class _RecordingDB:
    """Stands in for SessionDB; records which thread constructed it."""

    constructed_on: list = []

    def __init__(self):
        _RecordingDB.constructed_on.append(threading.get_ident())

    def get_meta(self, key):
        return None


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    _RecordingDB.constructed_on = []
    monkeypatch.setattr(goals, "_DB_CACHE", {})
    yield


def _patch_sessiondb(monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "SessionDB", _RecordingDB)


def test_loop_thread_cache_miss_constructs_off_loop(monkeypatch):
    """On the event-loop thread with a cold cache: construction happens on a
    background thread (never the loop thread). A fast init is returned via
    the grace window; either way the loop thread never runs SessionDB()."""
    _patch_sessiondb(monkeypatch)
    loop_thread_id = None
    inline_result = "UNSET"

    async def main():
        nonlocal loop_thread_id, inline_result
        loop_thread_id = threading.get_ident()
        inline_result = goals._get_session_db()

    asyncio.run(main())

    # Fast init: the grace window returns the real instance (no silent
    # feature loss on the first call).
    assert isinstance(inline_result, _RecordingDB)
    # Cache populated for subsequent calls.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not goals._DB_CACHE:
        time.sleep(0.02)
    assert goals._DB_CACHE, "background bootstrap never populated the cache"
    assert _RecordingDB.constructed_on, "SessionDB never constructed"
    assert all(t != loop_thread_id for t in _RecordingDB.constructed_on), (
        "SessionDB was constructed on the event-loop thread"
    )


def test_loop_thread_cache_hit_returns_cached_instance(monkeypatch):
    """A warm cache is returned directly even on the loop thread."""
    _patch_sessiondb(monkeypatch)
    sentinel = _RecordingDB()
    from hermes_constants import get_hermes_home

    goals._DB_CACHE[str(get_hermes_home())] = sentinel
    result = "UNSET"

    async def main():
        nonlocal result
        result = goals._get_session_db()

    asyncio.run(main())
    assert result is sentinel


def test_worker_thread_constructs_inline(monkeypatch):
    """No running loop on the calling thread → construct inline, return it."""
    _patch_sessiondb(monkeypatch)
    db = goals._get_session_db()
    assert db is not None
    assert isinstance(db, _RecordingDB)
    assert _RecordingDB.constructed_on == [threading.get_ident()]


def test_slow_construction_does_not_block_the_loop(monkeypatch):
    """A SessionDB whose init blocks (locked-DB migration) must not stall
    the event loop past the watchdog probe window: bounded grace wait,
    then degrade to None."""
    import hermes_state

    class _BlockingDB:
        def __init__(self):
            time.sleep(2.0)  # simulated contended migration

        def get_meta(self, key):
            return None

    monkeypatch.setattr(hermes_state, "SessionDB", _BlockingDB)
    elapsed = None
    result = "UNSET"

    async def main():
        nonlocal elapsed, result
        t0 = time.monotonic()
        result = goals._get_session_db()
        elapsed = time.monotonic() - t0

    asyncio.run(main())
    assert result is None, "contended init must degrade to None"
    assert elapsed is not None and elapsed < 1.0, (
        f"loop-thread call blocked for {elapsed:.2f}s — watchdog territory"
    )


class _SlowThenReadyDB:
    """Stands in for a cold-disk SessionDB: init (schema + FTS DDL) is slow
    but healthy — finishes well past the 0.25s read window, well inside the
    write window. Records set_meta writes."""

    writes: dict = {}

    def __init__(self):
        time.sleep(0.6)

    def get_meta(self, key):
        return self.writes.get(key)

    def set_meta(self, key, value):
        self.writes[key] = value


def test_save_goal_waits_out_slow_bootstrap(monkeypatch):
    """A slow-but-healthy cold init must not drop the first /goal write
    (#89175): save_goal waits the one-shot write window for durability, so
    the row lands even when init exceeds the read grace window."""
    import hermes_state

    _SlowThenReadyDB.writes = {}
    monkeypatch.setattr(hermes_state, "SessionDB", _SlowThenReadyDB)
    persisted = []

    async def main():
        state = goals.GoalState(goal="ship it", max_turns=7)
        goals.save_goal("sess-89175", state)

    asyncio.run(main())
    db = next(iter(goals._DB_CACHE.values()), None)
    assert db is not None, "background bootstrap never populated the cache"
    persisted = db.writes.get("goal:sess-89175")
    assert persisted, (
        "first /goal write was dropped: bootstrap exceeded the wait window"
    )
    assert '"ship it"' in persisted


def test_load_goal_keeps_tight_loop_window(monkeypatch):
    """Reads keep the 0.25s grace window: a slow init degrades load_goal to
    None quickly instead of stalling the loop thread (#89175)."""
    import hermes_state

    _SlowThenReadyDB.writes = {}
    monkeypatch.setattr(hermes_state, "SessionDB", _SlowThenReadyDB)
    elapsed = None
    result = "UNSET"

    async def main():
        nonlocal elapsed, result
        t0 = time.monotonic()
        result = goals.load_goal("sess-89175")
        elapsed = time.monotonic() - t0

    asyncio.run(main())
    assert result is None, "slow init must not fabricate a goal for reads"
    assert elapsed is not None and elapsed < 0.5, (
        f"read path blocked for {elapsed:.2f}s — reads must keep the tight window"
    )


def test_save_goal_drop_warns_after_write_wait(monkeypatch, caplog):
    """When even the write window expires (wedged init), the dropped write
    is surfaced at WARNING instead of vanishing below DEBUG (#89175)."""
    import hermes_state

    class _NeverReadyDB:
        def __init__(self):
            time.sleep(30.0)  # wedged far past any wait

        def get_meta(self, key):
            return None

    monkeypatch.setattr(hermes_state, "SessionDB", _NeverReadyDB)
    monkeypatch.setattr(goals, "_DB_BOOTSTRAP_WRITE_WAIT_S", 0.15)
    wrote = []

    async def main():
        import logging

        with caplog.at_level(logging.WARNING, logger="hermes_cli.goals"):
            state = goals.GoalState(goal="later", max_turns=3)
            t0 = time.monotonic()
            goals.save_goal("sess-wedged", state)
            wrote.append(time.monotonic() - t0)

    asyncio.run(main())
    assert wrote and wrote[0] < 1.0, "write wait must stay bounded"
    assert any(
        "dropped" in rec.message for rec in caplog.records
    ), "dropped goal write must log at WARNING"
