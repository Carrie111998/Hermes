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
