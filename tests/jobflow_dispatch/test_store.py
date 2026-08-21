"""Durable activation ledger.

EventBus delivery is at-least-once, so the same mailbox message will be seen
more than once. The ledger is what makes activation idempotent: one claim per
``(message_key, activity_id)``.

Two failure modes it must distinguish, which a naive "have I seen this?" check
conflates:

* **interrupted** — claimed, then the process died before completing. That work
  never happened and must become reclaimable once the lease expires.
* **completed** — the work ran. It must NEVER be reclaimable, however old.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from jobflow_dispatch.store import ActivationStore

LEASE = 900


@pytest.fixture
def store(tmp_path):
    return ActivationStore(tmp_path / "dispatch.db", lease_seconds=LEASE)


class TestClaimIdempotence:
    def test_first_claim_wins_duplicate_loses(self, store):
        assert store.claim("m1", "cron.jobflow.matcher", now=1000) is True
        assert store.claim("m1", "cron.jobflow.matcher", now=1100) is False

    def test_different_activities_claim_the_same_message(self, store):
        assert store.claim("m1", "cron.jobflow.matcher", now=1000) is True
        assert store.claim("m1", "jobflow.tailor.generate", now=1000) is True

    def test_different_messages_are_independent(self, store):
        assert store.claim("m1", "cron.jobflow.matcher", now=1000) is True
        assert store.claim("m2", "cron.jobflow.matcher", now=1000) is True


class TestLeaseRecovery:
    def test_interrupted_claim_is_reclaimable_after_the_lease(self, store):
        assert store.claim("m1", "cron.jobflow.matcher", now=1000) is True
        assert store.claim("m1", "cron.jobflow.matcher", now=1000 + LEASE) is False
        assert store.claim("m1", "cron.jobflow.matcher", now=1001 + LEASE) is True

    def test_completed_work_is_never_reclaimable(self, store):
        """The distinction a 'seen it?' check would get wrong."""
        assert store.claim("m1", "cron.jobflow.matcher", now=1000) is True
        store.complete("m1", "cron.jobflow.matcher", outcome="succeeded", now=1010)

        assert store.claim("m1", "cron.jobflow.matcher", now=10_000_000) is False

    def test_reclaim_refreshes_the_lease(self, store):
        store.claim("m1", "cron.jobflow.matcher", now=1000)
        store.claim("m1", "cron.jobflow.matcher", now=1001 + LEASE)
        # The reclaim reset the clock, so the original expiry no longer applies.
        assert store.claim("m1", "cron.jobflow.matcher", now=1002 + LEASE) is False


class TestCompletion:
    def test_complete_records_outcome_and_clears_pending(self, store):
        store.claim("m1", "cron.jobflow.matcher", now=1000, correlation_id="c1")
        assert [r.message_key for r in store.pending("cron.jobflow.matcher")] == ["m1"]

        store.complete("m1", "cron.jobflow.matcher", outcome="succeeded", now=1010)

        assert store.pending("cron.jobflow.matcher") == []
        row = store.get("m1", "cron.jobflow.matcher")
        assert row.state == "completed"
        assert row.outcome == "succeeded"
        assert row.correlation_id == "c1"

    def test_completing_an_unclaimed_message_is_rejected(self, store):
        with pytest.raises(KeyError, match="missing"):
            store.complete("nope", "cron.jobflow.matcher", outcome="succeeded", now=1)

    def test_pending_is_scoped_to_the_activity(self, store):
        store.claim("m1", "cron.jobflow.matcher", now=1000)
        store.claim("m2", "jobflow.tailor.generate", now=1000)
        assert [r.message_key for r in store.pending("cron.jobflow.matcher")] == ["m1"]

    def test_claim_census_returns_every_durable_claim_without_a_limit(self, store):
        for index in range(525):
            store.claim(
                f"message-{index:03d}",
                "cron.jobflow.matcher" if index % 2 else "jobflow.tailor.generate",
                now=1000 + index,
                correlation_id=f"correlation-{index:03d}",
            )
        store.complete(
            "message-001", "cron.jobflow.matcher", outcome="succeeded", now=2000
        )

        rows = store.claim_census()

        assert len(rows) == 524
        assert all(row.state == "claimed" for row in rows)
        assert rows == sorted(rows, key=lambda row: (row.activity_id, row.message_key))
        assert {row.message_key for row in rows} == {
            f"message-{index:03d}" for index in range(525)
        } - {"message-001"}

    def test_claim_census_query_failure_raises_instead_of_returning_partial(self, store, monkeypatch):
        class Failing:
            def execute(self, *_args, **_kwargs):
                raise sqlite3.OperationalError("injected census failure")

        monkeypatch.setattr(store._local, "conn", Failing())
        with pytest.raises(sqlite3.OperationalError, match="census failure"):
            store.claim_census()


class TestValidation:
    @pytest.mark.parametrize("bad", ("", "   ", None))
    def test_blank_identity_is_rejected(self, store, bad):
        with pytest.raises(ValueError):
            store.claim(bad, "cron.jobflow.matcher", now=1000)
        with pytest.raises(ValueError):
            store.claim("m1", bad, now=1000)

    def test_non_numeric_now_is_rejected(self, store):
        with pytest.raises(ValueError, match="now"):
            store.claim("m1", "cron.jobflow.matcher", now="soon")


class TestConcurrency:
    def test_exactly_one_claimer_wins_across_connections(self, tmp_path):
        path = tmp_path / "dispatch.db"
        stores = [ActivationStore(path, lease_seconds=LEASE) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(
                lambda s: s.claim("m1", "cron.jobflow.matcher", now=1000), stores
            ))
        assert sum(1 for r in results if r) == 1, results


class TestDurability:
    def test_state_survives_reopen(self, tmp_path):
        path = tmp_path / "dispatch.db"
        first = ActivationStore(path, lease_seconds=LEASE)
        first.claim("m1", "cron.jobflow.matcher", now=1000)
        first.complete("m1", "cron.jobflow.matcher", outcome="no_work", now=1010)

        reopened = ActivationStore(path, lease_seconds=LEASE)
        assert reopened.get("m1", "cron.jobflow.matcher").outcome == "no_work"
        assert reopened.claim("m1", "cron.jobflow.matcher", now=10_000_000) is False

    def test_failed_write_rolls_back_and_store_stays_usable(self, store, monkeypatch):
        original = store._get_conn()

        class Failing:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("injected")

            def rollback(self):
                original.rollback()

        monkeypatch.setattr(store._local, "conn", Failing())
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            store.claim("m1", "cron.jobflow.matcher", now=1000)

        monkeypatch.setattr(store._local, "conn", original)
        assert store.claim("m1", "cron.jobflow.matcher", now=1000) is True


class TestSharedLedgerPath:
    def test_dispatcher_and_reconciler_agree_on_one_ledger(self):
        """Divergence here means duplicate model calls with no error anywhere.

        The subscriber claims into its ledger; the reconciler checks the same
        one to decide what was missed. Two paths = the reconciler re-dispatches
        everything the subscriber already took.
        """
        import importlib.util
        import pathlib
        import sys

        from jobflow_dispatch.store import default_ledger_path

        wrapper_path = (
            pathlib.Path.home() / ".hermes" / "profiles" / "main" / "scripts"
            / "jobflow_reconcile.py"
        )
        if not wrapper_path.is_file():
            pytest.skip("reconcile wrapper not present")

        spec = importlib.util.spec_from_file_location("jobflow_reconcile_probe", wrapper_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        try:
            spec.loader.exec_module(mod)
            assert mod.LEDGER_PATH == default_ledger_path()
        finally:
            sys.modules.pop(spec.name, None)

    def test_ledger_lives_under_the_canonical_root_not_a_profile(self):
        from jobflow_dispatch.store import default_ledger_path

        path = default_ledger_path()
        assert path.name == "jobflow_dispatch.db"
        assert path.parent.name == "telemetry"
        assert "profiles" not in path.parts


class TestAtomicWakeOutbox:
    def test_claim_for_wake_commits_claim_and_outbox_together(self, store):
        outbox = store.claim_for_wake(
            "tailor/inbox/m1.json",
            "jobflow.tailor.generate",
            job_id="job-1",
            caller="jobflow-dispatcher",
            reason="mailbox_message",
            now=10,
            correlation_id="c1",
        )

        assert outbox is not None
        assert store.get(outbox.message_key, outbox.activity_id).state == "claimed"
        assert store.pending_wake_outbox() == [outbox]

    def test_release_cascades_the_same_claims_pending_outbox(self, store):
        outbox = store.claim_for_wake(
            "tailor/inbox/m1.json",
            "jobflow.tailor.generate",
            job_id="job-1",
            caller="jobflow-dispatcher",
            now=10,
        )
        assert outbox is not None

        store.release(outbox.message_key, outbox.activity_id)

        assert store.get(outbox.message_key, outbox.activity_id) is None
        assert store.pending_wake_outbox() == []

    def test_completion_removes_a_pending_outbox(self, store):
        outbox = store.claim_for_wake(
            "tailor/inbox/m1.json",
            "jobflow.tailor.generate",
            job_id="job-1",
            caller="jobflow-dispatcher",
            now=10,
        )
        assert outbox is not None

        store.complete(
            outbox.message_key,
            outbox.activity_id,
            outcome="success",
            now=11,
        )

        assert store.pending_wake_outbox() == []


class TestLeaseOutlivesRealRuns:
    def test_default_lease_exceeds_the_cron_wall_clock_ceiling(self):
        """A lease shorter than a legitimate run causes duplicate model calls.

        Nothing calls complete() in production yet, so lease expiry is the ONLY
        thing that releases a claim. If a woken worker can legitimately run
        longer than the lease, redelivery or reconciliation re-claims the same
        message mid-run and wakes it a second time.
        """
        from jobflow_dispatch.store import CRON_WALL_CLOCK_CEILING_SECONDS, DEFAULT_LEASE_SECONDS

        assert DEFAULT_LEASE_SECONDS > CRON_WALL_CLOCK_CEILING_SECONDS

    def test_release_returns_a_claim_for_immediate_retry(self):
        """Used when the wake could not be delivered after the claim committed."""
        import tempfile, pathlib
        from jobflow_dispatch.store import ActivationStore

        store = ActivationStore(pathlib.Path(tempfile.mkdtemp()) / "d.db", lease_seconds=900)
        assert store.claim("m1", "a1", now=1000) is True
        store.release("m1", "a1")
        assert store.claim("m1", "a1", now=1001) is True, "release must not wait for the lease"

    def test_release_never_resurrects_completed_work(self):
        import tempfile, pathlib
        from jobflow_dispatch.store import ActivationStore

        store = ActivationStore(pathlib.Path(tempfile.mkdtemp()) / "d.db", lease_seconds=900)
        store.claim("m1", "a1", now=1000)
        store.complete("m1", "a1", outcome="succeeded", now=1010)
        store.release("m1", "a1")
        assert store.claim("m1", "a1", now=2000) is False

    def test_release_of_unknown_row_is_a_noop(self):
        import tempfile, pathlib
        from jobflow_dispatch.store import ActivationStore

        store = ActivationStore(pathlib.Path(tempfile.mkdtemp()) / "d.db", lease_seconds=900)
        store.release("nope", "a1")


class TestLeaseFitsInsideTheRecoveryWindow:
    """The lease is a blind spot in the safety net, not just a crash timer.

    A claimed row is invisible to ``scan_actionable`` until its lease lapses.
    So the lease is bounded on BOTH sides and the existing
    ``TestLeaseOutlivesRealRuns`` only pins the lower one — which is exactly how
    the 900 -> 7200 bump landed without anyone re-deriving what it did to the
    6-hourly reconciler. These are the missing upper bounds.
    """

    def test_reconciler_period_matches_the_scheduled_cron(self):
        """Pinned to cron ``jobflow-reconcile`` (64711e6d8334, `30 0,6,12,18`).

        If that schedule is ever widened, this constant must move with it or the
        headroom assertion below silently starts checking a fiction.
        """
        from jobflow_dispatch.store import RECONCILER_PERIOD_SECONDS

        assert RECONCILER_PERIOD_SECONDS == 6 * 3600

    def test_default_lease_closes_well_before_the_reconciler_returns(self):
        """A lease that outlives the reconciler window blinds the safety net.

        Nothing calls complete() in production, so lease expiry is the only
        thing that makes a claim visible again. If the lease ever reaches the
        reconciler's period, a claim can span an entire window and genuinely
        stranded work waits for the tick after next. The 2x margin is
        deliberate: at exactly 1x the two clocks merely have to drift to
        reproduce the bug.

        Today: 7200 * 2 = 14400 <= 21600, so this passes as written. It is a
        tripwire for the NEXT bump, not a description of a current defect.
        """
        from jobflow_dispatch.store import (
            DEFAULT_LEASE_SECONDS,
            RECONCILER_PERIOD_SECONDS,
        )

        assert DEFAULT_LEASE_SECONDS * 2 <= RECONCILER_PERIOD_SECONDS, (
            f"lease {DEFAULT_LEASE_SECONDS}s leaves too little headroom under a "
            f"{RECONCILER_PERIOD_SECONDS}s reconciler window — a claim would hide "
            "genuinely stranded work across a full recovery cycle"
        )


class TestIsAvailableIsTheOnlyPredicate:
    """Both recovery paths must reach the same verdict from the same code.

    The reconciler decides what to resurface with this; shadow decides what it
    would have woken with it. Two copies would eventually disagree, and the
    disagreement would look like a dispatcher fault.
    """

    def test_unknown_work_is_available(self, store):
        from jobflow_dispatch.store import is_available

        assert is_available(store, "m1", "a1", 1000) is True

    def test_live_claim_is_not_available(self, store):
        from jobflow_dispatch.store import is_available

        store.claim("m1", "a1", now=1000)
        assert is_available(store, "m1", "a1", 1000 + LEASE) is False

    def test_expired_claim_is_available_again(self, store):
        from jobflow_dispatch.store import is_available

        store.claim("m1", "a1", now=1000)
        assert is_available(store, "m1", "a1", 1000 + LEASE + 1) is True

    def test_completed_work_is_never_available(self, store):
        from jobflow_dispatch.store import is_available

        store.claim("m1", "a1", now=1000)
        store.complete("m1", "a1", outcome="succeeded", now=1010)
        assert is_available(store, "m1", "a1", 10_000_000) is False

    def test_reconcile_imports_it_rather_than_reimplementing(self):
        """Guards against someone quietly restoring a second copy."""
        from jobflow_dispatch import reconcile
        from jobflow_dispatch.store import is_available

        assert reconcile.is_available is is_available
        assert not hasattr(reconcile, "_is_available")

class TestLeaseIsValidatedAtConstruction:
    """The lease was the one unvalidated input in a module that checks everything.

    A bad value does not fail loudly here — it fails as a wrong comparison
    inside claim(), which reads as "dedupe is behaving oddly" rather than as a
    config error. And an oversized one silently blinds the reconciler, which is
    the failure this whole guard exists to prevent.
    """

    def test_the_shipped_default_is_accepted(self, tmp_path):
        from jobflow_dispatch.store import DEFAULT_LEASE_SECONDS

        store = ActivationStore(tmp_path / "d.db")
        assert store.lease_seconds == DEFAULT_LEASE_SECONDS

    @pytest.mark.parametrize("bad", [0, -1, -900])
    def test_non_positive_is_rejected(self, tmp_path, bad):
        with pytest.raises(ValueError, match="lease_seconds"):
            ActivationStore(tmp_path / "d.db", lease_seconds=bad)

    @pytest.mark.parametrize("bad", ["900", None, True, False])
    def test_non_numeric_is_rejected(self, tmp_path, bad):
        """True would otherwise pass as 1 — a one-second lease on every claim."""
        with pytest.raises(ValueError, match="lease_seconds"):
            ActivationStore(tmp_path / "d.db", lease_seconds=bad)

    def test_a_lease_that_can_span_a_recovery_window_is_rejected(self, tmp_path):
        from jobflow_dispatch.store import RECONCILER_PERIOD_SECONDS

        with pytest.raises(ValueError, match="reconciler"):
            ActivationStore(tmp_path / "d.db",
                            lease_seconds=RECONCILER_PERIOD_SECONDS)

    def test_the_runtime_guard_is_1x_while_the_default_policy_is_2x(self, tmp_path):
        """Deliberate mismatch — do not "fix" it by tightening this to 2x.

        The runtime guard rejects only the PROVABLY broken: a lease that can
        hide work across a full reconciler window. The 2x margin in
        TestLeaseFitsInsideTheRecoveryWindow is a policy on the SHIPPED DEFAULT,
        which is a stricter question than what any ad-hoc store may be built
        with. A near-boundary value must stay constructible so tests can
        exercise the boundary.
        """
        from jobflow_dispatch.store import RECONCILER_PERIOD_SECONDS

        store = ActivationStore(tmp_path / "d.db",
                                lease_seconds=RECONCILER_PERIOD_SECONDS - 1)
        assert store.lease_seconds == RECONCILER_PERIOD_SECONDS - 1

    def test_a_float_lease_is_accepted(self, tmp_path):
        """Only bool is special-cased; the value is used in numeric comparison."""
        store = ActivationStore(tmp_path / "d.db", lease_seconds=900.5)
        assert store.lease_seconds == 900.5
