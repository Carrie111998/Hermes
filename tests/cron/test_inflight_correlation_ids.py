"""cron.inflight.current_inflight_correlation_ids — the accessor behind the
GATEWAY_STOPPED payload key ``inflight_cron_correlation_ids``.

``gateway/run.py``'s shutdown path has imported this symbol since 2026-04-30
(M1, GATEWAY_STARTED/STOPPED lifecycle events) inside a try/except that falls
back to ``[]``. The module never existed, so every GATEWAY_STOPPED ever emitted
carried an empty list: a restart could not say which crons it killed.

The correlation id is the ``cron_started`` event_id, matching the existing
``prior_cron_started_event_id`` convention that ``cron_skipped_duplicate``
already uses (cron/scheduler.py, events/producers/cron_emitter.py).
"""

import sys

import pytest


@pytest.fixture
def clean_registry():
    """Give each test an empty in-flight registry and restore it afterwards."""
    from cron import scheduler

    with scheduler._in_flight_lock:
        saved = dict(scheduler._in_flight)
        scheduler._in_flight.clear()
    try:
        yield scheduler
    finally:
        with scheduler._in_flight_lock:
            scheduler._in_flight.clear()
            scheduler._in_flight.update(saved)


def test_returns_empty_list_when_scheduler_module_never_loaded(monkeypatch):
    """Shutdown must not pay a heavy ``cron.scheduler`` import just to ask the
    question. If the scheduler was never imported, no cron can be in flight,
    so the honest answer is [] — reached without importing anything."""
    from cron.inflight import current_inflight_correlation_ids

    monkeypatch.delitem(sys.modules, "cron.scheduler", raising=False)

    assert current_inflight_correlation_ids() == []
    assert "cron.scheduler" not in sys.modules, (
        "asking the question must not import the scheduler"
    )


def test_returns_empty_list_when_no_crons_in_flight(clean_registry):
    from cron.inflight import current_inflight_correlation_ids

    assert current_inflight_correlation_ids() == []


def test_returns_started_event_ids_of_in_flight_crons(clean_registry):
    """The happy path: two crons running, both past on_job_started."""
    from cron.inflight import current_inflight_correlation_ids

    scheduler = clean_registry
    assert scheduler._try_register_in_flight("job-a", "Job A") is None
    assert scheduler._try_register_in_flight("job-b", "Job B") is None
    scheduler._attach_started_event_id("job-a", "evt-aaa")
    scheduler._attach_started_event_id("job-b", "evt-bbb")

    assert sorted(current_inflight_correlation_ids()) == ["evt-aaa", "evt-bbb"]


def test_skips_records_whose_started_event_id_is_not_yet_attached(clean_registry):
    """A job registers its slot BEFORE on_job_started returns, so there is a
    real window where cron_started_event_id is None. A None is not a
    correlation id and must not reach the payload as a null entry."""
    from cron.inflight import current_inflight_correlation_ids

    scheduler = clean_registry
    assert scheduler._try_register_in_flight("job-pending", "Pending") is None
    assert scheduler._try_register_in_flight("job-ready", "Ready") is None
    scheduler._attach_started_event_id("job-ready", "evt-ready")

    assert current_inflight_correlation_ids() == ["evt-ready"]


def test_released_job_drops_out_of_the_list(clean_registry):
    """A cron that finished normally is not 'killed by shutdown'."""
    from cron.inflight import current_inflight_correlation_ids

    scheduler = clean_registry
    scheduler._try_register_in_flight("job-done", "Done")
    scheduler._attach_started_event_id("job-done", "evt-done")
    assert current_inflight_correlation_ids() == ["evt-done"]

    scheduler._release_in_flight("job-done")

    assert current_inflight_correlation_ids() == []


def test_never_raises_when_the_registry_is_unusable(monkeypatch):
    """This runs on the gateway shutdown path. A failure to answer must degrade
    to [] rather than propagate and abort the GATEWAY_STOPPED emission.

    A stand-in module is injected rather than breaking the real scheduler's
    lock: monkeypatching the live ``_in_flight_lock`` makes any fixture that
    also takes that lock explode during teardown.
    """
    from cron.inflight import current_inflight_correlation_ids

    class _ExplodingLock:
        def __enter__(self):
            raise RuntimeError("lock unavailable")

        def __exit__(self, *a):
            return False

    class _BadScheduler:
        _in_flight_lock = _ExplodingLock()
        _in_flight: dict = {}

    monkeypatch.setitem(sys.modules, "cron.scheduler", _BadScheduler())

    assert current_inflight_correlation_ids() == []


def test_never_raises_when_the_registry_is_missing_attributes(monkeypatch):
    """A scheduler module present but without the Guard #3 registry (an older
    or partially-initialised import) must also degrade to []."""
    from cron.inflight import current_inflight_correlation_ids

    class _EmptyScheduler:
        pass

    monkeypatch.setitem(sys.modules, "cron.scheduler", _EmptyScheduler())

    assert current_inflight_correlation_ids() == []


def test_gateway_shutdown_hook_can_import_the_symbol():
    """gateway/run.py does `from cron.inflight import
    current_inflight_correlation_ids` and calls list() on the result."""
    from cron.inflight import current_inflight_correlation_ids

    assert callable(current_inflight_correlation_ids)
    assert isinstance(list(current_inflight_correlation_ids()), list)
