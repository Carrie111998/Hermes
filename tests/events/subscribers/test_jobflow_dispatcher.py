"""EventBus -> scheduler activation for JobFlow workers.

The dispatcher turns an actionable mailbox message into an in-memory wake.
It never writes jobs.json, never re-enables a disabled worker, and never
dispatches the same logical work twice.

Default mode is OFF: registering the subscriber must change nothing until an
operator opts in via HERMES_JOBFLOW_EVENT_DISPATCH.
"""

from __future__ import annotations

import pytest

from events.schema import Event, EventType, Priority
from events.subscribers.jobflow_dispatcher import JobFlowDispatcher
from jobflow_dispatch.store import ActivationStore


@pytest.fixture
def store(tmp_path):
    return ActivationStore(tmp_path / "dispatch.db", lease_seconds=900)


@pytest.fixture
def woken():
    return []


def _dispatcher(store, woken, *, mode="on", resolver=None):
    return JobFlowDispatcher(
        bus=None,
        store=store,
        resolve_job_id=resolver or (lambda activity_id: f"job-for-{activity_id}"),
        waker=lambda job_id, **kw: woken.append((job_id, kw.get("reason"))) or True,
        mode=mode,
    )


def _event(message_type="TAILOR_REQUEST", to="tailor",
           file="tailor/inbox/20260810T01_TAILOR_REQUEST_main_aa.json",
           correlation_id="c1"):
    return Event(
        event_id="e1",
        event_type=EventType.MAILBOX_MESSAGE,
        source="tracker",
        timestamp="2026-08-10T20:00:00Z",
        priority=Priority.LOW,
        payload={"message_type": message_type, "to": to, "file": file,
                 "from": "tracker", "summary": "s", "inner_payload": {}},
        correlation_id=correlation_id,
    )


class TestActivation:
    def test_actionable_message_wakes_its_worker(self, store, woken):
        _dispatcher(store, woken).handle(_event())
        assert woken == [("job-for-jobflow.tailor.generate", "mailbox_message")]

    def test_duplicate_event_dispatches_once(self, store, woken):
        d = _dispatcher(store, woken)
        event = _event()
        d.handle(event)
        d.handle(event)
        assert len(woken) == 1

    def test_non_actionable_message_wakes_nobody(self, store, woken):
        _dispatcher(store, woken).handle(_event("NOTIFICATION", to="main"))
        assert woken == []

    def test_misrouted_message_wakes_nobody(self, store, woken):
        _dispatcher(store, woken).handle(_event("SCORE_REQUEST", to="tailor"))
        assert woken == []


class TestModes:
    def test_off_is_the_default_and_dispatches_nothing(self, store, woken):
        d = JobFlowDispatcher(
            bus=None, store=store,
            resolve_job_id=lambda a: "j1",
            waker=lambda job_id, **kw: woken.append(job_id),
        )
        d.handle(_event())
        assert woken == []

    def test_shadow_claims_but_does_not_wake(self, store, woken):
        """Shadow must record the decision without acting on it."""
        d = _dispatcher(store, woken, mode="shadow")
        d.handle(_event())
        assert woken == []
        assert store.get("tailor/inbox/20260810T01_TAILOR_REQUEST_main_aa.json",
                         "jobflow.tailor.generate") is not None


class TestFailClosed:
    def test_unresolvable_activity_is_skipped(self, store, woken):
        d = _dispatcher(store, woken, resolver=lambda a: None)
        d.handle(_event())
        assert woken == []

    def test_resolver_failure_never_escapes(self, store, woken):
        def _boom(activity_id):
            raise RuntimeError("jobs.json unreadable")

        d = _dispatcher(store, woken, resolver=_boom)
        d.handle(_event())
        assert woken == []

    def test_missing_file_key_falls_back_to_correlation_id(self, store, woken):
        event = _event(file=None, correlation_id="corr-7")
        _dispatcher(store, woken).handle(event)
        assert len(woken) == 1
        assert store.get("corr-7", "jobflow.tailor.generate") is not None

    def test_no_key_at_all_is_dropped(self, store, woken):
        _dispatcher(store, woken).handle(_event(file=None, correlation_id=None))
        assert woken == []

    def test_malformed_payload_never_raises(self, store, woken):
        bad = Event(event_id="e2", event_type=EventType.MAILBOX_MESSAGE, source="x",
                    timestamp="2026-08-10T20:00:00Z", priority=Priority.LOW,
                    payload={"message_type": 42, "to": None}, correlation_id="c")
        _dispatcher(store, woken).handle(bad)
        assert woken == []


class TestKnownCoverageGap:
    def test_research_request_has_no_event_path(self, store, woken):
        """RESEARCH_REQUEST is absent from MailboxWatcher.MIRRORED_MESSAGE_TYPES.

        The researcher therefore CANNOT be event-activated and depends on the
        deterministic reconciler. Adding the type to the watcher would also
        change notification delivery, which this workstream must not do.
        Encoded as a test so the gap is explicit rather than discovered later.
        """
        from events.producers.mailbox_watcher import MIRRORED_MESSAGE_TYPES

        assert "RESEARCH_REQUEST" not in MIRRORED_MESSAGE_TYPES

        # The route exists, so the reconciler can still activate it.
        from jobflow_dispatch.contracts import route_mailbox
        assert route_mailbox("RESEARCH_REQUEST", "researcher", {}) == (
            "cron.jobflow.researcher",
        )
