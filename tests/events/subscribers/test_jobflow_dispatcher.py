"""EventBus -> scheduler activation for JobFlow workers.

The dispatcher turns an actionable mailbox message into an in-memory wake.
It never writes jobs.json, never re-enables a disabled worker, and never
dispatches the same logical work twice.

Default mode is OFF: registering the subscriber must change nothing until an
operator opts in via HERMES_JOBFLOW_EVENT_DISPATCH.
"""

from __future__ import annotations

import sqlite3

import pytest

from events.schema import Event, EventType, Priority
from events.subscribers.jobflow_dispatcher import MODE_ENV, JobFlowDispatcher
from jobflow_dispatch.store import ActivationStore


@pytest.fixture
def store(tmp_path):
    return ActivationStore(tmp_path / "dispatch.db", lease_seconds=900)


@pytest.fixture
def woken():
    return []


def _dispatcher(store, woken, *, mode="on", resolver=None, clock=None, sleep=None):
    kwargs = {"clock": clock} if clock is not None else {}
    # Real backoff would make every release-failure test sleep; the retry's
    # timing is asserted on its own below, not paid for in every other test.
    kwargs["sleep"] = sleep if sleep is not None else (lambda _seconds: None)
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
    def test_off_is_the_default_and_dispatches_nothing(self, store, woken, monkeypatch):
        # The "default" this asserts is the CODE default. resolve_mode(None)
        # falls back to os.getenv(MODE_ENV), so an operator who has opted in
        # -- profiles/main/.env carries HERMES_JOBFLOW_EVENT_DISPATCH=on, and
        # the nightly gate runs under that env -- would otherwise make this
        # test read the live deployment's mode and fail on a healthy tree.
        monkeypatch.delenv(MODE_ENV, raising=False)
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

class _ReleaseFails:
    """Claims succeed, releases do not — an orphan claim, manufactured."""

    def __init__(self, inner):
        self._inner = inner
        self.lease_seconds = inner.lease_seconds
        self.release_calls = 0

    def get(self, message_key, activity_id):
        return self._inner.get(message_key, activity_id)

    def claim(self, *a, **kw):
        return self._inner.claim(*a, **kw)

    def release(self, *a, **kw):
        self.release_calls += 1
        raise sqlite3.OperationalError("database is locked")


class _ReleaseFailsThenRecovers(_ReleaseFails):
    """The contention case: SQLITE_BUSY for a while, then the writer lets go.

    This is the failure the retry exists for. ``_ReleaseFails`` models the
    permanent fault; this one models the transient one, which is the far more
    likely of the two given ``PRAGMA busy_timeout=5000``.
    """

    def __init__(self, inner, failures):
        super().__init__(inner)
        self._failures = failures

    def release(self, *a, **kw):
        self.release_calls += 1
        if self.release_calls <= self._failures:
            raise sqlite3.OperationalError("database is locked")
        return self._inner.release(*a, **kw)


