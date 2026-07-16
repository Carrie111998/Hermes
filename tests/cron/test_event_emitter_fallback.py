"""Regression tests for the cron event-emitter fallback (2026-07-16).

Root cause of the "emission dark windows": cron jobs in profiles/main are
ticked by TWO processes — the gateway (which runs
``events.gateway_integration.startup()`` and therefore has a bus) and the
``hermes serve`` web-server process (which does not).  Whenever the serve
process won the jobs.json claim races for a stretch of hours, every one of
its runs called ``_get_event_emitter()``, got ``get_bus() is None``, cached
the ``False`` sentinel forever, and silently dropped every
cron_started/cron_completed/cron_failed emit — while the jobs themselves ran
fine.  Downstream, watchdog_sweep's per-source MAX(timestamp) reads saw
silence and raised false alarms.

The fix: when the gateway bus is absent, fall back to a process-local
``EventBus`` on the canonical events DB (SQLite WAL is multi-process safe;
``FailureClusterDetector``'s state file was already designed for the
cross-process case).
"""

import sqlite3

import pytest


@pytest.fixture
def _reset_emitter_cache():
    """Reset cron.scheduler's module-global emitter cache around each test."""
    import cron.scheduler as sched

    old = sched._event_emitter
    sched._event_emitter = None
    yield
    sched._event_emitter = old


@pytest.fixture
def _hermetic_event_paths(tmp_path, monkeypatch):
    """Point the events DB and cluster-detector state at a tempdir."""
    db_path = tmp_path / "events" / "event_bus.db"
    monkeypatch.setattr("events.paths.events_db_path", lambda: db_path)
    monkeypatch.setattr(
        "events.producers.cron_emitter.failure_cluster_state_path",
        lambda: tmp_path / "events" / "failure_cluster_state.json",
    )
    return db_path


def test_fallback_emitter_when_gateway_bus_absent(
    _reset_emitter_cache, _hermetic_event_paths, monkeypatch
):
    """No gateway bus (non-gateway process) must NOT mean no events."""
    import cron.scheduler as sched
    import events.gateway_integration as gi

    monkeypatch.setattr(gi, "get_bus", lambda: None)

    emitter = sched._get_event_emitter()
    assert emitter is not None, (
        "_get_event_emitter() returned None with get_bus()=None — cron "
        "lifecycle events from non-gateway processes are silently dropped "
        "(the emission-dark-window bug)"
    )

    event_id = emitter.on_job_started(
        job_id="j1", job_name="test-job", schedule="*/5 * * * *"
    )
    assert event_id

    conn = sqlite3.connect(str(_hermetic_event_paths))
    try:
        rows = conn.execute(
            "SELECT event_type, source FROM events"
        ).fetchall()
    finally:
        conn.close()
    assert ("cron_started", "test-job") in rows


def test_fallback_emitter_is_cached(
    _reset_emitter_cache, _hermetic_event_paths, monkeypatch
):
    import cron.scheduler as sched
    import events.gateway_integration as gi

    monkeypatch.setattr(gi, "get_bus", lambda: None)

    first = sched._get_event_emitter()
    second = sched._get_event_emitter()
    assert first is second


def test_gateway_bus_still_preferred(
    _reset_emitter_cache, _hermetic_event_paths, monkeypatch
):
    """When the gateway bus exists, the emitter must use it (no fallback)."""
    import cron.scheduler as sched
    import events.gateway_integration as gi
    from events.bus import EventBus

    gateway_bus = EventBus(db_path=_hermetic_event_paths)
    monkeypatch.setattr(gi, "get_bus", lambda: gateway_bus)

    emitter = sched._get_event_emitter()
    assert emitter is not None
    assert emitter.bus is gateway_bus


def test_emitter_unavailable_logs_warning_and_caches_false(
    _reset_emitter_cache, _hermetic_event_paths, monkeypatch, caplog
):
    """A genuinely broken bus must leave a WARNING trace, not a DEBUG one."""
    import cron.scheduler as sched
    import events.gateway_integration as gi

    def _boom():
        raise RuntimeError("bus construction exploded")

    monkeypatch.setattr(gi, "get_bus", _boom)

    with caplog.at_level("WARNING", logger="cron.scheduler"):
        emitter = sched._get_event_emitter()

    assert emitter is None
    assert sched._event_emitter is False
    assert any(
        "cron lifecycle events will not be recorded" in r.message.lower()
        for r in caplog.records
    ), "emitter-unavailable must log at WARNING so dark windows leave a trace"
