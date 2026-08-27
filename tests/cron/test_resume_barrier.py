"""A pause that carries an authorization condition must be a fence, not a flag.

THE INCIDENT, stated accurately -- the first reading of it was wrong, and the
wrong reading is itself part of what these tests defend against.

At 2026-08-26T05:17:33-35Z -- ``event_bus.db`` stores UTC, so 01:17 EDT, and
mixing the two is its own trap -- the three Gate-2 barrier jobs
(``jobflow-matcher``, ``jaum-inbox-sweeper``, ``jaum-daytime-relay``) were
resumed inside three seconds, and ``caller`` is null on all three CRON_RESUMED
rows, against ``"hermes_cli:cron_resume"`` on every other resume that day. It
is tempting to read that null as an unsanctioned actor. TWO independent
sessions did, and one of them re-paused all three jobs on that reading,
reversing an operator override.

It was not. The lift was ``bin/gate2_resume_barrier_set.py``, the sanctioned
executor, which at that moment simply OMITTED its ``caller=`` argument. The
artifact that still checks out is the script's own mtime, 2026-08-26T01:27:44
EDT -- about ten minutes AFTER the lift -- so the ``caller=CALLER`` line now
visible at its line 105 did not exist when those events were emitted. Reading
the current source and concluding "this tool always stamps a caller, so the
null came from somewhere else" is judging a past event against a later version
of an untracked file.

A second artifact corroborated it and is now GONE, which is worth knowing
before anyone tries to re-verify from it. The script backs up jobs.json at its
line 92 to a FIXED name, ``profiles/main/cron/jobs.json.pre-gate2-resume``, so
every run clobbers the last. It read 01:17:20 -- thirteen seconds before the
resume -- when checked that morning; a second run at 12:18 EDT overwrote it and
the 01:17 pre-image is unrecoverable. That is a reading taken at the time, not
a file to go look at.

So: caller=None does NOT imply "ran outside the tooling". What it does imply
is that an EMPTY attribution is not neutral -- it actively misleads, and it
cost two sessions a false forensic narrative. That is the first thing these
tests cover.

Two separate defects are exercised below, and each stands on its own
regardless of who ran what that night.

**Identity.** ``resume_job(job_id)`` accepted ``caller=None`` and merely logged
a warning, so a sanctioned tool with a one-line omission produced
unattributable audit rows for ten minutes and nothing objected. A refusal
would have caught it on the first invocation. All five in-tree call sites
(hermes_cli, hermes_console, both HTTP APIs, the LLM tool) already pass a
caller, so refusing a blank one on a REASONED pause breaks no production path.

**Structure.** This is the one that matters, and it is why the identity fix
alone is not enough. The barrier lived in ``paused_reason``, and
``_unpause_updates`` CLEARS ``paused_reason`` -- correctly, a running job must
not advertise why it was once paused. So the condition was destroyed by the
very act it existed to gate, and nothing downstream could re-check it: the
admission scan reads ``enabled``/``state``, both of which an un-pause resets.

The sanctioned script does run ``gate2_landed_check.py`` and does refuse on NOT
LANDED -- but it ships a ``--skip-gate-check`` flag, self-labelled "ARM-TEST
ONLY ... Never use for a real resume", which skips that subprocess outright. It
was used for a real resume twice: at 01:17 EDT and again at 12:18, the second
time with Gate 2 measured at 0/25 repaired, exit 1, NOT LANDED. An arm-test
bypass shipping on the production script is a documented override, not a closed
gate. A caller string proves WHO acted; it can never prove they were ALLOWED to.

Which is the argument for doing this at ADMISSION rather than in the executor:
a barrier the scheduler re-reads would have held through BOTH lifts, because it
refuses the fire independently of how -- or by whom -- the un-pause happened.

The fix is ``resume_barrier``: a durable ``{reason, set_at, set_by}`` record
that no un-pause path touches, that ``update_job`` cannot reach (it is in
``_IMMUTABLE_JOB_FIELDS``), and that the scheduler re-reads at ADMISSION. The
distinguishing property, and the one the arm tests below exercise, is that a
barriered job refuses to run *even when something un-pauses it anyway* --
including a direct ``jobs.json`` write, which is the one actor no in-process
check can intercept.

Its honest limit is tested too: a FILE-LEVEL restore of a pre-barrier
jobs.json genuinely retires the barrier, because the field is durable state
rather than a proof. ``restore_jobs_cas`` refuses that shape; ``cp`` cannot be
refused by anything in this process.

Hermeticity: ``tests/conftest.py``'s autouse ``_hermetic_environment`` points
HERMES_HOME at a per-test tmpdir, so every write here lands there and never in
the canonical profile store.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cron import jobs as J


BARRIER_REASON = (
    "Gate-2 identity-clobber ceremony barrier; resume only after Gate 2 lands"
)


@pytest.fixture
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory (mirrors tests/cron/test_jobs.py)."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def _barriered(name="target", schedule="every 1h", pause_reason=BARRIER_REASON):
    """A paused job carrying a barrier — the 2026-08-26 shape."""
    job = J.create_job(prompt="work", schedule=schedule, name=name)
    J.pause_job(job["id"], reason=pause_reason, caller="test:pause")
    J.set_resume_barrier(job["id"], reason=BARRIER_REASON, caller="test:authorizer")
    return J.get_job(job["id"])


def _force_unpause_on_disk(job_id):
    """Un-pause a job by writing jobs.json directly, bypassing every API.

    This is the arm test's whole point. Every in-process guard can be bypassed
    by a writer that does not call it, and jobs.json is a plain file — the
    2026-08-26 lift went through ``resume_job``, but the next one need not.
    A fence that only holds against callers who politely ask is not a fence, so
    the admission gates are tested against the rudest possible un-pause.
    """
    with J._jobs_lock():
        jobs = J.load_jobs()
        for row in jobs:
            if row["id"] == job_id:
                row["enabled"] = True
                row["state"] = "scheduled"
                row["paused"] = False
                row["paused_at"] = None
                row["paused_reason"] = None
                row["next_run_at"] = (
                    datetime.now(timezone.utc) - timedelta(minutes=5)
                ).isoformat()
        J.save_jobs(jobs)


# ---------------------------------------------------------------------------
# Identity: an anonymous resume of a reasoned pause
# ---------------------------------------------------------------------------


class TestResumeRequiresCallerForReasonedPause:
    def test_anonymous_resume_of_a_reasoned_pause_is_refused(self, tmp_cron_dir):
        """The literal 2026-08-26 call: resume_job(job_id) and nothing else."""
        job = J.create_job(prompt="work", schedule="every 1h", name="reasoned")
        J.pause_job(job["id"], reason=BARRIER_REASON, caller="test:pause")

        with pytest.raises(ValueError, match="requires a non-empty caller"):
            J.resume_job(job["id"])

        # Refused means refused: the job is still contained afterwards. A guard
        # that raises after the write would be worse than none, because the
        # exception would read as "nothing happened".
        after = J.get_job(job["id"])
        assert after["state"] == "paused"
        assert after["enabled"] is False
        assert after["paused_reason"] == BARRIER_REASON

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_blank_caller_counts_as_anonymous(self, tmp_cron_dir, blank):
        """An empty string must not satisfy the check that a null fails."""
        job = J.create_job(prompt="work", schedule="every 1h", name="reasoned")
        J.pause_job(job["id"], reason="because", caller="test:pause")
        with pytest.raises(ValueError, match="requires a non-empty caller"):
            J.resume_job(job["id"], caller=blank)

    def test_anonymous_resume_of_an_unreasoned_pause_still_works(self, tmp_cron_dir):
        """Back-compat: a pause that states no condition has none to protect.

        Deliberately narrow. Tightening every resume would break callers that
        never had a condition to honour, and the defect was specifically about
        lifting somebody's *stated* condition without saying who you are.
        """
        job = J.create_job(prompt="work", schedule="every 1h", name="quiet")
        J.pause_job(job["id"], caller="test:pause")
        resumed = J.resume_job(job["id"])
        assert resumed is not None
        assert resumed["state"] == "scheduled"

    def test_named_caller_resumes_a_reasoned_pause(self, tmp_cron_dir):
        """The sanctioned path keeps working — no in-tree call site regresses."""
        job = J.create_job(prompt="work", schedule="every 1h", name="reasoned")
        J.pause_job(job["id"], reason=BARRIER_REASON, caller="test:pause")
        resumed = J.resume_job(job["id"], caller="hermes_cli:cron_resume")
        assert resumed["state"] == "scheduled"
        assert resumed["enabled"] is True
        assert resumed["paused_reason"] is None


# ---------------------------------------------------------------------------
# The barrier itself
# ---------------------------------------------------------------------------


class TestSetAndClearBarrier:
    def test_set_records_who_what_when(self, tmp_cron_dir):
        job = J.create_job(prompt="work", schedule="every 1h", name="target")
        J.set_resume_barrier(job["id"], reason=BARRIER_REASON, caller="diego:gate2")
        barrier = J.get_job(job["id"])["resume_barrier"]
        assert barrier["reason"] == BARRIER_REASON
        assert barrier["set_by"] == "diego:gate2"
        assert barrier["set_at"]

    def test_set_does_not_pause(self, tmp_cron_dir):
        """Barriering and pausing are separate acts on purpose.

        A barrier on a running job is meaningful — it takes effect at the next
        admission check — and conflating the two would mean you cannot express
        "let the in-flight run finish, but do not start another".
        """
        job = J.create_job(prompt="work", schedule="every 1h", name="target")
        J.set_resume_barrier(job["id"], reason=BARRIER_REASON, caller="diego:gate2")
        assert J.get_job(job["id"])["state"] == "scheduled"

    @pytest.mark.parametrize("bad", ["", "   ", None])
    def test_set_refuses_blank_caller_and_blank_reason(self, tmp_cron_dir, bad):
        job = J.create_job(prompt="work", schedule="every 1h", name="target")
        with pytest.raises(ValueError):
            J.set_resume_barrier(job["id"], reason=BARRIER_REASON, caller=bad)
        with pytest.raises(ValueError):
            J.set_resume_barrier(job["id"], reason=bad, caller="diego:gate2")
        assert J.get_job(job["id"]).get("resume_barrier") is None

    def test_clear_archives_the_barrier_and_the_justification(self, tmp_cron_dir):
        """The lift is the moment worth attributing, not the barrier."""
        job = _barriered()
        J.clear_resume_barrier(
            job["id"], caller="diego:gate2-landed", reason="25/25 targets carry score_repairs"
        )
        after = J.get_job(job["id"])
        assert after["resume_barrier"] is None
        [entry] = after["resume_barrier_history"]
        assert entry["reason"] == BARRIER_REASON
        assert entry["set_by"] == "test:authorizer"
        assert entry["cleared_by"] == "diego:gate2-landed"
        assert entry["cleared_reason"] == "25/25 targets carry score_repairs"

    @pytest.mark.parametrize("bad", ["", "   ", None])
    def test_clear_refuses_blank_caller_and_blank_reason(self, tmp_cron_dir, bad):
        job = _barriered()
        with pytest.raises(ValueError):
            J.clear_resume_barrier(job["id"], caller=bad, reason="landed")
        with pytest.raises(ValueError):
            J.clear_resume_barrier(job["id"], caller="diego", reason=bad)
        assert J.get_job(job["id"])["resume_barrier"] is not None

    def test_clear_does_not_resume(self, tmp_cron_dir):
        """"I may lift this" and "I am putting it back on the schedule" are
        never the same keystroke."""
        job = _barriered()
        J.clear_resume_barrier(job["id"], caller="diego", reason="landed")
        after = J.get_job(job["id"])
        assert after["state"] == "paused"
        assert after["enabled"] is False

    def test_clearing_an_absent_barrier_is_a_no_op(self, tmp_cron_dir):
        """Idempotent teardown must not fail on its second run."""
        job = J.create_job(prompt="work", schedule="every 1h", name="target")
        result = J.clear_resume_barrier(job["id"], caller="diego", reason="n/a")
        assert result is not None
        assert result.get("resume_barrier") is None

    def test_resetting_replaces_and_archives_without_a_gap(self, tmp_cron_dir):
        """Sharpening a live barrier's wording must not require clearing it.

        The window between a clear and a re-set is exactly when the job would
        slip through, so re-setting is allowed in place.
        """
        job = _barriered()
        J.set_resume_barrier(job["id"], reason="sharper wording", caller="diego")
        after = J.get_job(job["id"])
        assert after["resume_barrier"]["reason"] == "sharper wording"
        assert after["resume_barrier_history"][-1]["reason"] == BARRIER_REASON

    def test_history_is_capped(self, tmp_cron_dir):
        job = J.create_job(prompt="work", schedule="every 1h", name="target")
        for i in range(J.RESUME_BARRIER_HISTORY_LIMIT + 5):
            J.set_resume_barrier(job["id"], reason=f"barrier {i}", caller="diego")
        history = J.get_job(job["id"])["resume_barrier_history"]
        assert len(history) == J.RESUME_BARRIER_HISTORY_LIMIT
        assert history[-1]["reason"] == f"barrier {J.RESUME_BARRIER_HISTORY_LIMIT + 3}"


class TestBarrierIsUnreachableThroughUpdateJob:
    """``update_job`` is the shared writer under every lifecycle path.

    If a barrier could be set or cleared as an ordinary field update, then
    every surface that forwards a client-supplied patch — both HTTP APIs, the
    LLM tool's ``update`` action — would be a way around it. Refusing at
    ``update_job`` makes ``set_resume_barrier`` / ``clear_resume_barrier`` the
    only doors, and both demand a caller.
    """

    def test_update_job_refuses_to_set_a_barrier(self, tmp_cron_dir):
        job = J.create_job(prompt="work", schedule="every 1h", name="target")
        with pytest.raises(ValueError, match="cannot be updated"):
            J.update_job(job["id"], {"resume_barrier": {"reason": "smuggled"}})
        assert J.get_job(job["id"]).get("resume_barrier") is None

    def test_update_job_refuses_to_clear_a_barrier(self, tmp_cron_dir):
        job = _barriered()
        with pytest.raises(ValueError, match="cannot be updated"):
            J.update_job(job["id"], {"resume_barrier": None})
        assert J.get_job(job["id"])["resume_barrier"] is not None

    def test_unpausing_through_update_job_leaves_the_barrier(self, tmp_cron_dir):
        """The narrow bypass: flip enabled/state without touching the barrier.

        This one SUCCEEDS as a write — ``update_job`` is not the place to
        adjudicate authorization — and the point is what happens next. The
        barrier survives, so the admission gates below still refuse the job.
        """
        job = _barriered()
        J.update_job(job["id"], {"enabled": True, "state": "scheduled"})
        assert J.get_job(job["id"])["resume_barrier"] is not None


# ---------------------------------------------------------------------------
# Every un-pause path refuses
# ---------------------------------------------------------------------------


class TestUnpausePathsRefuse:
    def test_resume_job_refuses(self, tmp_cron_dir):
        job = _barriered()
        with pytest.raises(J.ResumeBarrierError) as exc:
            J.resume_job(job["id"], caller="hermes_cli:cron_resume")
        # The refusal must name the barrier, not just say "no": the next
        # person needs to know what condition to go satisfy.
        assert BARRIER_REASON in str(exc.value)
        assert "test:authorizer" in str(exc.value)
        assert J.get_job(job["id"])["state"] == "paused"

    def test_a_named_caller_does_not_help(self, tmp_cron_dir):
        """The barrier outranks identity — this is why (a) alone was not enough.

        A caller string is supplied by whoever is calling. It proves WHO acted;
        it can never prove they consulted the gate. The barrier is checked
        BEFORE the caller rule for exactly this reason.
        """
        job = _barriered()
        for caller in ("hermes_cli:cron_resume", "diego", "llm:cronjob_tool"):
            with pytest.raises(J.ResumeBarrierError):
                J.resume_job(job["id"], caller=caller)

    def test_trigger_job_refuses(self, tmp_cron_dir):
        """``hermes cron run`` on a paused job has always ended the pause.

        That makes it a second door onto the same authorization decision, and
        a shorter command than ``resume``. It is also a *separately understood*
        path: the 2026-08-25T22:22:41 matcher lift went through it. A barrier
        that only ``resume_job`` honoured would be walked around by typing
        ``run``.
        """
        job = _barriered()
        with pytest.raises(J.ResumeBarrierError):
            J.trigger_job(job["id"], caller="hermes_cli:cron_run")
        after = J.get_job(job["id"])
        assert after["state"] == "paused"
        assert after["enabled"] is False

    def test_trigger_job_on_an_unbarriered_job_still_works(self, tmp_cron_dir):
        job = J.create_job(prompt="work", schedule="every 1h", name="free")
        assert J.trigger_job(job["id"], caller="test:run") is not None

    def test_resume_works_once_the_barrier_is_cleared(self, tmp_cron_dir):
        """The fence opens — deliberately, attributably, and in two steps."""
        job = _barriered()
        J.clear_resume_barrier(job["id"], caller="diego:gate2-landed", reason="landed")
        resumed = J.resume_job(job["id"], caller="hermes_cli:cron_resume")
        assert resumed["state"] == "scheduled"
        assert resumed["enabled"] is True


# ---------------------------------------------------------------------------
# Admission — the structural half
# ---------------------------------------------------------------------------


class TestAdmissionRefusesEvenWhenUnpaused:
    """The property that separates a fence from a flag.

    Everything above intercepts a caller. These two tests bypass every caller
    and un-pause the job by writing ``jobs.json`` directly, then assert the job
    still does not run. That is the guarantee the 2026-08-26 pause did not have:
    once something cleared it, nothing downstream could tell it had ever been
    conditional.
    """

    def test_due_scan_skips_a_barriered_job(self, tmp_cron_dir):
        job = _barriered()
        _force_unpause_on_disk(job["id"])

        # Arm test: the same record without the barrier IS admitted, so the
        # skip below is attributable to the barrier and not to the job being
        # unrunnable for some unrelated reason.
        control = J.create_job(prompt="work", schedule="every 1h", name="control")
        _force_unpause_on_disk(control["id"])
        assert control["id"] in {j["id"] for j in J.get_due_jobs()}

        assert job["id"] not in {j["id"] for j in J.get_due_jobs()}

    def test_claim_fire_refuses_a_barriered_job(self, tmp_cron_dir):
        """The manual and recovery paths reach a fire without the due scan."""
        job = _barriered()
        _force_unpause_on_disk(job["id"])
        assert J.claim_job_for_fire(job["id"]) is False

        control = J.create_job(prompt="work", schedule="every 1h", name="control")
        _force_unpause_on_disk(control["id"])
        assert J.claim_job_for_fire(control["id"]) is True

    def test_a_malformed_barrier_still_fences(self, tmp_cron_dir):
        """Corrupting the fence must not be the cheapest way past it."""
        job = J.create_job(prompt="work", schedule="every 1h", name="target")
        with J._jobs_lock():
            jobs = J.load_jobs()
            for row in jobs:
                if row["id"] == job["id"]:
                    row["resume_barrier"] = "not a dict"
            J.save_jobs(jobs)
        _force_unpause_on_disk(job["id"])
        assert J.claim_job_for_fire(job["id"]) is False
        with pytest.raises(J.ResumeBarrierError):
            J.resume_job(job["id"], caller="hermes_cli:cron_resume")

    def test_an_empty_dict_is_not_a_barrier(self, tmp_cron_dir):
        """A record of not having one must not fence the job forever."""
        job = J.create_job(prompt="work", schedule="every 1h", name="target")
        with J._jobs_lock():
            jobs = J.load_jobs()
            for row in jobs:
                if row["id"] == job["id"]:
                    row["resume_barrier"] = {}
            J.save_jobs(jobs)
        assert J.claim_job_for_fire(job["id"]) is True


class TestUnpauseUpdatesNeverClearsTheBarrier:
    """The regression guard on the root cause.

    ``_unpause_updates`` clearing ``paused_reason`` is what turned an
    authorization condition into something a resume destroyed on its way past.
    If ``resume_barrier`` is ever added to that helper's update set, the fence
    becomes a flag again and every test above starts passing for the wrong
    reason — they would all be exercising a barrier that the un-pause they are
    testing had already deleted.
    """

    def test_unpause_updates_does_not_touch_resume_barrier(self, tmp_cron_dir):
        job = _barriered()
        updates = J._unpause_updates(J.get_job(job["id"]))
        assert "resume_barrier" not in updates
        assert "resume_barrier_history" not in updates

    def test_barrier_survives_a_full_pause_resume_cycle(self, tmp_cron_dir):
        job = J.create_job(prompt="work", schedule="every 1h", name="target")
        J.set_resume_barrier(job["id"], reason=BARRIER_REASON, caller="diego")
        # The pause/resume pair below is only reachable because the resume is
        # refused; force the un-pause on disk to prove the field survives the
        # write itself and not merely the refusal.
        J.pause_job(job["id"], reason="routine", caller="test:pause")
        _force_unpause_on_disk(job["id"])
        assert J.get_job(job["id"])["resume_barrier"]["reason"] == BARRIER_REASON


# ---------------------------------------------------------------------------
# Bulk containment CAS
# ---------------------------------------------------------------------------


class TestBarrierEventsCarryAnAttributedCaller:
    """The barrier's own audit rows must not repeat the caller=None failure.

    This is the direct lesson of the incident in this module's header: the
    events were emitted correctly and the ATTRIBUTION was empty, which read as
    an unattributed actor and sent two sessions down a false narrative. A
    barrier event can never carry a null caller, because cron.jobs refuses a
    blank one before the emit is reached.
    """

    def _events(self, monkeypatch):
        seen = []
        import events.producers.cron_lifecycle_emitter as E

        monkeypatch.setattr(
            E, "emit_cron_barrier",
            lambda bus, **kw: seen.append(kw) or "evt",
        )
        monkeypatch.setattr(J, "_get_event_bus", lambda: object())
        return seen

    def test_set_emits_an_attributed_event(self, tmp_cron_dir, monkeypatch):
        seen = self._events(monkeypatch)
        job = J.create_job(prompt="work", schedule="every 1h", name="target")
        J.set_resume_barrier(job["id"], reason=BARRIER_REASON, caller="diego:gate2")
        [evt] = seen
        assert evt["action"] == "barrier_set"
        assert evt["caller"] == "diego:gate2"
        assert evt["barrier_reason"] == BARRIER_REASON

    def test_clear_emits_the_justification_and_the_retired_barrier(
        self, tmp_cron_dir, monkeypatch
    ):
        """Both halves on one event, so the lift reads without a join.

        By the time anyone looks, the job record no longer holds the barrier
        at all -- it moved to resume_barrier_history. The event has to carry
        what was retired as well as why, or reading it means reconstructing
        the barrier from a second source.
        """
        job = _barriered()
        seen = self._events(monkeypatch)
        J.clear_resume_barrier(
            job["id"], caller="diego:gate2-landed", reason="25/25 carry score_repairs"
        )
        [evt] = seen
        assert evt["action"] == "barrier_cleared"
        assert evt["caller"] == "diego:gate2-landed"
        assert evt["reason"] == "25/25 carry score_repairs"
        assert evt["barrier_reason"] == BARRIER_REASON
        assert evt["barrier_set_by"] == "test:authorizer"

    def test_a_bus_failure_never_breaks_the_barrier_write(
        self, tmp_cron_dir, monkeypatch
    ):
        """The record is the authority; the event is best-effort.

        Same contract as emit_cron_lifecycle_safe. The barrier is already
        durable when the emit runs, so a bus outage costs an audit row and
        never a barrier that half-happened.
        """
        monkeypatch.setattr(
            J, "_get_event_bus", lambda: (_ for _ in ()).throw(RuntimeError("bus down"))
        )
        job = J.create_job(prompt="work", schedule="every 1h", name="target")
        assert J.set_resume_barrier(
            job["id"], reason=BARRIER_REASON, caller="diego"
        ) is not None
        assert J.get_job(job["id"])["resume_barrier"]["reason"] == BARRIER_REASON


class TestRestoreCasRefuses:
    def test_bulk_restore_cannot_revive_a_barriered_row(self, tmp_cron_dir, monkeypatch):
        """The third door. It is refused during validation, before any write.

        The CAS is all-or-nothing, so catching it at validation is the only
        place the whole restore can be refused cleanly — a refusal partway
        through would leave the dependency-ordered restore half-applied.
        """
        job = _barriered(name="jobflow-matcher")
        paused_row = next(r for r in J.load_jobs() if r["id"] == job["id"])
        target_row = dict(paused_row)
        target_row.update(
            {"enabled": True, "state": "scheduled", "paused": False,
             "paused_at": None, "paused_reason": None}
        )

        # The real DispatchBarrier is a production capability object from
        # jobflow_dispatch; stub its validator out so this test exercises the
        # barrier refusal rather than the quarantine plumbing around it.
        monkeypatch.setattr(
            J, "_require_dispatch_barrier",
            lambda barrier: {"schema_version": 1, "complete": True},
        )

        with pytest.raises(J.ResumeBarrierError):
            J.restore_jobs_cas(
                expected_paused_rows=[paused_row],
                target_rows=[target_row],
                dependency_order=["jobflow-matcher"],
                dispatch_barrier=object(),
                caller="test:restore",
            )
        assert J.get_job(job["id"])["state"] == "paused"

    def test_a_restore_from_a_PRE_BARRIER_backup_is_refused(
        self, tmp_cron_dir, monkeypatch
    ):
        """The shape most likely to occur during an incident rollback.

        A jobs.json backup taken before the barrier existed has rows with no
        ``resume_barrier`` key at all. Restoring one would not "lift" the
        barrier so much as erase the fact there had been one. It is refused
        because ``resume_barrier`` is outside the allowed containment-field
        set, so present-here/absent-there lands in ``changed - allowed``.
        Raised as a ValueError rather than ResumeBarrierError -- this is the
        CAS's own field-scope guard doing the work, and the test exists to
        pin that it actually covers the new field rather than silently
        tolerating it.
        """
        monkeypatch.setattr(
            J, "_require_dispatch_barrier",
            lambda barrier: {"schema_version": 1, "complete": True},
        )
        job = _barriered(name="jobflow-matcher")
        paused_row = next(r for r in J.load_jobs() if r["id"] == job["id"])

        stale = {k: v for k, v in paused_row.items() if k != "resume_barrier"}
        with pytest.raises(ValueError, match="outside containment fields"):
            J.restore_jobs_cas(
                expected_paused_rows=[paused_row],
                target_rows=[stale],
                dependency_order=["jobflow-matcher"],
                dispatch_barrier=object(),
                caller="test:restore",
            )
        assert J.get_job(job["id"])["resume_barrier"] is not None

    def test_a_FILE_LEVEL_restore_does_retire_the_barrier(self, tmp_cron_dir):
        """The honest limit, pinned so nobody assumes otherwise.

        Nothing in this process can refuse a `cp old-jobs.json jobs.json` --
        that writer never calls in here. The barrier is durable STATE, not a
        proof, so restoring a pre-barrier snapshot genuinely retires it and
        the admission gates go quiet with it. This test asserts the gap
        rather than pretending it is closed: the operational rule is to
        re-assert barriers after any file-level rollback.
        """
        job = _barriered()
        pre_barrier = [
            {k: v for k, v in row.items() if k != "resume_barrier"}
            for row in J.load_jobs()
        ]
        with J._jobs_lock():
            J.save_jobs(pre_barrier)

        assert J.get_job(job["id"]).get("resume_barrier") is None
        _force_unpause_on_disk(job["id"])
        assert J.claim_job_for_fire(job["id"]) is True