class TestOrphanClaimIsLoud:
    """A failed release is the one dispatcher fault that costs work, not latency.

    Everywhere else the reconciler is the safety net, so a dropped wake is just
    a delay. Not here: the claim committed, so the reconciler SKIPS the message,
    and no worker was woken. It stays invisible for a full lease. Swallowing the
    exception is right for loop stability; swallowing it quietly is not.
    """

    def test_the_failure_is_greppable_by_a_stable_marker(self, store, woken, caplog):
        import logging

        d = _dispatcher(_ReleaseFails(store), woken, resolver=lambda a: None)
        with caplog.at_level(logging.ERROR):
            d.handle(_event())

        assert JobFlowDispatcher.ORPHAN_MARKER in caplog.text

    def test_it_names_the_work_and_the_cost(self, store, woken, caplog):
        """Which message, which activity, and how long it is invisible."""
        import logging

        d = _dispatcher(_ReleaseFails(store), woken, resolver=lambda a: None)
        with caplog.at_level(logging.ERROR):
            d.handle(_event())

        assert "tailor/inbox/20260810T01_TAILOR_REQUEST_main_aa.json" in caplog.text
        assert "jobflow.tailor.generate" in caplog.text
        assert "900" in caplog.text, "the lease is how long the work stays hidden"

    def test_it_is_logged_at_error_not_warning(self, store, woken, caplog):
        """Warning is for a wake that was merely refused; this one loses work."""
        import logging

        d = _dispatcher(_ReleaseFails(store), woken, resolver=lambda a: None)
        with caplog.at_level(logging.DEBUG):
            d.handle(_event())

        orphan = [r for r in caplog.records
                  if JobFlowDispatcher.ORPHAN_MARKER in r.getMessage()]
        assert orphan and all(r.levelno >= logging.ERROR for r in orphan)

    def test_a_release_fault_still_never_stalls_the_loop(self, store, woken):
        """Loudness must not come at the cost of the fail-closed guarantee."""
        d = _dispatcher(_ReleaseFails(store), woken, resolver=lambda a: None)

        d._dispatch(_event())  # must not raise

        assert woken == []

    def test_a_successful_release_says_nothing(self, store, woken, caplog):
        """No marker on the happy path, or grepping for it finds noise."""
        import logging

        d = _dispatcher(store, woken, resolver=lambda a: None)
        with caplog.at_level(logging.DEBUG):
            d.handle(_event())

        assert JobFlowDispatcher.ORPHAN_MARKER not in caplog.text

    def test_the_marker_is_only_reached_after_the_retries(self, store, woken, caplog):
        """Loudness is the LAST resort, not the first response."""
        import logging

        ledger = _ReleaseFails(store)
        d = _dispatcher(ledger, woken, resolver=lambda a: None)
        with caplog.at_level(logging.ERROR):
            d.handle(_event())

        assert ledger.release_calls == JobFlowDispatcher.RELEASE_ATTEMPTS
        assert JobFlowDispatcher.ORPHAN_MARKER in caplog.text


