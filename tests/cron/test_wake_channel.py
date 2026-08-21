"""Durable wake queue between the event dispatcher and the scheduler.

Activation deliberately does NOT go through ``trigger_job``: that writes
jobs.json on every activation (contending with the scheduler's own ~1/min
rewrite) and sets ``enabled: True``, which would silently revive a worker an
operator had disabled. The queue instead uses the canonical cross-profile
quarantine-control database and is drained transactionally by ``tick()``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from cron import wake_channel


@pytest.fixture(autouse=True)
def _clean():
    wake_channel.clear_wakes()
    yield
    wake_channel.clear_wakes()


class TestRequestAndDrain:
    def test_request_then_drain_yields_the_job(self):
        assert wake_channel.request_wake("j1", caller="test", reason="score_request") is True
        assert wake_channel.drain_wakes() == {"j1"}

    def test_drain_is_exhaustive(self):
        wake_channel.request_wake("j1", caller="test", reason="r")
        wake_channel.drain_wakes()
        assert wake_channel.drain_wakes() == set()

    def test_duplicate_requests_collapse(self):
        assert wake_channel.request_wake("j1", caller="test", reason="r") is True
        assert wake_channel.request_wake("j1", caller="test", reason="r") is False
        assert wake_channel.drain_wakes() == {"j1"}

    def test_distinct_jobs_accumulate(self):
        wake_channel.request_wake("j1", caller="test", reason="r")
        wake_channel.request_wake("j2", caller="test", reason="r")
        assert wake_channel.drain_wakes() == {"j1", "j2"}

    def test_pending_is_observable_without_consuming(self):
        wake_channel.request_wake("j1", caller="test", reason="r")
        assert wake_channel.pending_wakes() == frozenset({"j1"})
        assert wake_channel.drain_wakes() == {"j1"}


class TestValidation:
    @pytest.mark.parametrize("bad", ("", "   ", None, 7))
    def test_blank_job_id_is_rejected(self, bad):
        with pytest.raises(ValueError, match="job_id"):
            wake_channel.request_wake(bad, caller="test", reason="r")

    def test_caller_is_required(self):
        """Anonymous wakes make postmortem attribution impossible."""
        with pytest.raises(ValueError, match="caller"):
            wake_channel.request_wake("j1", caller="  ", reason="r")


class TestBounded:
    def test_channel_is_capped_and_drops_loudly(self):
        """A runaway producer must not grow the channel without bound."""
        from jobflow_dispatch.quarantine_control import WakeQueueFullError

        cap = wake_channel.MAX_PENDING
        for i in range(cap):
            assert wake_channel.request_wake(f"j{i}", caller="test", reason="r") is True
        with pytest.raises(WakeQueueFullError, match="capacity"):
            wake_channel.request_wake("overflow", caller="test", reason="r")
        assert "overflow" not in wake_channel.pending_wakes()


class TestConcurrency:
    def test_concurrent_requests_and_drain_lose_nothing(self):
        def _add(i):
            wake_channel.request_wake(f"j{i}", caller="test", reason="r")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(_add, range(200)))
        assert len(wake_channel.drain_wakes()) == 200

    def test_only_one_drainer_gets_each_job(self):
        for i in range(100):
            wake_channel.request_wake(f"j{i}", caller="test", reason="r")
        with ThreadPoolExecutor(max_workers=4) as pool:
            batches = list(pool.map(lambda _: wake_channel.drain_wakes(), range(4)))
        seen = [j for b in batches for j in b]
        assert len(seen) == len(set(seen)) == 100


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------


class TestSchedulerIntegration:
    """`_collect_woken_jobs` is what tick() uses to turn wakes into work."""

    def _jobs(self, monkeypatch, rows):
        from cron import scheduler
        monkeypatch.setattr(scheduler, "load_jobs", lambda: rows)
        return scheduler

    def test_woken_enabled_job_is_returned_without_consuming_before_submit(
        self, monkeypatch
    ):
        s = self._jobs(monkeypatch, [{"id": "j1", "name": "a", "enabled": True}])
        wake_channel.request_wake("j1", caller="test", reason="r")

        got = s._collect_woken_jobs(exclude_ids=set())

        assert [j["id"] for j in got] == ["j1"]
        assert wake_channel.pending_wakes() == frozenset({"j1"})
        assert got[0]["_durable_wake"]["job_id"] == "j1"

    def test_jobs_load_failure_retains_every_wake(self, monkeypatch):
        from cron import scheduler

        monkeypatch.setattr(
            scheduler,
            "load_jobs",
            lambda: (_ for _ in ()).throw(OSError("jobs.json unreadable")),
        )
        wake_channel.request_wake("j1", caller="test", reason="r")

        assert scheduler._collect_woken_jobs(exclude_ids=set()) == []
        assert wake_channel.pending_wakes() == frozenset({"j1"})

    def test_exact_wake_ack_does_not_delete_a_replacement(self):
        wake_channel.request_wake("j1", caller="test", reason="first")
        first = wake_channel.peek_wakes()[0]
        wake_channel.ack_wake(first)
        wake_channel.request_wake("j1", caller="test", reason="second")

        assert wake_channel.ack_wake(first) is False
        assert wake_channel.pending_wakes() == frozenset({"j1"})

    def test_disabled_job_is_never_revived(self, monkeypatch):
        """trigger_job would have set enabled=True; this must not."""
        s = self._jobs(monkeypatch, [{"id": "j1", "name": "a", "enabled": False}])
        wake_channel.request_wake("j1", caller="test", reason="r")

        assert s._collect_woken_jobs(exclude_ids=set()) == []

    def test_already_due_job_is_not_run_twice(self, monkeypatch):
        s = self._jobs(monkeypatch, [{"id": "j1", "name": "a", "enabled": True}])
        wake_channel.request_wake("j1", caller="test", reason="r")

        assert s._collect_woken_jobs(exclude_ids={"j1"}) == []

    def test_unknown_job_id_is_dropped(self, monkeypatch):
        s = self._jobs(monkeypatch, [{"id": "other", "name": "a", "enabled": True}])
        wake_channel.request_wake("ghost", caller="test", reason="r")

        assert s._collect_woken_jobs(exclude_ids=set()) == []

    def test_wakes_are_consumed_even_when_unusable(self, monkeypatch):
        """A disabled job's wake must not be redelivered forever."""
        s = self._jobs(monkeypatch, [{"id": "j1", "name": "a", "enabled": False}])
        wake_channel.request_wake("j1", caller="test", reason="r")

        s._collect_woken_jobs(exclude_ids=set())

        assert wake_channel.pending_wakes() == frozenset()

    def test_submit_failure_retains_wake_for_retry(self, monkeypatch):
        from cron import scheduler

        job = {"id": "j1", "name": "a", "enabled": True}
        monkeypatch.setattr(scheduler, "get_due_and_skipped_jobs", lambda: ([], []))
        monkeypatch.setattr(scheduler, "load_jobs", lambda: [job])
        monkeypatch.setattr(scheduler, "create_execution", lambda *_a, **_k: {"id": "e1"})
        monkeypatch.setattr(scheduler, "finish_execution", lambda *_a, **_k: None)
        monkeypatch.setattr(scheduler, "_interpreter_shutting_down", lambda *_a: False)

        class _Pool:
            def submit(self, _callable):
                raise RuntimeError("executor unavailable")

        monkeypatch.setattr(scheduler, "_get_parallel_pool", lambda _limit: _Pool())
        wake_channel.request_wake("j1", caller="test", reason="r")

        assert scheduler.tick(verbose=False, sync=False) == 0
        assert wake_channel.pending_wakes() == frozenset({"j1"})

    def test_successful_submit_acknowledges_wake_before_admission_release(self, monkeypatch):
        from cron import scheduler

        calls = []
        job = {"id": "j1", "name": "a", "enabled": True}
        monkeypatch.setattr(scheduler, "get_due_and_skipped_jobs", lambda: ([], []))
        monkeypatch.setattr(scheduler, "load_jobs", lambda: [job])
        monkeypatch.setattr(scheduler, "create_execution", lambda *_a, **_k: {"id": "e1"})

        class _Future:
            def add_done_callback(self, _callback):
                return None

            def exception(self):
                return None

        class _Pool:
            def submit(self, _callable):
                calls.append("submit")
                return _Future()

        class _Store:
            def dispatch_section(self, *, boundary):
                class _Admission:
                    def __enter__(self):
                        calls.append("enter")

                    def __exit__(self, *_args):
                        assert wake_channel.pending_wakes() == frozenset()
                        calls.append("exit")
                return _Admission()

        monkeypatch.setattr(scheduler, "default_control_store", lambda: _Store())
        monkeypatch.setattr(scheduler, "_get_parallel_pool", lambda _limit: _Pool())
        monkeypatch.setattr(scheduler, "_running_job_ids", set())
        wake_channel.request_wake("j1", caller="test", reason="r")

        assert scheduler.tick(verbose=False, sync=False) == 1
        assert calls.index("submit") < calls.index("exit")

    def test_collection_never_raises_into_the_tick(self, monkeypatch):
        from cron import scheduler

        def _boom():
            raise OSError("jobs.json unreadable")

        monkeypatch.setattr(scheduler, "load_jobs", _boom)
        wake_channel.request_wake("j1", caller="test", reason="r")

        assert scheduler._collect_woken_jobs(exclude_ids=set()) == []

    def test_woken_job_schedule_is_not_advanced(self, monkeypatch):
        """An event must not shift the job's regular cadence."""
        from cron import scheduler

        advanced = []
        monkeypatch.setattr(scheduler, "load_jobs",
                            lambda: [{"id": "j1", "name": "a", "enabled": True}])
        monkeypatch.setattr(scheduler, "advance_next_run", lambda jid: advanced.append(jid))

        scheduler._collect_woken_jobs(exclude_ids=set())

        assert advanced == []


