"""Pausing and resuming a cron job must leave a record on the event bus.

Until 2026-08-25 neither did. ``trigger_job`` emitted CRON_TRIGGERED; both
``pause_job`` and ``resume_job`` routed through ``update_job``, which has no
emit path at all, so the transition existed only as a changed field in
``jobs.json`` with no timestamped, attributed record anywhere.

That is not a hypothetical gap. On 2026-08-24/25 eight jobflow/jaum/tracker
rows were paused and resumed repeatedly by an actor nobody could identify; two
independent sessions tried to attribute the churn from ``audit.jsonl`` and from
agent transcripts and both failed, because there was nothing to find. A peer
confirmed it empirically: ``cron_triggered`` was present for the 18:00Z fires
of jaum-inbox-sweeper and tracker-operator-drain, and *nothing whatsoever* for
the 14:07:04 EDT pause of tracker-operator-drain.

The job RECORD gained the WHY in cfe15649ad (``paused_reason``, archived into
``paused_history`` on resume). These tests cover the other half: the WHO and
the WHEN of the transition itself.

Three properties are worth stating up front, because each is a thing the
record alone cannot tell you and each has its own test below:

  * ``previous_state`` separates a real transition from a repeat pause of an
    already-paused job. The record after a repeat pause is byte-identical in
    shape to the record after a first pause; only the event distinguishes
    them, and "was this eight pauses or one?" was the churn investigation's
    first unanswerable question.
  * ``trigger_job`` implicitly ends a pause (it sets ``enabled: True`` and
    clears the pause fields). Without an event on that path a reader joining
    CRON_PAUSED to CRON_RESUMED sees an unterminated pause on a job that has
    been running for days — worse than no trail, because it reads as a job
    still contained.
  * A bus failure must never break a pause. The emit is best-effort in the
    same sense as ``emit_cron_triggered_safe``: the state mutation is already
    durable, so a bus outage costs an audit record and nothing else.

Hermeticity: ``tests/conftest.py``'s autouse ``_hermetic_environment`` points
HERMES_HOME at a per-test tmpdir, and ``get_default_hermes_root()`` returns an
outside-``~/.hermes`` HERMES_HOME as-is, so ``EventBus()`` here resolves into
that tmpdir rather than the canonical bus. The real-bus tests below are
therefore end-to-end (they prove the event is actually persisted and queryable,
not merely that a function was called) without writing to production.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

from cron import jobs as J
from events.bus import EventBus
from events.schema import EventType


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    """Redirect cron storage to a temp directory."""
    with J.use_cron_store(tmp_path):
        yield tmp_path


@pytest.fixture
def bus():
    return EventBus()


def _mk(name="alpha", **extra):
    """Create a real job through the public API and return the stored row."""
    return J.create_job(
        name=name,
        prompt=f"run {name}",
        schedule="0 9 * * *",
        **extra,
    )


def _payloads(bus, event_type):
    return [e.payload for e in bus.query(event_type=event_type)]


def _seed_cursor_at_zero(bus, subscriber_id):
    """Force a subscriber's cursor to 0 so it sees events emitted before its
    first poll. The bus's first-registration default jumps to head-of-bus to
    prevent backlog floods on real deploys; a test needs the backfill."""
    bus._execute(
        """INSERT INTO subscriber_cursors (subscriber_id, last_rowid, updated_at)
           VALUES (?, 0, datetime('now'))
           ON CONFLICT(subscriber_id) DO UPDATE SET last_rowid = 0""",
        (subscriber_id,),
    )


# ---------------------------------------------------------------------------
# PAUSE
# ---------------------------------------------------------------------------


class TestPauseEmits:
    def test_pause_writes_a_cron_paused_event_to_the_bus(self, store, bus):
        job = _mk()

        assert _payloads(bus, EventType.CRON_PAUSED) == [], (
            "creating a job must not emit a pause"
        )

        J.pause_job(job["id"], reason="jaum backlog", caller="hermes_cli:cron_pause")

        events = bus.query(event_type=EventType.CRON_PAUSED)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["job_id"] == job["id"]
        assert payload["job_name"] == "alpha"
        assert payload["caller"] == "hermes_cli:cron_pause"
        assert payload["reason"] == "jaum backlog"
        assert payload["action"] == "paused"
        assert payload["new_state"] == "paused"
        assert payload["previous_state"] == "scheduled"
        # paused_at on the event is the value that actually landed on the row,
        # not a second clock reading — a postmortem joins the two on it.
        assert payload["paused_at"] == J.get_job(job["id"])["paused_at"]

    def test_event_carries_job_id_for_correlation_not_only_in_the_payload(
        self, store, bus
    ):
        """``job_id`` is a first-class Event column, not just a payload key.

        ``bus.query`` has no job_id filter, but the digest/rowid readers and
        every subscriber that groups by job read the column. Emitting the id
        only into the payload would make this event invisible to them.
        """
        job = _mk()
        J.pause_job(job["id"], reason="why", caller="test:pause")

        event = bus.query(event_type=EventType.CRON_PAUSED)[0]
        assert event.job_id == job["id"]
        assert event.source == "alpha"

    def test_repeat_pause_of_a_paused_job_is_distinguishable_from_a_first_pause(
        self, store, bus
    ):
        """The churn discriminator: ``previous_state``.

        Pausing an already-paused job overwrites paused_at/paused_reason and
        leaves a record indistinguishable from a first pause. Eight rows
        flapping and eight rows paused once look identical in jobs.json. Only
        previous_state tells them apart.
        """
        job = _mk()
        J.pause_job(job["id"], reason="first", caller="test:pause")
        J.pause_job(job["id"], reason="second", caller="test:pause")

        payloads = _payloads(bus, EventType.CRON_PAUSED)
        assert len(payloads) == 2
        assert [p["previous_state"] for p in payloads] == ["scheduled", "paused"]
        assert [p["reason"] for p in payloads] == ["first", "second"]

    def test_whitespace_only_reason_is_recorded_as_none_on_the_event_too(
        self, store, bus
    ):
        """The event agrees with the record rather than reporting "   "."""
        job = _mk()
        J.pause_job(job["id"], reason="   ", caller="test:pause")

        assert J.get_job(job["id"])["paused_reason"] is None
        assert _payloads(bus, EventType.CRON_PAUSED)[0]["reason"] is None

    def test_unknown_job_emits_nothing(self, store, bus):
        assert J.pause_job("no-such-job", reason="x", caller="test:pause") is None
        assert bus.query(event_type=EventType.CRON_PAUSED) == []

    def test_anonymous_caller_warns(self, store, caplog):
        job = _mk()
        with caplog.at_level("WARNING", logger="cron.jobs"):
            J.pause_job(job["id"], reason="x")
        assert "pause_job called anonymously" in caplog.text

    def test_a_bus_failure_does_not_break_the_pause(self, store, monkeypatch, caplog):
        """Fail-open, exactly like the trigger emitter.

        The mutation is already durable when the emit runs, so the only correct
        response to a broken bus is to log and carry on. A pause that raises
        because audit failed would be a strictly worse system than one with no
        audit at all.
        """
        def _boom():
            raise RuntimeError("bus is down")

        monkeypatch.setattr(J, "_get_event_bus", _boom)
        job = _mk()

        with caplog.at_level("ERROR", logger="cron.jobs"):
            updated = J.pause_job(job["id"], reason="x", caller="test:pause")

        assert updated is not None
        assert updated["state"] == "paused"
        assert updated["enabled"] is False
        assert J.get_job(job["id"])["state"] == "paused"
        assert "cron_paused emit failed" in caplog.text


# ---------------------------------------------------------------------------
# RESUME
# ---------------------------------------------------------------------------


class TestResumeEmits:
    def test_resume_carries_the_pause_it_is_ending(self, store, bus):
        """``reason``/``paused_at`` on a resume are the values being RETIRED.

        ``_unpause_updates`` clears both from the record (a running job must
        not advertise why it was once paused). If the resume event did not
        carry them, the WHY would be reconstructible only by finding the
        matching CRON_PAUSED — and a pause that predates this feature, or one
        whose event was lost to a bus outage, has no match to find.
        """
        job = _mk()
        J.pause_job(job["id"], reason="jaum backlog", caller="test:pause")
        paused_at = J.get_job(job["id"])["paused_at"]

        J.resume_job(job["id"], caller="hermes_cli:cron_resume")

        events = bus.query(event_type=EventType.CRON_RESUMED)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["job_id"] == job["id"]
        assert payload["job_name"] == "alpha"
        assert payload["caller"] == "hermes_cli:cron_resume"
        assert payload["action"] == "resumed"
        assert payload["reason"] == "jaum backlog"
        assert payload["paused_at"] == paused_at
        assert payload["previous_state"] == "paused"
        assert payload["new_state"] == "scheduled"
        assert payload["next_run_at"] == J.get_job(job["id"])["next_run_at"]

        # And the record itself no longer carries the reason — which is the
        # whole point of putting it on the event.
        assert J.get_job(job["id"])["paused_reason"] is None

    def test_resume_event_agrees_with_the_paused_history_archive(self, store, bus):
        """Two records of the same pause must not disagree.

        cfe15649ad archives the finished pause into ``paused_history``. The
        event is the other copy; if they drifted, an auditor would have to
        pick one to believe.
        """
        job = _mk()
        J.pause_job(job["id"], reason="broken upstream", caller="test:pause")
        J.resume_job(job["id"], caller="test:resume")

        archived = J.get_job(job["id"])["paused_history"][-1]
        payload = _payloads(bus, EventType.CRON_RESUMED)[0]
        assert payload["reason"] == archived["paused_reason"]
        assert payload["paused_at"] == archived["paused_at"]

    def test_unknown_job_emits_nothing(self, store, bus):
        assert J.resume_job("no-such-job", caller="test:resume") is None
        assert bus.query(event_type=EventType.CRON_RESUMED) == []

    def test_anonymous_caller_warns(self, store, caplog):
        """The warn path survives, but only where it is still reachable.

        As of 2026-08-26 an anonymous resume of a pause that carries a
        ``paused_reason`` is REFUSED outright rather than warned about (see
        tests/cron/test_resume_barrier.py), so this pause deliberately states
        no reason. A pause that says nothing has no stated condition to
        protect, and warning is still the right answer there: attribution is
        worth having even when nothing is being gated.
        """
        job = _mk()
        J.pause_job(job["id"], caller="test:pause")
        with caplog.at_level("WARNING", logger="cron.jobs"):
            J.resume_job(job["id"])
        assert "resume_job called anonymously" in caplog.text

    def test_a_refused_resume_emits_nothing(self, store, bus):
        """A past one-shot raises before any write. No write, no event."""
        job = _mk(name="oneshot")
        J.pause_job(job["id"], reason="x", caller="test:pause")
        # parse_schedule refuses a past one-shot at create time, so make the
        # row stale by hand -- which is also how it happens in production
        # (the timestamp was future when the job was created).
        past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        rows = J.load_jobs()
        for row in rows:
            if row["id"] == job["id"]:
                row["schedule"] = {"kind": "once", "run_at": past}
        J.save_jobs(rows)

        with pytest.raises(ValueError, match="Cannot resume"):
            J.resume_job(job["id"], caller="test:resume")

        assert bus.query(event_type=EventType.CRON_RESUMED) == []

    def test_a_bus_failure_does_not_break_the_resume(self, store, monkeypatch, caplog):
        def _boom():
            raise RuntimeError("bus is down")

        job = _mk()
        J.pause_job(job["id"], reason="x", caller="test:pause")
        monkeypatch.setattr(J, "_get_event_bus", _boom)

        with caplog.at_level("ERROR", logger="cron.jobs"):
            updated = J.resume_job(job["id"], caller="test:resume")

        assert updated is not None
        assert updated["state"] == "scheduled"
        assert J.get_job(job["id"])["enabled"] is True
        assert "cron_resumed emit failed" in caplog.text


# ---------------------------------------------------------------------------
# THE IMPLICIT UN-PAUSE
# ---------------------------------------------------------------------------


class TestTriggerJobUnpause:
    def test_triggering_a_paused_job_records_the_resume(self, store, bus):
        job = _mk()
        J.pause_job(job["id"], reason="contained", caller="test:pause")
        paused_at = J.get_job(job["id"])["paused_at"]

        J.trigger_job(job["id"], caller="hermes_cli:cron_run", reason="operator override")

        payloads = _payloads(bus, EventType.CRON_RESUMED)
        assert len(payloads) == 1
        assert payloads[0]["caller"] == "hermes_cli:cron_run"
        # The pause's reason, not the trigger's — the two are different facts
        # and the trigger's own reason already rides on CRON_TRIGGERED.
        assert payloads[0]["reason"] == "contained"
        assert payloads[0]["paused_at"] == paused_at
        assert payloads[0]["previous_state"] == "paused"

    def test_triggering_a_running_job_records_no_resume(self, store, bus):
        """Only a job that WAS paused gets a resume. Otherwise every routine
        `cron run` would manufacture a resume event for a pause that never
        happened, and the pause/resume pairing would stop meaning anything."""
        job = _mk()
        J.trigger_job(job["id"], caller="test:run", reason="just run it")

        assert bus.query(event_type=EventType.CRON_RESUMED) == []
        assert len(bus.query(event_type=EventType.CRON_TRIGGERED)) == 1

    def test_triggering_a_disabled_job_records_the_resume(self, store, bus):
        """``enabled: False`` without ``state: "paused"`` is the same
        operator-visible transition — trigger_job revives it either way."""
        job = _mk()
        J.update_job(job["id"], {"enabled": False})

        J.trigger_job(job["id"], caller="test:run")

        assert len(bus.query(event_type=EventType.CRON_RESUMED)) == 1

    def test_a_legacy_record_without_enabled_is_not_reported_as_resumed(self, store):
        """``job.get("enabled", True)`` matches the due scan's default.

        A record predating the ``enabled`` key is runnable, so treating a
        missing key as "disabled" would emit a spurious resume on every
        trigger of such a record.
        """
        assert J._is_paused({"state": "scheduled"}) is False
        assert J._is_paused({}) is False
        assert J._is_paused({"enabled": False}) is True
        assert J._is_paused({"state": "paused"}) is True

    def test_resume_is_emitted_before_the_trigger(self, store, monkeypatch):
        """Cause order: the job came out of its pause, then it was scheduled.

        Read the other way round, a CRON_TRIGGERED preceding its CRON_RESUMED
        says a paused job was fired and only afterwards un-paused.
        """
        order = []
        monkeypatch.setattr(
            J, "emit_cron_lifecycle_safe", lambda **kw: order.append(kw["action"])
        )
        monkeypatch.setattr(
            J, "emit_cron_triggered_safe", lambda **kw: order.append("triggered")
        )

        job = _mk()
        J.pause_job(job["id"], reason="x", caller="test:pause")
        order.clear()
        J.trigger_job(job["id"], caller="test:run")

        assert order == ["resumed", "triggered"]


# ---------------------------------------------------------------------------
# BULK CONTAINMENT CAS
# ---------------------------------------------------------------------------


def _digest(rows):
    raw = json.dumps(
        rows, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _row(job_id, name, *, enabled=True, state="scheduled", **extra):
    return {
        "id": job_id,
        "name": name,
        "enabled": enabled,
        "state": state,
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "prompt": f"run {name}",
        **extra,
    }


@pytest.fixture
def dispatch_barrier(tmp_path):
    from jobflow_dispatch.quarantine_control import QuarantineControlStore

    control = QuarantineControlStore(tmp_path / "control.db")
    with control.acquire_dispatch_barrier(reason="test lifecycle CAS") as barrier:
        yield barrier


class TestBulkCasEmits:
    """``pause_jobs_cas``/``restore_jobs_cas`` have no production caller yet.

    They are not dead API — they are the scheduler's bulk containment path,
    gated on a retained ``DispatchBarrier`` capability, defined and tested
    against a future consumer. Which makes now the cheapest possible moment to
    make attribution mandatory: ``caller`` is a REQUIRED keyword here, unlike
    ``pause_job``'s optional one, because there is no back-compat to keep and
    a containment sweep that pauses eight rows at once is exactly the shape
    that was unattributable on 2026-08-24/25.
    """

    def test_bulk_pause_emits_one_event_per_row_that_actually_changed(
        self, store, bus, dispatch_barrier
    ):
        alpha = _row("a1", "alpha")
        already = _row("b1", "beta", enabled=False, state="paused")
        J.save_jobs([copy.deepcopy(alpha), copy.deepcopy(already)])

        before = J.snapshot_jobs_by_name(("alpha", "beta"))
        result = J.pause_jobs_cas(
            ["alpha", "beta"],
            _digest(before),
            reason="containment",
            dispatch_barrier=dispatch_barrier,
            caller="jobflow:quarantine",
        )

        assert result["changed_job_ids"] == ["a1"]
        payloads = _payloads(bus, EventType.CRON_PAUSED)
        assert len(payloads) == 1, "an already-paused row is not a transition"
        assert payloads[0]["job_id"] == "a1"
        assert payloads[0]["caller"] == "jobflow:quarantine"
        assert payloads[0]["reason"] == "containment"
        assert payloads[0]["previous_state"] == "scheduled"
        assert payloads[0]["new_state"] == "paused"

    def test_bulk_pause_refuses_an_empty_caller(self, store, dispatch_barrier):
        alpha = _row("a1", "alpha")
        J.save_jobs([copy.deepcopy(alpha)])
        before = J.snapshot_jobs_by_name(("alpha",))

        with pytest.raises(ValueError, match="caller"):
            J.pause_jobs_cas(
                ["alpha"], _digest(before), reason="containment",
                dispatch_barrier=dispatch_barrier, caller="   ",
            )
        assert J.snapshot_jobs_by_name(("alpha",)) == before

    def test_a_refused_bulk_pause_emits_nothing(self, store, bus, dispatch_barrier):
        alpha = _row("a1", "alpha")
        J.save_jobs([copy.deepcopy(alpha)])

        with pytest.raises(ValueError, match="digest"):
            J.pause_jobs_cas(
                ["alpha"], "0" * 64, reason="containment",
                dispatch_barrier=dispatch_barrier, caller="jobflow:quarantine",
            )
        assert bus.query(event_type=EventType.CRON_PAUSED) == []

    def test_bulk_restore_emits_only_for_rows_it_actually_un_pauses(
        self, store, bus, dispatch_barrier
    ):
        """A restore may legally replace a paused row with another paused row.

        Rolling back to a still-contained snapshot is a valid target, and the
        containment fields are the only ones allowed to differ. Emitting a
        resume for it would make the CRON_PAUSED/CRON_RESUMED pairing
        symmetric but untrue.
        """
        alpha_live = _row("a1", "alpha")
        alpha_paused = _row(
            "a1", "alpha", enabled=False, state="paused",
            paused=True, paused_at="2026-08-24T18:07:04+00:00",
            paused_reason="containment",
        )
        beta_paused = _row(
            "b1", "beta", enabled=False, state="paused",
            paused=True, paused_at="2026-08-24T18:07:04+00:00",
            paused_reason="containment",
        )
        # beta's restore target stays paused, only its reason changes.
        beta_still_paused = copy.deepcopy(beta_paused)
        beta_still_paused["paused_reason"] = "still containing"

        J.save_jobs([copy.deepcopy(alpha_paused), copy.deepcopy(beta_paused)])

        J.restore_jobs_cas(
            expected_paused_rows=[alpha_paused, beta_paused],
            target_rows=[alpha_live, beta_still_paused],
            dependency_order=["alpha", "beta"],
            dispatch_barrier=dispatch_barrier,
            caller="jobflow:quarantine",
        )

        payloads = _payloads(bus, EventType.CRON_RESUMED)
        assert [p["job_id"] for p in payloads] == ["a1"]
        assert payloads[0]["caller"] == "jobflow:quarantine"
        assert payloads[0]["reason"] == "containment"
        assert payloads[0]["paused_at"] == "2026-08-24T18:07:04+00:00"
        assert payloads[0]["previous_state"] == "paused"
        assert payloads[0]["new_state"] == "scheduled"

    def test_bulk_restore_refuses_an_empty_caller(self, store, dispatch_barrier):
        paused = _row("a1", "alpha", enabled=False, state="paused")
        J.save_jobs([copy.deepcopy(paused)])

        with pytest.raises(ValueError, match="caller"):
            J.restore_jobs_cas(
                expected_paused_rows=[paused],
                target_rows=[_row("a1", "alpha")],
                dependency_order=["alpha"],
                dispatch_barrier=dispatch_barrier,
                caller="",
            )


# ---------------------------------------------------------------------------
# THE EMITTER ITSELF
# ---------------------------------------------------------------------------


class TestEmitter:
    def test_an_unknown_action_is_logged_rather_than_silently_dropped(
        self, store, bus, caplog
    ):
        """A caller-side typo must not become a missing audit record.

        The emitter maps action -> EventType. A miss with no log would leave
        the exact failure mode this whole feature exists to prevent: a
        transition that happened and left nothing behind.
        """
        from events.producers import cron_lifecycle_emitter as E

        with caplog.at_level("ERROR", logger=E.__name__):
            result = E.emit_cron_lifecycle(
                bus, action="unpaused", job_id="a1", job_name="alpha",
                caller="test", reason=None, paused_at=None,
                previous_state=None, new_state=None,
            )

        assert result is None
        assert "unknown action" in caplog.text
        assert bus.query(event_type=EventType.CRON_PAUSED) == []
        assert bus.query(event_type=EventType.CRON_RESUMED) == []

    def test_a_pause_payload_carries_no_next_run_at_key(self, store, bus):
        """next_run_at is a resume-only field. A paused job has no next run,
        and a null key invites a reader to treat "paused" as "runs at null"."""
        from events.producers import cron_lifecycle_emitter as E

        E.emit_cron_lifecycle(
            bus, action="paused", job_id="a1", job_name="alpha",
            caller="test", reason="why", paused_at="2026-08-25T00:00:00+00:00",
            previous_state="scheduled", new_state="paused",
        )
        assert "next_run_at" not in _payloads(bus, EventType.CRON_PAUSED)[0]

    def test_a_bus_emit_failure_is_swallowed_and_logged(self, store, caplog):
        from events.producers import cron_lifecycle_emitter as E

        class _BrokenBus:
            def emit(self, **kwargs):
                raise RuntimeError("disk full")

        with caplog.at_level("ERROR", logger=E.__name__):
            result = E.emit_cron_lifecycle(
                _BrokenBus(), action="paused", job_id="a1", job_name="alpha",
                caller="test", reason=None, paused_at=None,
                previous_state=None, new_state=None,
            )
        assert result is None
        assert "emit failed" in caplog.text


# ---------------------------------------------------------------------------
# THE CLAIM IN THE DOCSTRING
# ---------------------------------------------------------------------------


_EMIT_PATHS = (
    "pause_job",
    "resume_job",
    "trigger_job",
    "pause_jobs_cas",
    "restore_jobs_cas",
)


class TestEveryPathClaim:
    """``emit_cron_lifecycle_safe``'s docstring enumerates five call sites.

    The trigger emitter's docstring made the same "shared by every path" claim
    while two of its three paths emitted nothing, and two investigations read
    that sentence, found no events, and concluded provenance was unobtainable
    (see tests/cron/test_offschedule_provenance.py). Pin the sentence so it
    cannot go false again silently.
    """

    def test_the_docstring_names_every_path(self):
        doc = J.emit_cron_lifecycle_safe.__doc__ or ""
        for site in _EMIT_PATHS:
            assert site in doc, f"{site} missing from the emit-path table"

    def test_every_named_path_actually_calls_the_emitter(self):
        fns = (
            J.pause_job, J.resume_job, J._trigger_job_admitted,
            J.pause_jobs_cas, J.restore_jobs_cas,
        )
        assert len(fns) == len(_EMIT_PATHS)
        for fn in fns:
            assert "emit_cron_lifecycle_safe(" in inspect.getsource(fn), (
                f"{fn.__name__} does not emit a lifecycle event"
            )

    def test_update_job_deliberately_does_not(self):
        """The shared writer stays out of it.

        Every caller that moves a lifecycle field already knows which
        transition it is making. Emitting from ``update_job`` would mean
        inferring the transition from a field diff, and would fire on writes
        that change no state at all.
        """
        assert "emit_cron_lifecycle_safe(" not in inspect.getsource(J.update_job)

    def test_request_run_deliberately_does_not(self):
        """``request_run`` never enables and writes no lifecycle field.

        Its docstring makes "this cannot change operator-visible state" an
        assertable property; emitting a lifecycle event from it would be a
        record of a transition that did not occur.
        """
        assert "emit_cron_lifecycle_safe(" not in inspect.getsource(
            J._request_run_admitted
        )


# ---------------------------------------------------------------------------
# THE FILE THE INVESTIGATIONS ACTUALLY GREPPED
# ---------------------------------------------------------------------------


class TestAuditTrail:
    def test_a_pause_and_its_resume_land_in_audit_jsonl(self, store, bus, tmp_path):
        """``audit.jsonl`` is the file both sessions searched and came up empty.

        ``AuditLogger`` filters no event types, so wiring the emit to the bus
        is sufficient by construction — but "sufficient" is a claim about a
        subscriber this change never touched, and the whole reason this gap
        cost two investigations is that everyone assumed the trail existed.
        Prove the record actually reaches the file, end to end.
        """
        from events.subscribers.audit_logger import AuditLogger

        audit_path = tmp_path / "audit" / "audit.jsonl"
        sub = AuditLogger(bus, audit_path=audit_path)
        _seed_cursor_at_zero(bus, sub.subscriber_id)

        job = _mk(name="tracker-operator-drain")
        J.pause_job(
            job["id"], reason="jaum backlog", caller="hermes_cli:cron_pause"
        )
        J.resume_job(job["id"], caller="hermes_cli:cron_resume")
        sub.poll()

        records = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        by_type = {r["event_type"]: r for r in records}

        assert "cron_paused" in by_type
        assert "cron_resumed" in by_type
        paused = by_type["cron_paused"]
        assert paused["payload"]["caller"] == "hermes_cli:cron_pause"
        assert paused["payload"]["reason"] == "jaum backlog"
        assert paused["payload"]["job_name"] == "tracker-operator-drain"
        assert paused["job_id"] == job["id"]
        assert by_type["cron_resumed"]["payload"]["caller"] == "hermes_cli:cron_resume"


# ---------------------------------------------------------------------------
# SCHEMA + ROUTING PAIRING
# ---------------------------------------------------------------------------


class TestSchemaAndRouting:
    def test_both_types_round_trip_through_from_string(self):
        assert EventType.from_string("cron_paused") is EventType.CRON_PAUSED
        assert EventType.from_string("cron_resumed") is EventType.CRON_RESUMED

    def test_both_route_to_the_cron_firehose_as_trace(self):
        """Adding an EventType is not a one-file change — see events/coverage.py.

        An unmapped type does not raise; ``classify()`` degrades to
        WARN-on-watchdog_alerts, which would page the operator's phone lane
        for what is pure telemetry.
        """
        from events.routing_policy import Attention, _POLICY

        for et in (EventType.CRON_PAUSED, EventType.CRON_RESUMED):
            spec = _POLICY[et]
            assert spec.attention is Attention.TRACE
            assert spec.topic_key == "cron_firehose"

    def test_icons_are_disjoint_within_the_cron_firehose_topic(self):
        """The uniqueness standard is per-topic, not global.

        CRON_RESUMED deliberately does not reuse CRON_STARTED's play glyph:
        both land in cron_firehose, where an operator scans them side by side.
        """
        from events.routing_policy import _POLICY

        same_topic = [
            et for et, spec in _POLICY.items() if spec.topic_key == "cron_firehose"
        ]
        icons = [et.icon for et in same_topic]
        assert len(icons) == len(set(icons))
        assert EventType.CRON_PAUSED.icon != EventType.CRON_RESUMED.icon