class TestReleaseRetriesBeforeConcedingAnOrphan:
    """Prevent the orphan, don't just report it.

    P4 made a failed release greppable. That makes an orphan findable after the
    fact; it does not make one rarer. This is the one dispatcher fault that costs
    WORK rather than latency — the reconciler skips the message because it looks
    claimed, and no worker was ever woken, so it stays invisible to BOTH paths
    for a full lease (7200s) against a reconciler that runs every 6h.

    The plausible cause is transient: SQLITE_BUSY past the store's
    ``busy_timeout=5000`` under contention. A short bounded backoff usually
    clears it, which turns most would-be orphans into a few hundred milliseconds
    of latency — the cheap failure this subsystem is designed around.
    """

    KEY = "tailor/inbox/20260810T01_TAILOR_REQUEST_main_aa.json"
    ACTIVITY = "jobflow.tailor.generate"

    def test_a_release_that_recovers_on_the_last_attempt_leaves_no_orphan(
        self, store, woken, caplog
    ):
        """The whole point: contention that clears must not strand the work."""
        import logging

        failures = JobFlowDispatcher.RELEASE_ATTEMPTS - 1
        assert failures >= 1, (
            "a budget of one attempt is not a retry — this test would pass "
            "vacuously, and so would the orphan-prevention it stands for"
        )
        ledger = _ReleaseFailsThenRecovers(store, failures=failures)
        d = _dispatcher(ledger, woken, resolver=lambda a: None)

        with caplog.at_level(logging.DEBUG):
            d.handle(_event())

        assert ledger.release_calls == JobFlowDispatcher.RELEASE_ATTEMPTS
        assert store.get(self.KEY, self.ACTIVITY) is None, (
            "the claim was handed back, so the reconciler can resurface the work"
        )
        assert JobFlowDispatcher.ORPHAN_MARKER not in caplog.text, (
            "a recovered release is not an orphan; marking it would make the "
            "marker useless for finding real ones"
        )

    def test_a_release_that_recovers_immediately_stops_retrying(self, store, woken):
        """One transient failure costs one retry, not the full budget."""
        ledger = _ReleaseFailsThenRecovers(store, failures=1)
        d = _dispatcher(ledger, woken, resolver=lambda a: None)

        d.handle(_event())

        assert ledger.release_calls == 2
        assert store.get(self.KEY, self.ACTIVITY) is None

    def test_the_retry_budget_is_bounded(self, store, woken):
        """Retrying must not become a way to stall the subscriber loop.

        Each attempt can already cost a full ``busy_timeout`` inside SQLite, so
        the bound that matters is the ATTEMPT count; the backoff is deliberately
        a small fraction of it. Asserted together because a future edit that
        raises either in isolation changes the worst-case loop stall.
        """
        slept = []
        ledger = _ReleaseFails(store)
        d = _dispatcher(ledger, woken, resolver=lambda a: None,
                        sleep=slept.append)

        d._dispatch(_event())

        assert ledger.release_calls == JobFlowDispatcher.RELEASE_ATTEMPTS
        assert len(slept) == JobFlowDispatcher.RELEASE_ATTEMPTS - 1, (
            "one backoff between attempts, and none after the last failure"
        )
        assert sum(slept) <= 1.0, (
            f"worst-case added delay per release is {sum(slept)}s; the retry "
            "budget must stay small next to the 5s busy_timeout it sits behind"
        )

    def test_there_is_a_backoff_for_every_retry(self):
        """An off-by-one here would raise IndexError inside a fault handler."""
        assert (
            len(JobFlowDispatcher.RELEASE_BACKOFF_SECONDS)
            >= JobFlowDispatcher.RELEASE_ATTEMPTS - 1
        )

    def test_a_budget_longer_than_the_backoff_still_reaches_the_marker(
        self, store, woken, caplog, monkeypatch
    ):
        """The degrade path for the mis-edit the previous test forbids.

        Indexing the backoff blind would raise IndexError from inside the fault
        handler. That escapes ``_release`` and skips the ORPHAN_MARKER log — so
        a botched constant bump would silently remove the only trace a stranded
        claim leaves, which is worse than the mis-edit itself.
        """
        import logging

        monkeypatch.setattr(
            JobFlowDispatcher,
            "RELEASE_ATTEMPTS",
            len(JobFlowDispatcher.RELEASE_BACKOFF_SECONDS) + 3,
        )
        ledger = _ReleaseFails(store)
        d = _dispatcher(ledger, woken, resolver=lambda a: None)

        with caplog.at_level(logging.ERROR):
            d._dispatch(_event())  # directly: an IndexError must be able to escape

        assert ledger.release_calls == JobFlowDispatcher.RELEASE_ATTEMPTS
        assert JobFlowDispatcher.ORPHAN_MARKER in caplog.text

    def test_the_backoff_grows(self):
        """Re-issuing at a fixed interval loses the same race repeatedly."""
        backoff = JobFlowDispatcher.RELEASE_BACKOFF_SECONDS
        assert all(b > 0 for b in backoff)
        assert list(backoff) == sorted(backoff)

    def test_exhausted_retries_still_never_stall_the_loop(self, store, woken):
        """Calls ``_dispatch`` directly ON PURPOSE.

        ``handle()`` swallows exceptions, so routing through it would let this
        pass even if the retry loop re-raised on exhaustion. The final swallow
        has to be the dispatcher's own, and the failure has to be able to escape
        for this assertion to mean anything.
        """
        d = _dispatcher(_ReleaseFails(store), woken, resolver=lambda a: None)

        d._dispatch(_event())  # must not raise

        assert woken == []

    def test_a_recovered_release_is_reported_without_the_marker(
        self, store, woken, caplog
    ):
        """Contention that clears is still worth seeing — it precedes the real ones."""
        import logging

        d = _dispatcher(_ReleaseFailsThenRecovers(store, failures=1), woken,
                        resolver=lambda a: None)
        with caplog.at_level(logging.WARNING):
            d.handle(_event())

        assert "succeeded on attempt 2" in caplog.text
        assert JobFlowDispatcher.ORPHAN_MARKER not in caplog.text

    def test_the_wake_refusal_path_retries_too(self, store, woken):
        """Both release call sites go through ``_release``, not just the resolver one.

        The wake-channel refusal is the likelier release in production: it fires
        under load, which is exactly when the ledger is contended.
        """
        ledger = _ReleaseFailsThenRecovers(store, failures=1)
        d = _dispatcher(ledger, woken)
        d._waker = lambda job_id, **kw: False  # channel full

        d._dispatch(_event())

        assert ledger.release_calls == 2
        assert store.get(self.KEY, self.ACTIVITY) is None
