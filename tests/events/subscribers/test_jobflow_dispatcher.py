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


def _dispatcher(store, woken, *, mode="on", resolver=None, clock=None):
    kwargs = {"clock": clock} if clock is not None else {}
    return JobFlowDispatcher(
        bus=None,
        store=store,
        resolve_job_id=resolver or (lambda activity_id: f"job-for-{activity_id}"),
        waker=lambda job_id, **kw: woken.append((job_id, kw.get("reason"))) or True,
        mode=mode,
        **kwargs,
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

    def test_shadow_records_the_decision_without_acting_or_residue(self, store, woken):
        """Shadow observes; it must leave the ledger exactly as it found it.

        An earlier version of this test asserted shadow KEEPS its claim. That
        was wrong, and dangerously so: after a seven-day shadow every observed
        message would sit claimed, so flipping to `on` would dispatch nothing
        until each lease expired. Shadow's record is its log line, not a ledger
        row.
        """
        d = _dispatcher(store, woken, mode="shadow")
        d.handle(_event())
        assert woken == []
        assert store.get("tailor/inbox/20260810T01_TAILOR_REQUEST_main_aa.json",
                         "jobflow.tailor.generate") is None


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


class TestClaimIsReleasedWhenTheWakeFails:
    """A committed claim with no wake makes the message invisible to BOTH paths.

    The reconciler skips it (it looks claimed) and no worker was ever woken, so
    the work stalls until the lease expires — now two hours.
    """

    def test_full_wake_channel_releases_the_claim(self, store, woken):
        d = _dispatcher(store, woken)
        d._waker = lambda job_id, **kw: False  # channel full

        d.handle(_event())

        key = "tailor/inbox/20260810T01_TAILOR_REQUEST_main_aa.json"
        assert store.get(key, "jobflow.tailor.generate") is None, (
            "claim must be released so the reconciler can resurface the work"
        )

    def test_unresolvable_job_releases_the_claim(self, store, woken):
        d = _dispatcher(store, woken, resolver=lambda a: None)

        d.handle(_event())

        key = "tailor/inbox/20260810T01_TAILOR_REQUEST_main_aa.json"
        assert store.get(key, "jobflow.tailor.generate") is None

    def test_successful_wake_keeps_the_claim(self, store, woken):
        _dispatcher(store, woken).handle(_event())

        key = "tailor/inbox/20260810T01_TAILOR_REQUEST_main_aa.json"
        assert store.get(key, "jobflow.tailor.generate") is not None
        assert len(woken) == 1

    def test_shadow_mode_releases_so_the_run_can_be_repeated(self, store, woken):
        """Shadow must not consume work it never actually dispatched."""
        _dispatcher(store, woken, mode="shadow").handle(_event())

        key = "tailor/inbox/20260810T01_TAILOR_REQUEST_main_aa.json"
        assert store.get(key, "jobflow.tailor.generate") is None


class _WriteTrap:
    """A ledger that permits reads and detonates on every write."""

    def __init__(self, inner):
        self._inner = inner
        self.lease_seconds = inner.lease_seconds

    def get(self, message_key, activity_id):
        return self._inner.get(message_key, activity_id)

    def claim(self, *a, **kw):
        raise AssertionError("shadow must never claim")

    def release(self, *a, **kw):
        raise AssertionError("shadow must never release")

    def complete(self, *a, **kw):
        raise AssertionError("shadow must never complete")


class TestShadowNeverTouchesTheLedger:
    """Shadow must be passive on disk, not merely tidy afterwards.

    Claiming and then releasing leaves a window: the claim is committed while
    ``_resolve_job_id`` parses a 130 KB jobs.json. A kill inside that window —
    or a release that fails, since ``_release`` swallows every exception — left
    an orphan claim behind. Shadow wakes nobody, so every orphan was guaranteed
    lost work, hidden from the reconciler for a full lease (now two hours, up
    from fifteen minutes). The fix is to never write at all.
    """

    def test_shadow_issues_no_writes_whatsoever(self, store, woken):
        """Calls ``_dispatch`` directly ON PURPOSE.

        ``handle()`` swallows exceptions, so going through it would catch the
        trap's AssertionError and let this test pass while shadow was still
        writing. The write must be able to escape for the trap to mean anything.
        """
        d = _dispatcher(_WriteTrap(store), woken, mode="shadow")

        d._dispatch(_event())

        assert woken == []

    def test_shadow_still_reports_what_it_would_have_woken(self, store, woken, caplog):
        """Passivity must not cost the observation — that IS shadow's output."""
        import logging

        d = _dispatcher(_WriteTrap(store), woken, mode="shadow")
        with caplog.at_level(logging.INFO):
            d._dispatch(_event())

        assert "would wake job-for-jobflow.tailor.generate" in caplog.text
        assert "jobflow.tailor.generate" in caplog.text

    def test_shadow_respects_a_live_claim_it_did_not_make(self, store, woken, caplog):
        """It reads the ledger; it just refuses to write to it.

        A message the real path already holds is not something shadow "would
        wake", so reporting it would overstate recall at the gate.
        """
        import logging

        key = "tailor/inbox/20260810T01_TAILOR_REQUEST_main_aa.json"
        store.claim(key, "jobflow.tailor.generate", now=0)

        d = _dispatcher(_WriteTrap(store), woken, mode="shadow", clock=lambda: 1.0)
        with caplog.at_level(logging.INFO):
            d._dispatch(_event())

        assert "would wake" not in caplog.text

    def test_shadow_resurfaces_the_work_once_the_lease_lapses(self, store, woken, caplog):
        import logging

        key = "tailor/inbox/20260810T01_TAILOR_REQUEST_main_aa.json"
        store.claim(key, "jobflow.tailor.generate", now=0)

        d = _dispatcher(_WriteTrap(store), woken, mode="shadow",
                        clock=lambda: 901.0)
        with caplog.at_level(logging.INFO):
            d._dispatch(_event())

        assert "would wake" in caplog.text

    def test_a_resolver_fault_in_shadow_is_still_contained(self, store, woken):
        def _boom(activity_id):
            raise RuntimeError("jobs.json unreadable")

        d = _dispatcher(_WriteTrap(store), woken, mode="shadow", resolver=_boom)

        d._dispatch(_event())  # must not raise, and must not reach a write

        assert woken == []