class TestTickExecutesWokenJobs:
    """The wiring inside tick() itself, not just the collector.

    Removing `due_jobs = due_jobs + woken_jobs` from tick() left every
    collector test green — the helper was covered, the seam was not.
    """

    def test_tick_runs_a_job_that_only_an_event_asked_for(self, monkeypatch, tmp_path):
        from cron import scheduler

        job = {"id": "j1", "name": "woken-job", "enabled": True, "no_agent": True,
               "script": "x.py"}
        ran: list[str] = []

        monkeypatch.setattr(scheduler, "get_due_and_skipped_jobs", lambda: ([], []))
        monkeypatch.setattr(scheduler, "load_jobs", lambda: [job])
        monkeypatch.setattr(scheduler, "run_job",
                            lambda j, **k: (ran.append(j["id"]), (True, "", "", None))[1])
        monkeypatch.setattr(scheduler, "mark_job_run", lambda *a, **k: None)
        monkeypatch.setattr(scheduler, "save_job_output", lambda *a, **k: None)
        monkeypatch.setattr(scheduler, "advance_next_run", lambda *a, **k: None)
        monkeypatch.setattr(scheduler, "_deliver_result", lambda *a, **k: (True, None))
        monkeypatch.setattr(scheduler, "_running_job_ids", set())

        wake_channel.request_wake("j1", caller="test", reason="score_request")
        scheduler.tick(verbose=False, sync=True)

        assert ran == ["j1"], "tick did not execute the event-woken job"

    def test_tick_with_nothing_woken_and_nothing_due_runs_nothing(self, monkeypatch):
        from cron import scheduler

        ran: list[str] = []
        monkeypatch.setattr(scheduler, "get_due_and_skipped_jobs", lambda: ([], []))
        monkeypatch.setattr(scheduler, "load_jobs", lambda: [])
        monkeypatch.setattr(scheduler, "run_job",
                            lambda j, **k: (ran.append(j["id"]), (True, "", "", None))[1])

        scheduler.tick(verbose=False, sync=True)

        assert ran == []


