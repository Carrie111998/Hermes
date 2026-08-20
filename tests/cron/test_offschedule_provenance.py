"""Every off-schedule cron fire must be attributable from audit.jsonl alone.

A cron job runs at an instant its schedule does not name for exactly three
reasons. Until 2026-08-20 only ONE of them emitted anything:

    explicit trigger    trigger_job / request_run       emitted cron_triggered
    event wake          cron.wake_channel -> scheduler  emitted NOTHING
    missed-run recovery cron/jobs.py fire-once branch   emitted NOTHING

(Plain LATENESS — the scheduler observing an on-schedule job past its grace
window — is not a trigger and deliberately stays uninstrumented.)

The gap was not merely a missing feature: ``emit_cron_triggered_safe``'s
docstring asserted it was "shared by every off-schedule trigger path", and two
separate investigations read that sentence, found no events, and concluded the
provenance was unobtainable. These tests pin the sentence so it cannot go
false again silently.

Patching note: both call sites look the emitter up as a module global, so the
spy must be installed on the module that OWNS the name at the call site —
``cron.jobs`` for the recovery path, ``cron.scheduler`` for the wake path
(``scheduler`` does ``from cron.jobs import emit_cron_triggered_safe``, so
patching ``cron.jobs`` alone would miss it).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cron import wake_channel
from cron.jobs import (
    RECOVERY_FIRE_REASON,
    _RECOVERY_FIRE_MARKER,
    create_job,
    get_due_and_skipped_jobs,
    load_jobs,
    save_jobs,
)
from cron.scheduler import WAKE_TRIGGER_CALLER, WAKE_TRIGGER_REASON


@pytest.fixture(autouse=True)
def _clean_wakes():
    wake_channel.clear_wakes()
    yield
    wake_channel.clear_wakes()


@pytest.fixture
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory (mirrors tests/cron/test_jobs.py)."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


class _Spy:
    """Records every emit_cron_triggered_safe call as a kwargs dict."""

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)

    def reasons(self):
        return [c["reason"] for c in self.calls]


@pytest.fixture
def wake_emits(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr("cron.scheduler.emit_cron_triggered_safe", spy)
    return spy


@pytest.fixture
def recovery_emits(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr("cron.jobs.emit_cron_triggered_safe", spy)
    return spy


def _set_next_run(job_id: str, iso: str) -> None:
    jobs = load_jobs()
    for j in jobs:
        if j["id"] == job_id:
            j["next_run_at"] = iso
    save_jobs(jobs)


def _set_recovery_policy(job_id: str, policy: str) -> None:
    jobs = load_jobs()
    for j in jobs:
        if j["id"] == job_id:
            j["recovery_policy"] = policy
    save_jobs(jobs)


# ---------------------------------------------------------------------------
# EVENT WAKE
# ---------------------------------------------------------------------------


class TestEventWakeEmitsProvenance:
    def _jobs(self, monkeypatch, rows):
        from cron import scheduler
        monkeypatch.setattr(scheduler, "load_jobs", lambda: rows)
        return scheduler

    def test_woken_job_emits_one_cron_triggered(self, monkeypatch, wake_emits):
        s = self._jobs(monkeypatch, [{"id": "j1", "name": "a", "enabled": True}])
        wake_channel.request_wake("j1", caller="events:dispatcher", reason="score_request")

        got = s._collect_woken_jobs(exclude_ids=set())

        assert [j["id"] for j in got] == ["j1"]
        assert len(wake_emits.calls) == 1
        call = wake_emits.calls[0]
        assert call["job_id"] == "j1"
        assert call["job_name"] == "a"
        assert call["caller"] == WAKE_TRIGGER_CALLER
        assert call["reason"] == WAKE_TRIGGER_REASON

    def test_emit_records_that_the_schedule_did_not_move(self, monkeypatch, wake_emits):
        """A wake adds a run; it must not shift the cadence, and must say so."""
        s = self._jobs(monkeypatch, [{
            "id": "j1", "name": "a", "enabled": True,
            "next_run_at": "2026-08-20T18:00:00+00:00",
        }])
        advanced = []
        monkeypatch.setattr(s, "advance_next_run", lambda jid: advanced.append(jid))
        wake_channel.request_wake("j1", caller="test", reason="r")

        s._collect_woken_jobs(exclude_ids=set())

        assert advanced == []
        call = wake_emits.calls[0]
        assert call["previous_next_run_at"] == "2026-08-20T18:00:00+00:00"
        assert call["new_next_run_at"] == call["previous_next_run_at"]

    def test_request_wake_alone_emits_nothing(self, wake_emits):
        """The emit belongs at the FIRE, not at the request.

        request_wake is called once per producer event AND again by the
        re-queue branch, so emitting there would count a wake more than once.
        """
        wake_channel.request_wake("j1", caller="test", reason="r")
        assert wake_emits.calls == []

    def test_disabled_job_emits_nothing(self, monkeypatch, wake_emits):
        s = self._jobs(monkeypatch, [{"id": "j1", "name": "a", "enabled": False}])
        wake_channel.request_wake("j1", caller="test", reason="r")

        assert s._collect_woken_jobs(exclude_ids=set()) == []
        assert wake_emits.calls == []

    def test_unknown_job_emits_nothing(self, monkeypatch, wake_emits):
        s = self._jobs(monkeypatch, [{"id": "other", "name": "a", "enabled": True}])
        wake_channel.request_wake("ghost", caller="test", reason="r")

        assert s._collect_woken_jobs(exclude_ids=set()) == []
        assert wake_emits.calls == []

    def test_job_already_due_on_schedule_emits_nothing(self, monkeypatch, wake_emits):
        """It is firing ON schedule this tick — that is not an off-schedule fire."""
        s = self._jobs(monkeypatch, [{"id": "j1", "name": "a", "enabled": True}])
        wake_channel.request_wake("j1", caller="test", reason="r")

        assert s._collect_woken_jobs(exclude_ids={"j1"}) == []
        assert wake_emits.calls == []

    def test_requeued_running_job_emits_once_across_ticks(self, monkeypatch, wake_emits):
        """The double-count trap: a busy worker re-queues its wake every tick.

        Emitting at request_wake would produce one event per tick until the
        run ended. Emitting at the fire produces exactly one, whenever it
        finally happens.
        """
        s = self._jobs(monkeypatch, [{"id": "j1", "name": "a", "enabled": True}])
        running = {"j1"}
        monkeypatch.setattr(s, "get_running_job_ids", lambda: set(running))
        wake_channel.request_wake("j1", caller="events:dispatcher", reason="score_request")

        for _ in range(3):
            assert s._collect_woken_jobs(exclude_ids=set()) == []
        assert wake_emits.calls == []

        running.clear()
        got = s._collect_woken_jobs(exclude_ids=set())

        assert [j["id"] for j in got] == ["j1"]
        assert len(wake_emits.calls) == 1
        # Attribution names the MECHANISM, not the re-queue caller — the
        # re-queue path passes caller="cron.tick", which must not surface.
        assert wake_emits.calls[0]["caller"] == WAKE_TRIGGER_CALLER

    def test_emit_failure_never_stalls_the_tick(self, monkeypatch):
        """Losing a wake degrades to the reconciler; raising stalls every job."""
        from cron import scheduler

        def _boom(**_kwargs):
            raise RuntimeError("event bus down")

        monkeypatch.setattr(scheduler, "load_jobs",
                            lambda: [{"id": "j1", "name": "a", "enabled": True}])
        monkeypatch.setattr(scheduler, "emit_cron_triggered_safe", _boom)
        wake_channel.request_wake("j1", caller="test", reason="r")

        got = scheduler._collect_woken_jobs(exclude_ids=set())

        assert [j["id"] for j in got] == ["j1"], "the job must still fire"


# ---------------------------------------------------------------------------
# MISSED-RUN RECOVERY
# ---------------------------------------------------------------------------


class TestRecoveryFireEmitsProvenance:
    def test_catch_up_fire_emits_one_cron_triggered(
        self, tmp_cron_dir, monkeypatch, recovery_emits
    ):
        """Daily cron missed by 4h, no policy → fires once, and says why."""
        now = datetime(2026, 4, 30, 3, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        job = create_job(prompt="daily check", schedule="0 23 * * *")
        _set_next_run(job["id"], "2026-04-29T23:00:00+00:00")

        due, _skipped = get_due_and_skipped_jobs()

        assert any(j["id"] == job["id"] for j in due)
        calls = [c for c in recovery_emits.calls if c["job_id"] == job["id"]]
        assert len(calls) == 1
        assert calls[0]["caller"] == "cron.miss_recovery"
        assert calls[0]["reason"] == RECOVERY_FIRE_REASON
        # The catch-up leaves next_run_at alone (advance_next_run moves it just
        # before the run), so the payload records an unmoved schedule.
        assert calls[0]["previous_next_run_at"] == "2026-04-29T23:00:00+00:00"
        assert calls[0]["new_next_run_at"] == calls[0]["previous_next_run_at"]

    def test_on_time_fire_within_grace_emits_nothing(
        self, tmp_cron_dir, monkeypatch, recovery_emits
    ):
        """Ordinary tick jitter is not a trigger and must not be recorded."""
        now = datetime(2026, 4, 29, 23, 0, 30, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        job = create_job(prompt="daily check", schedule="0 23 * * *")
        _set_next_run(job["id"], "2026-04-29T23:00:00+00:00")

        due, _skipped = get_due_and_skipped_jobs()

        assert any(j["id"] == job["id"] for j in due), "should still fire on time"
        assert recovery_emits.calls == []

    def test_skipped_recovery_emits_nothing(
        self, tmp_cron_dir, monkeypatch, recovery_emits
    ):
        """A SKIPPED miss never fired, so there is no trigger to record.

        cron_skipped already covers that case; a cron_triggered here would
        claim a run that did not happen.
        """
        now = datetime(2026, 4, 30, 2, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        job = create_job(prompt="anchored daily", schedule="0 23 * * *")
        _set_next_run(job["id"], "2026-04-29T23:00:00+00:00")
        _set_recovery_policy(job["id"], "skip_only")

        due, skipped = get_due_and_skipped_jobs()

        assert not any(j["id"] == job["id"] for j in due)
        assert [s["reason"] for s in skipped if s["job_id"] == job["id"]] == ["skip_only"]
        assert recovery_emits.calls == []

    def test_marker_never_reaches_jobs_json(self, tmp_cron_dir, monkeypatch):
        """The transient key rides a deepcopy; jobs.json must stay clean."""
        now = datetime(2026, 4, 30, 3, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        job = create_job(prompt="daily check", schedule="0 23 * * *")
        _set_next_run(job["id"], "2026-04-29T23:00:00+00:00")

        get_due_and_skipped_jobs()

        raw = (tmp_cron_dir / "cron" / "jobs.json").read_text(encoding="utf-8")
        assert _RECOVERY_FIRE_MARKER not in raw
        assert all(_RECOVERY_FIRE_MARKER not in j for j in load_jobs())

    def test_marker_is_popped_from_the_returned_due_job(self, tmp_cron_dir, monkeypatch):
        """Downstream (run_job, the parallel pool) must not see the marker."""
        now = datetime(2026, 4, 30, 3, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        job = create_job(prompt="daily check", schedule="0 23 * * *")
        _set_next_run(job["id"], "2026-04-29T23:00:00+00:00")

        due, _skipped = get_due_and_skipped_jobs()

        fired = [j for j in due if j["id"] == job["id"]]
        assert fired and _RECOVERY_FIRE_MARKER not in fired[0]

    def test_emit_happens_with_the_jobs_lock_released(
        self, tmp_cron_dir, monkeypatch
    ):
        """_jobs_lock() is a cross-process file lock documented as short.

        An event-bus write inside it would make every standalone `hermes cron`
        invocation on the box queue behind a SQLite transaction.
        """
        import cron.jobs as J

        now = datetime(2026, 4, 30, 3, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        job = create_job(prompt="daily check", schedule="0 23 * * *")
        _set_next_run(job["id"], "2026-04-29T23:00:00+00:00")

        depth_at_emit = []

        def _spy(**_kwargs):
            depth_at_emit.append(getattr(J._jobs_lock_state, "depth", 0))

        monkeypatch.setattr(J, "emit_cron_triggered_safe", _spy)

        get_due_and_skipped_jobs()

        assert depth_at_emit == [0], "emit ran while the jobs lock was still held"

    def test_emit_failure_never_breaks_the_due_scan(self, tmp_cron_dir, monkeypatch):
        now = datetime(2026, 4, 30, 3, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)

        job = create_job(prompt="daily check", schedule="0 23 * * *")
        _set_next_run(job["id"], "2026-04-29T23:00:00+00:00")

        def _boom(**_kwargs):
            raise RuntimeError("event bus down")

        monkeypatch.setattr("cron.jobs.emit_cron_triggered_safe", _boom)

        due, _skipped = get_due_and_skipped_jobs()

        assert any(j["id"] == job["id"] for j in due), "the job must still fire"


# ---------------------------------------------------------------------------
# The docstring is the contract
# ---------------------------------------------------------------------------


class TestSharedPathClaimIsTrue:
    """"Shared by every off-schedule trigger path" is a testable claim."""

    def test_the_three_causes_stay_separable_in_audit_jsonl(self):
        """Distinct reason= strings are what keeps the causes countable."""
        assert WAKE_TRIGGER_REASON == "event_wake"
        assert RECOVERY_FIRE_REASON == "missed_run_recovery"
        assert WAKE_TRIGGER_REASON != RECOVERY_FIRE_REASON

    def test_docstring_names_every_path_it_claims_to_share(self):
        from cron.jobs import emit_cron_triggered_safe

        doc = emit_cron_triggered_safe.__doc__ or ""
        assert "shared by every off-schedule trigger path" in doc.lower()
        for token in (
            "trigger_job",
            "_collect_woken_jobs",
            "_emit_recovery_fire_",
            WAKE_TRIGGER_REASON,
            "missed_run_",
        ):
            assert token in doc, f"docstring no longer names {token!r}"

    def test_both_new_paths_route_through_the_shared_emitter(self):
        """Not a spy assertion — the source must literally call it.

        A future refactor that inlines a bus.emit() at either site would keep
        every behavioural test green while making the docstring false again.
        """
        import inspect

        from cron import jobs as J
        from cron import scheduler as S

        assert "emit_cron_triggered_safe(" in inspect.getsource(
            J._emit_recovery_fire_triggers)
        assert "emit_cron_triggered_safe(" in inspect.getsource(
            S._collect_woken_jobs)