class TestWakeSurvivesAConcurrentRun:
    """A wake for a job that is ALREADY RUNNING must be re-queued, not dropped.

    Canary day 0 (2026-08-18) measured the cost of dropping it: both live wakes
    hit the duplicate-fire guard because the producer and consumer share a
    schedule boundary (tracker-cycle `0 */4`, matcher `0 */2` — the tracker's
    SCORE_REQUESTs landed 10:06:51-54, inside the matcher run that started
    10:00:57). The wake was discarded, so the messages waited for the next
    scheduled run up to two hours later and the dispatcher's ledger claim aged
    out unfinished. Event dispatch delivered zero accelerations.

    Re-queueing makes the wake survive the in-flight run: the next tick after it
    finishes dispatches the job. The channel is a set, so a re-queue is one
    entry no matter how many ticks it waits, and a genuinely hung run is bounded
    by the cron wall-clock timeout rather than by this loop.
    """

    def _jobs(self, monkeypatch, rows):
        from cron import scheduler
        monkeypatch.setattr(scheduler, "load_jobs", lambda: rows)
        return scheduler

    def test_running_job_wake_is_requeued_not_collected(self, monkeypatch):
        s = self._jobs(monkeypatch, [{"id": "j1", "name": "matcher", "enabled": True}])
        monkeypatch.setattr(s, "get_running_job_ids", lambda: frozenset({"j1"}))
        wake_channel.request_wake("j1", caller="test", reason="r")

        assert s._collect_woken_jobs(exclude_ids=set()) == []
        # still pending for a later tick — this is the whole point
        assert "j1" in wake_channel.pending_wakes()

    def test_requeued_wake_dispatches_once_the_run_finishes(self, monkeypatch):
        s = self._jobs(monkeypatch, [{"id": "j1", "name": "matcher", "enabled": True}])
        running = {"j1"}
        monkeypatch.setattr(s, "get_running_job_ids", lambda: frozenset(running))
        wake_channel.request_wake("j1", caller="test", reason="r")

        assert s._collect_woken_jobs(exclude_ids=set()) == []   # blocked, requeued
        running.clear()                                          # run finishes
        got = s._collect_woken_jobs(exclude_ids=set())

        assert [j["id"] for j in got] == ["j1"]
        # Collection alone is not a handoff. The exact wake remains until the
        # scheduler successfully submits this returned job to an executor.
        assert wake_channel.pending_wakes() == frozenset({"j1"})

    def test_requeue_survives_many_blocked_ticks_as_one_entry(self, monkeypatch):
        """A 12-minute run spans ~12 ticks; the channel must not grow."""
        s = self._jobs(monkeypatch, [{"id": "j1", "name": "matcher", "enabled": True}])
        monkeypatch.setattr(s, "get_running_job_ids", lambda: frozenset({"j1"}))
        wake_channel.request_wake("j1", caller="test", reason="r")

        for _ in range(12):
            assert s._collect_woken_jobs(exclude_ids=set()) == []
        assert wake_channel.pending_wakes() == frozenset({"j1"})

    def test_a_disabled_running_job_is_still_not_revived(self, monkeypatch):
        """Re-queue must not resurrect a wake an operator's disable should kill."""
        s = self._jobs(monkeypatch, [{"id": "j1", "name": "matcher", "enabled": False}])
        monkeypatch.setattr(s, "get_running_job_ids", lambda: frozenset({"j1"}))
        wake_channel.request_wake("j1", caller="test", reason="r")

        assert s._collect_woken_jobs(exclude_ids=set()) == []
        assert wake_channel.pending_wakes() == frozenset()

    def test_unknown_running_job_is_still_dropped(self, monkeypatch):
        s = self._jobs(monkeypatch, [{"id": "other", "name": "a", "enabled": True}])
        monkeypatch.setattr(s, "get_running_job_ids", lambda: frozenset({"j1"}))
        wake_channel.request_wake("j1", caller="test", reason="r")

        assert s._collect_woken_jobs(exclude_ids=set()) == []
        assert wake_channel.pending_wakes() == frozenset()

    def test_scheduled_fire_this_tick_still_consumes_the_wake(self, monkeypatch):
        """exclude_ids means the job is ALREADY firing now — the wake is
        satisfied, not blocked, so it must be consumed rather than re-queued."""
        s = self._jobs(monkeypatch, [{"id": "j1", "name": "matcher", "enabled": True}])
        monkeypatch.setattr(s, "get_running_job_ids", lambda: frozenset())
        wake_channel.request_wake("j1", caller="test", reason="r")

        assert s._collect_woken_jobs(exclude_ids={"j1"}) == []
        assert wake_channel.pending_wakes() == frozenset()
