"""Tests for CronStaleMonitor subscriber (SR-106).

The monitor tracks cron_started events that haven't been matched by
cron_completed / cron_failed within a threshold window, and emits a
single cron_stale event per detection (no spam).
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from events.bus import EventBus
from events.schema import EventType
from events.subscribers.cron_stale_monitor import CronStaleMonitor

# The deployed production thresholds, relative to the Hermes root.
# `notifications/` is a canonical `~/.hermes` root directory — it lives in the
# *outer* Hermes checkout, not in `agent-src`.
_THRESHOLDS_REL = Path("notifications") / "cron_stale_thresholds.json"


def _find_hermes_root() -> Path | None:
    """Return the Hermes root (the checkout holding `notifications/`), or None.

    Searches upward from this file for the directory that actually contains
    the thresholds file. Deliberately *not* a fixed count of `.parent` hops:
    this file sits three levels deeper inside a git worktree
    (`agent-src/.claude/worktrees/<name>/tests/events/subscribers/`) than in
    the main checkout (`agent-src/tests/events/subscribers/`), so any fixed
    count that escapes the checkout is right in exactly one of the two
    layouts. The previous `parents[4]` was correct only from the main checkout
    and silently resolved to `agent-src/.claude/worktrees` otherwise.

    Git cannot anchor this either: `rev-parse --show-toplevel` returns the
    *worktree* path rather than `agent-src`, and `events.paths`'
    `get_default_hermes_root()` reads `HERMES_HOME`, which the suite redirects
    to a per-test tempdir (see `tests/conftest.py`) — the very reason this path
    is hand-derived. Searching for the artifact itself is correct at any
    nesting depth and needs neither a subprocess nor the environment.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / _THRESHOLDS_REL).is_file():
            return candidate
    return None


_HERMES_ROOT = _find_hermes_root()
THRESHOLDS_CONFIG = None if _HERMES_ROOT is None else _HERMES_ROOT / _THRESHOLDS_REL

# How far *under* its threshold the "not yet stale" run is backdated in
# test_production_threshold_boundaries.
#
# The monitor reads its own `datetime.now()` inside `_check_stale()`, so the
# age it computes is the backdate plus however long the test takes to reach
# `poll()` — an EventBus emit, a second sqlite connection to rewrite the
# timestamp, a subscriber construction and a cursor write. A 1-second margin
# (the original) lost that race whenever the box was loaded: the per-file
# parallel harness reproduced it on `nightly-test-gate`, where the monitor saw
# age_seconds=3600 against threshold=3600 and fired. Nothing about the
# assertion was wrong — the input drifted past the boundary before it was read.
#
# 120s keeps ~100x headroom over the observed latency while still discriminating
# the per-job override from the 1200s default: every override under test is
# 1800s or more, so `threshold - 120` stays above 1200 and a monitor that
# ignored the override would still alert here and fail the test.
_WITHIN_MARGIN_SECONDS = 120

# Only the production-threshold test needs the deployed file; every other test
# in this module is hermetic, so this skips that one test rather than the whole
# module.
requires_thresholds_config = pytest.mark.skipif(
    THRESHOLDS_CONFIG is None,
    reason=(
        f"Hermes root not found: no ancestor of {Path(__file__).resolve()} "
        f"contains {_THRESHOLDS_REL.as_posix()}. The deployed thresholds live "
        "in the outer ~/.hermes checkout, which is absent from this tree."
    ),
)


@pytest.fixture
def bus(tmp_path):
    db = tmp_path / "event_bus.db"
    b = EventBus(db_path=db)
    yield b
    b.close()


def _stale_events(bus):
    return [e for e in bus.query() if e.event_type == EventType.CRON_STALE]


def _emit_started(bus, job_id: str, started_at: datetime | None = None) -> str:
    """Emit cron_started. If started_at given, rewrite the stored timestamp
    so we can simulate an old event without waiting."""
    eid = bus.emit(
        event_type=EventType.CRON_STARTED,
        source="scheduler",
        payload={"job_id": job_id, "job_name": job_id, "schedule": "* * * * *"},
    )
    if started_at is not None:
        # Backdate the stored row to simulate elapsed time.
        import sqlite3
        conn = sqlite3.connect(str(bus.db_path))
        conn.execute(
            "UPDATE events SET timestamp = ? WHERE event_id = ?",
            (started_at.isoformat(), eid),
        )
        conn.commit()
        conn.close()
    return eid


def _monitor(bus):
    """Construct a CronStaleMonitor and read from the start of the bus.

    These tests emit cron_started/_completed BEFORE constructing the monitor,
    so force the cursor to 0 (the construction-time seed lands at head, past the
    already-emitted events). See events/subscribers/base.py.
    """
    m = CronStaleMonitor(bus)
    bus._execute(
        "INSERT OR REPLACE INTO subscriber_cursors "
        "(subscriber_id, last_rowid, updated_at) VALUES (?, 0, datetime('now'))",
        (m.subscriber_id,),
    )
    return m


class TestCronStaleMonitor:
    def test_started_then_completed_does_not_alert(self, bus):
        _emit_started(bus, "job-a")
        bus.emit(EventType.CRON_COMPLETED, "scheduler",
                 {"job_id": "job-a", "job_name": "job-a", "duration": 1.2})

        mon = _monitor(bus)
        mon.poll()

        assert _stale_events(bus) == []

    def test_started_only_within_threshold_does_not_alert(self, bus):
        _emit_started(bus, "job-b")
        mon = _monitor(bus)
        mon.poll()

        assert _stale_events(bus) == []

    def test_started_only_past_threshold_emits_stale(self, bus):
        old = datetime.now(timezone.utc) - timedelta(
            seconds=CronStaleMonitor.STALE_THRESHOLD_SECONDS + 30)
        _emit_started(bus, "job-c", started_at=old)

        mon = _monitor(bus)
        mon.poll()

        stale = _stale_events(bus)
        assert len(stale) == 1
        assert stale[0].payload["job_id"] == "job-c"
        assert stale[0].payload["age_seconds"] >= CronStaleMonitor.STALE_THRESHOLD_SECONDS

    def test_does_not_double_alert_same_stale_job(self, bus):
        old = datetime.now(timezone.utc) - timedelta(
            seconds=CronStaleMonitor.STALE_THRESHOLD_SECONDS + 30)
        _emit_started(bus, "job-d", started_at=old)

        mon = _monitor(bus)
        mon.poll()
        mon.poll()
        mon.poll()

        assert len(_stale_events(bus)) == 1

    def test_completion_clears_alert_state(self, bus):
        """After a stale alert, if the job finally completes and a new run
        also goes stale, we alert again (not permanently silenced)."""
        old = datetime.now(timezone.utc) - timedelta(
            seconds=CronStaleMonitor.STALE_THRESHOLD_SECONDS + 30)
        _emit_started(bus, "job-e", started_at=old)

        mon = _monitor(bus)
        mon.poll()
        assert len(_stale_events(bus)) == 1

        bus.emit(EventType.CRON_COMPLETED, "scheduler",
                 {"job_id": "job-e", "job_name": "job-e", "duration": 601.0})
        mon.poll()

        _emit_started(bus, "job-e", started_at=old)
        mon.poll()

        assert len(_stale_events(bus)) == 2

    def test_failed_also_clears_open_state(self, bus):
        _emit_started(bus, "job-f")
        bus.emit(EventType.CRON_FAILED, "scheduler",
                 {"job_id": "job-f", "job_name": "job-f",
                  "duration": 0.1, "error": "boom", "consecutive_errors": 1})

        mon = _monitor(bus)
        mon.poll()

        assert _stale_events(bus) == []

    def test_multiple_jobs_tracked_independently(self, bus):
        old = datetime.now(timezone.utc) - timedelta(
            seconds=CronStaleMonitor.STALE_THRESHOLD_SECONDS + 30)
        _emit_started(bus, "job-g", started_at=old)
        _emit_started(bus, "job-h")  # fresh

        mon = _monitor(bus)
        mon.poll()

        stale = _stale_events(bus)
        assert len(stale) == 1
        assert stale[0].payload["job_id"] == "job-g"

    def test_missing_job_id_payload_is_ignored(self, bus):
        """Defensive: a CRON_STARTED without job_id in payload should not crash."""
        bus.emit(EventType.CRON_STARTED, "scheduler", {})  # no job_id

        mon = _monitor(bus)
        mon.poll()  # must not raise

        assert _stale_events(bus) == []

    @requires_thresholds_config
    @pytest.mark.parametrize(
        ("job_name", "threshold"),
        [
            ("jobflow-tracker-cycle", 2100),
            ("jobflow-tracker-followup", 2400),
            ("nightly-test-gate", 3600),
            ("postgres-sync", 1800),
        ],
    )
    def test_production_threshold_boundaries(self, bus, job_name, threshold):
        config = json.loads(THRESHOLDS_CONFIG.read_text(encoding="utf-8"))

        assert config["default_seconds"] == 1200
        assert config["per_job"][job_name] == threshold

        within = datetime.now(timezone.utc) - timedelta(
            seconds=threshold - _WITHIN_MARGIN_SECONDS
        )
        _emit_started(bus, job_name, started_at=within)
        mon = CronStaleMonitor(
            bus,
            default_threshold_seconds=config["default_seconds"],
            per_job_thresholds=config["per_job"],
        )
        bus._execute(
            "INSERT OR REPLACE INTO subscriber_cursors "
            "(subscriber_id, last_rowid, updated_at) VALUES (?, 0, datetime('now'))",
            (mon.subscriber_id,),
        )
        mon.poll()
        assert _stale_events(bus) == []

        past = datetime.now(timezone.utc) - timedelta(seconds=threshold + 1)
        _emit_started(bus, job_name, started_at=past)
        mon.poll()

        stale = _stale_events(bus)
        assert len(stale) == 1
        assert stale[0].payload["job_name"] == job_name
        assert stale[0].payload["threshold_seconds"] == threshold


# ---------------------------------------------------------------------------
# Ticker-heartbeat watchdog
#
# Regression cover for the 2026-08-11 silent scheduler outage. The cron-scheduler
# thread died at startup and the gateway ran 5h08m with NO scheduler. This
# subscriber could not see it: it alerts only on a job that STARTED and never
# finished, and a dead scheduler emits zero cron_started — so `_open_jobs` stayed
# empty and `_check_stale()` had nothing to check. It was a dead-man's-switch
# driven by the very events that had stopped.
#
# `cron.jobs.record_ticker_heartbeat()` already wrote the one signal that proves
# the outage (the heartbeat file ages without bound once the thread is gone), but
# nothing polled it in the background — only interactive `hermes cron status`
# read it. These tests wire that signal into the subscriber that already owns
# cron-health alerting.
# ---------------------------------------------------------------------------

def _patch_age(monkeypatch, value):
    """Force cron.jobs.get_ticker_heartbeat_age() to return ``value``."""
    import cron.jobs
    monkeypatch.setattr(cron.jobs, "get_ticker_heartbeat_age", lambda: value)


def test_stale_ticker_heartbeat_emits_cron_stale(bus, monkeypatch):
    """A heartbeat older than the threshold means the ticker thread is gone."""
    mon = _monitor(bus)
    _patch_age(monkeypatch, mon.TICKER_STALE_THRESHOLD_SECONDS + 60)

    mon.poll()

    stale = _stale_events(bus)
    assert len(stale) == 1, "a dead ticker did not raise cron_stale"
    assert stale[0].payload["scope"] == "ticker", \
        "ticker alert must be distinguishable from a stuck-job alert"
    assert stale[0].payload["age_seconds"] >= mon.TICKER_STALE_THRESHOLD_SECONDS


def test_fresh_ticker_heartbeat_is_silent(bus, monkeypatch):
    """A ticker beating normally must never alert."""
    mon = _monitor(bus)
    _patch_age(monkeypatch, 5.0)

    mon.poll()

    assert _stale_events(bus) == []


def test_unknown_ticker_heartbeat_age_is_silent(bus, monkeypatch):
    """None = 'cannot determine' (older build / never ran / torn read), not dead.

    get_ticker_heartbeat_age()'s own contract says callers must treat None as
    unknown. Alerting here would page on every fresh install.
    """
    mon = _monitor(bus)
    _patch_age(monkeypatch, None)

    mon.poll()

    assert _stale_events(bus) == []


def test_stale_ticker_alerts_once_then_rearms_after_recovery(bus, monkeypatch):
    """One alert per outage — but a NEW outage after recovery alerts again."""
    mon = _monitor(bus)

    _patch_age(monkeypatch, mon.TICKER_STALE_THRESHOLD_SECONDS + 60)
    mon.poll()
    mon.poll()
    assert len(_stale_events(bus)) == 1, "ticker alert spammed while still stale"

    # Ticker recovers — this must re-arm the alert.
    _patch_age(monkeypatch, 1.0)
    mon.poll()
    assert len(_stale_events(bus)) == 1

    # A second, separate outage must alert again.
    _patch_age(monkeypatch, mon.TICKER_STALE_THRESHOLD_SECONDS + 60)
    mon.poll()
    assert len(_stale_events(bus)) == 2, "a new outage after recovery did not alert"


def test_ticker_check_failure_does_not_break_job_staleness_check(bus, monkeypatch):
    """The watchdog must never take down the subscriber it lives in."""
    import cron.jobs

    def _boom():
        raise OSError("heartbeat file unreadable")

    mon = _monitor(bus)
    monkeypatch.setattr(cron.jobs, "get_ticker_heartbeat_age", _boom)

    mon.poll()  # must not raise

    assert _stale_events(bus) == []


# =========================================================================
# GATEWAY_STOPPED resolution (2026-08-16)
#
# gateway/run.py stamps every GATEWAY_STOPPED with the cron_started event_ids
# of the runs it is about to kill (cron/inflight.py). Before this, a cron cut
# short by a restart was indistinguishable from one that wedged: the monitor
# kept it in _open_jobs and fired a generic HIGH-priority cron_stale ~20
# minutes later. These pin the correct attribution and the suppression.
# =========================================================================

def _emit_gateway_stopped(bus, correlation_ids, exit_reason="graceful"):
    return bus.emit(
        event_type=EventType.GATEWAY_STOPPED,
        source="gateway",
        payload={
            "exit_reason": exit_reason,
            "inflight_cron_correlation_ids": list(correlation_ids),
        },
    )


class TestGatewayStoppedResolution:
    def test_resolves_the_inflight_job_and_attributes_the_shutdown(self, bus):
        started_id = _emit_started(bus, "nightly-audit")
        _emit_gateway_stopped(bus, [started_id])
        mon = _monitor(bus)

        mon.poll()
        mon.shutdown()  # the flush point — see TestShutdownAttributionTiming

        evts = _stale_events(bus)
        assert len(evts) == 1, "the killed run must be reported exactly once"
        payload = evts[0].payload
        assert payload["job_id"] == "nightly-audit"
        assert payload["scope"] == "gateway_stopped", (
            "must be distinguishable from a wedge — mirrors the existing "
            "scope='ticker' idiom"
        )
        assert payload["exit_reason"] == "graceful"
        assert "nightly-audit" not in mon._open_jobs, "entry must be resolved"

    def test_shutdown_report_is_not_a_high_priority_wedge_alarm(self, bus):
        """A run killed by a deliberate restart is explained, not an emergency.
        The generic wedge alert is HIGH; this one must be quieter."""
        from events.schema import Priority

        started_id = _emit_started(bus, "scout")
        _emit_gateway_stopped(bus, [started_id])
        mon = _monitor(bus)

        mon.poll()
        mon.shutdown()

        evts = _stale_events(bus)
        assert len(evts) == 1
        assert evts[0].priority == Priority.NORMAL

    def test_suppresses_the_later_generic_stale_alert(self, bus):
        """The whole point: without this, the same run ALSO fires a generic
        HIGH cron_stale once the threshold elapses — a false 'wedged' alarm
        for a job the gateway itself killed."""
        old = datetime.now(timezone.utc) - timedelta(seconds=99999)
        started_id = _emit_started(bus, "long-job", started_at=old)
        _emit_gateway_stopped(bus, [started_id])
        mon = _monitor(bus)

        mon.poll()
        mon.poll()  # a second sweep must not produce the generic alert
        mon.shutdown()

        evts = _stale_events(bus)
        assert len(evts) == 1, f"expected exactly one report, got {[e.payload for e in evts]}"
        assert evts[0].payload["scope"] == "gateway_stopped"

    def test_is_processed_even_though_it_carries_no_job_id(self, bus):
        """Regression pin: handle() early-returns when payload has no job_id,
        and GATEWAY_STOPPED has none. The shutdown branch must run BEFORE that
        guard, or this feature is silently dead."""
        started_id = _emit_started(bus, "job-x")
        eid = _emit_gateway_stopped(bus, [started_id])
        stopped = [e for e in bus.query() if e.event_id == eid][0]
        assert "job_id" not in stopped.payload

        mon = _monitor(bus)
        mon.poll()
        mon.shutdown()

        assert len(_stale_events(bus)) == 1

    def test_unknown_correlation_id_is_silent(self, bus):
        _emit_started(bus, "job-a")
        _emit_gateway_stopped(bus, ["evt-never-seen"])
        mon = _monitor(bus)

        mon.poll()

        assert _stale_events(bus) == []
        assert "job-a" in mon._open_jobs, "an unrelated open job must be left alone"

    def test_empty_or_missing_inflight_list_is_a_noop(self, bus):
        _emit_started(bus, "job-a")
        _emit_gateway_stopped(bus, [])
        bus.emit(
            event_type=EventType.GATEWAY_STOPPED,
            source="gateway",
            payload={"exit_reason": "restart"},  # key absent entirely
        )
        mon = _monitor(bus)

        mon.poll()

        assert _stale_events(bus) == []
        assert "job-a" in mon._open_jobs

    def test_completed_job_is_not_resolved_by_a_later_shutdown(self, bus):
        """A run that finished normally is not 'killed by the shutdown'. Its
        correlation id must stop resolving once a terminal event arrives."""
        started_id = _emit_started(bus, "job-done")
        bus.emit(
            event_type=EventType.CRON_COMPLETED,
            source="scheduler",
            payload={"job_id": "job-done", "job_name": "job-done"},
        )
        _emit_gateway_stopped(bus, [started_id])
        mon = _monitor(bus)

        mon.poll()

        assert _stale_events(bus) == []

    def test_a_restart_resolves_only_the_runs_it_killed(self, bus):
        started_a = _emit_started(bus, "job-killed")
        _emit_started(bus, "job-still-running")
        _emit_gateway_stopped(bus, [started_a])
        mon = _monitor(bus)

        mon.poll()
        mon.shutdown()

        evts = _stale_events(bus)
        assert len(evts) == 1
        assert evts[0].payload["job_id"] == "job-killed"
        assert "job-killed" not in mon._open_jobs
        assert "job-still-running" in mon._open_jobs


# =========================================================================
# Snapshot timing (2026-08-17)
#
# gateway/run.py takes the inflight snapshot EARLY in _stop_impl_body, before
# the gateway drains its in-flight work — and it has to stay there, because a
# teardown that gets force-killed past _TASKKILL_TIMEOUT_S would otherwise emit
# no GATEWAY_STOPPED at all. So "in flight when the stop began" is NOT the same
# claim as "killed by the stop": a run can still finish while the gateway tears
# down. Production, 2026-08-17: jobflow-researcher was reported killed at
# 05:31:30 and emitted cron_completed at 05:32:05, 35s later.
#
# The monitor therefore STAGES the attribution when it sees the shutdown and
# emits at shutdown() — the last moment before the process exits, and the first
# moment the answer is final.
# =========================================================================

class TestShutdownAttributionTiming:
    def test_attribution_is_deferred_to_shutdown(self, bus):
        started_id = _emit_started(bus, "job-killed")
        _emit_gateway_stopped(bus, [started_id])
        mon = _monitor(bus)

        mon.poll()
        assert _stale_events(bus) == [], (
            "attributing at poll time asserts the run is dead while the "
            "gateway is still draining it"
        )

        mon.shutdown()

        evts = _stale_events(bus)
        assert len(evts) == 1
        assert evts[0].payload["scope"] == "gateway_stopped"
        assert evts[0].payload["job_id"] == "job-killed"

    def test_a_run_that_finishes_during_teardown_is_not_reported_killed(self, bus):
        """The defect this exists to fix. The terminal event arrives in a LATER
        poll than the shutdown event, which is exactly how it happened live."""
        started_id = _emit_started(bus, "job-finishes-late")
        _emit_gateway_stopped(bus, [started_id])
        mon = _monitor(bus)

        mon.poll()  # sees the shutdown; the run is still going

        bus.emit(
            event_type=EventType.CRON_COMPLETED,
            source="scheduler",
            payload={"job_id": "job-finishes-late", "job_name": "job-finishes-late"},
        )
        mon.poll()  # the run lands before teardown finishes
        mon.shutdown()

        assert _stale_events(bus) == [], (
            "a run that completed during teardown was reported shutdown-killed"
        )

    def test_a_run_still_going_at_the_last_moment_is_reported(self, bus):
        """The other half: deferring must not turn into never reporting."""
        started_id = _emit_started(bus, "job-truly-killed")
        _emit_gateway_stopped(bus, [started_id])
        mon = _monitor(bus)

        mon.poll()
        mon.poll()
        mon.shutdown()

        evts = _stale_events(bus)
        assert len(evts) == 1
        assert evts[0].payload["job_id"] == "job-truly-killed"

    def test_the_generic_wedge_alert_is_suppressed_from_the_moment_it_is_seen(self, bus):
        """Suppression must NOT wait for shutdown(): between seeing the
        shutdown and the process exiting there are more polls, and the run is
        already past its threshold."""
        old = datetime.now(timezone.utc) - timedelta(seconds=99999)
        started_id = _emit_started(bus, "long-job", started_at=old)
        _emit_gateway_stopped(bus, [started_id])
        mon = _monitor(bus)

        mon.poll()
        mon.poll()

        # Deferring the ATTRIBUTION must not defer the SUPPRESSION: the run is
        # already 99999s old, so a single unsuppressed _check_stale would page.
        generic = [e for e in _stale_events(bus)
                   if e.payload.get("scope") != "gateway_stopped"]
        assert generic == [], f"the generic HIGH wedge alert leaked: {generic}"
        assert "long-job" not in mon._open_jobs

    def test_flushing_twice_reports_once(self, bus):
        started_id = _emit_started(bus, "job-killed")
        _emit_gateway_stopped(bus, [started_id])
        mon = _monitor(bus)

        mon.poll()
        mon.shutdown()
        mon.shutdown()

        assert len(_stale_events(bus)) == 1

    def test_shutdown_without_a_gateway_stopped_reports_nothing(self, bus):
        """An ordinary in-process restart (startup() calls shutdown() when a bus
        already exists) must not invent attributions for healthy open runs."""
        _emit_started(bus, "job-running")
        mon = _monitor(bus)

        mon.poll()
        mon.shutdown()

        assert _stale_events(bus) == []

    def test_age_is_measured_at_the_shutdown_not_at_the_flush(self, bus):
        """``age_seconds`` answers "how far into the run did the shutdown land".

        Deferring the REPORT must not change what the report SAYS. The field
        predates the deferral — it was computed against the GATEWAY_STOPPED —
        so measuring it at flush time silently redefines it, adding however
        long teardown took. Teardown on this box has run minutes (2026-08-17:
        05:31:22Z stop, the process still emitting at 05:36:48Z), so the drift
        is not a rounding error.

        Self-calibrating rather than absolute: the bound is derived from the
        measured staging window, so a loaded box cannot fail it. cf. the
        _WITHIN_MARGIN_SECONDS note above, where a fixed margin lost that race.
        """
        import time

        BACKDATE_SECONDS = 600
        DEFERRAL_SECONDS = 3  # stands in for the rest of teardown

        old = datetime.now(timezone.utc) - timedelta(seconds=BACKDATE_SECONDS)
        started_id = _emit_started(bus, "job-killed", started_at=old)
        _emit_gateway_stopped(bus, [started_id])
        mon = _monitor(bus)

        before_poll = time.monotonic()
        mon.poll()  # stages the report
        staged_within = time.monotonic() - before_poll
        time.sleep(DEFERRAL_SECONDS)
        mon.shutdown()  # flushes it

        evts = [e for e in _stale_events(bus)
                if e.payload.get("scope") == "gateway_stopped"]
        assert len(evts) == 1
        age = evts[0].payload["age_seconds"]
        # +1 absorbs int() truncation; the deferral must NOT be in there.
        assert age <= BACKDATE_SECONDS + staged_within + 1, (
            f"age_seconds={age} carries the {DEFERRAL_SECONDS}s deferral "
            f"(run was {BACKDATE_SECONDS}s old when the shutdown was seen, "
            f"staged within {staged_within:.2f}s)"
        )

    def test_age_is_measured_at_the_shutdown_not_at_the_poll(self, bus):
        """The other half of the same question: which CLOCK, not which MOMENT.

        Measuring at the staging site fixed the flush drift, but the staging
        site still read ``datetime.now()`` — and staging happens when the
        subscriber POLLS the GATEWAY_STOPPED, not when the gateway stopped.
        ``poll_interval_seconds`` is 60, so the answer carried up to a whole
        poll interval of inflation, and it is read against a 1200s wedge
        threshold. Production, 2026-08-19: runs started 16:00:40Z, gateway
        stopped 16:00:54Z (true age 14s), handled 16:01:48Z — the emitted
        rows said 68.

        Exact, not bounded: both stamps are durable and backdated here, so the
        answer is arithmetic and no amount of box load can move it.
        """
        STARTED_AGO = 900
        STOPPED_AGO = 840  # the shutdown landed 60s into the run
        TRUE_AGE = STARTED_AGO - STOPPED_AGO

        now = datetime.now(timezone.utc)
        started_id = _emit_started(
            bus, "job-killed", started_at=now - timedelta(seconds=STARTED_AGO),
        )
        stopped_id = _emit_gateway_stopped(bus, [started_id])
        _backdate(bus, stopped_id, now - timedelta(seconds=STOPPED_AGO))

        mon = _monitor(bus)
        mon.poll()  # stages, STOPPED_AGO seconds after the shutdown
        mon.shutdown()

        evts = [e for e in _stale_events(bus)
                if e.payload.get("scope") == "gateway_stopped"]
        assert len(evts) == 1
        assert evts[0].payload["age_seconds"] == TRUE_AGE, (
            f"age_seconds={evts[0].payload['age_seconds']} is measured from "
            f"the poll, not from the GATEWAY_STOPPED (true age {TRUE_AGE}s)"
        )

    def test_a_shutdown_stamped_before_the_run_started_reports_zero(self, bus):
        """Clock skew must not produce a negative age.

        ``_age_at_shutdown``, the successor-side path, already clamps; the
        in-process path has to agree with it or the same shutdown reads
        differently depending on which side reported it.
        """
        now = datetime.now(timezone.utc)
        started_id = _emit_started(
            bus, "job-skewed", started_at=now - timedelta(seconds=100),
        )
        stopped_id = _emit_gateway_stopped(bus, [started_id])
        _backdate(bus, stopped_id, now - timedelta(seconds=160))

        mon = _monitor(bus)
        mon.poll()
        mon.shutdown()

        evts = [e for e in _stale_events(bus)
                if e.payload.get("scope") == "gateway_stopped"]
        assert len(evts) == 1
        assert evts[0].payload["age_seconds"] == 0


# =========================================================================
# Successor-side reconstruction (2026-08-17)
#
# The staged report above is flushed in shutdown(), which only runs on a
# GRACEFUL teardown. A gateway force-killed past _TASKKILL_TIMEOUT_S, or cut
# down by the shutdown watchdog's exit_code=1, never reaches it: the staged
# reports die with the process and NOTHING is recorded for runs that genuinely
# were killed. The 2026-08-12 census found six shutdowns started that day and
# three completed — about half.
#
# The successor cannot recover them by handling the predecessor's
# GATEWAY_STOPPED, because _started_event_ids is per-process and the cursor
# seed is INSERT OR IGNORE — a restart never replays the CRON_STARTED rows that
# built the map. Production, 2026-08-17 04:12:03Z: the successor handled it and
# emitted nothing, every correlation id missing from an empty map.
#
# So the successor rebuilds the attribution from the bus in startup(), where
# every input is durable and the answer is already final.
# =========================================================================

def _emit_completed(bus, job_id: str, event_type=None) -> str:
    return bus.emit(
        event_type=event_type or EventType.CRON_COMPLETED,
        source="scheduler",
        payload={"job_id": job_id, "job_name": job_id},
    )


def _backdate(bus, event_id: str, when: datetime) -> None:
    """Rewrite one stored row's timestamp; rowid order is untouched."""
    import sqlite3
    conn = sqlite3.connect(str(bus.db_path))
    conn.execute(
        "UPDATE events SET timestamp = ? WHERE event_id = ?",
        (when.isoformat(), event_id),
    )
    conn.commit()
    conn.close()


def _rowid_of(bus, event_id: str) -> int:
    import sqlite3
    conn = sqlite3.connect(str(bus.db_path))
    try:
        return conn.execute(
            "SELECT rowid FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _cursor_of(bus, subscriber_id: str) -> int:
    import sqlite3
    conn = sqlite3.connect(str(bus.db_path))
    try:
        row = conn.execute(
            "SELECT last_rowid FROM subscriber_cursors WHERE subscriber_id = ?",
            (subscriber_id,),
        ).fetchone()
        return -1 if row is None else row[0]
    finally:
        conn.close()


def _set_cursor(bus, subscriber_id: str, rowid: int) -> None:
    bus._execute(
        "INSERT OR REPLACE INTO subscriber_cursors "
        "(subscriber_id, last_rowid, updated_at) VALUES (?, ?, datetime('now'))",
        (subscriber_id, rowid),
    )


def _successor(bus):
    """A monitor as a NEW gateway process builds it.

    Deliberately NOT ``_monitor()``: the successor's in-memory state is empty
    and its cursor is whatever the predecessor left, which is the entire reason
    handle() cannot resolve these correlation ids.
    """
    return CronStaleMonitor(bus)


def _shutdown_stales(bus):
    return [e for e in _stale_events(bus)
            if e.payload.get("scope") == "gateway_stopped"]


class TestStartupShutdownReconstruction:
    def test_reports_a_run_whose_gateway_was_force_killed(self, bus):
        """The gap itself: no predecessor flush ever happened."""
        from events.schema import Priority

        started_id = _emit_started(bus, "job-killed")
        stopped_id = _emit_gateway_stopped(
            bus, [started_id], exit_reason="force_kill",
        )

        mon = _successor(bus)
        mon.startup()

        evts = _shutdown_stales(bus)
        assert len(evts) == 1, (
            "a run the shutdown killed went unrecorded because the process "
            "died before shutdown() could flush it"
        )
        payload = evts[0].payload
        assert payload["job_id"] == "job-killed"
        assert payload["job_name"] == "job-killed"
        assert payload["exit_reason"] == "force_kill"
        assert payload["cron_started_event_id"] == started_id
        assert payload["gateway_stopped_event_id"] == stopped_id
        assert evts[0].priority == Priority.NORMAL, (
            "a deliberate kill is explained, not paged"
        )

    def test_handling_alone_cannot_do_it_which_is_why_startup_exists(self, bus):
        """Reproduces production 2026-08-17 04:12:03Z.

        The predecessor consumed the cron_started (its cursor advanced past it)
        and died before the gateway_stopped, so the successor's poll() sees the
        shutdown against an empty _started_event_ids map. Nothing is emitted.
        This is the motivation, and it must stay true: the fix is a startup
        query, NOT a cursor rewind that would replay the CRON_STARTED rows.
        """
        started_id = _emit_started(bus, "job-killed")
        _emit_gateway_stopped(bus, [started_id])

        mon = _successor(bus)
        _set_cursor(bus, mon.subscriber_id, _rowid_of(bus, started_id))

        mon.poll()
        mon.shutdown()

        assert _shutdown_stales(bus) == []
        assert mon._started_event_ids == {}

    def test_does_not_double_report_what_the_predecessor_flushed(self, bus):
        """One shutdown, two killed runs, one already reported by the
        predecessor's graceful flush. The successor must add exactly the
        missing one."""
        killed_a = _emit_started(bus, "job-a")
        killed_b = _emit_started(bus, "job-b")

        # The predecessor saw the shutdown and flushed — but only for job-a
        # (it was force-killed partway through, or job-b's correlation id was
        # not in its map). Emitted through the real path, not hand-rolled.
        pred = _monitor(bus)
        pred.poll()
        _emit_gateway_stopped(bus, [killed_a, killed_b])
        pred.poll()
        pred._pending_shutdown = [
            p for p in pred._pending_shutdown if p["job_id"] == "job-a"
        ]
        pred.shutdown()
        assert [e.payload["job_id"] for e in _shutdown_stales(bus)] == ["job-a"]

        mon = _successor(bus)
        mon.startup()

        reported = sorted(e.payload["job_id"] for e in _shutdown_stales(bus))
        assert reported == ["job-a", "job-b"], (
            f"expected job-a kept and job-b added, got {reported}"
        )

    def test_running_twice_reports_once(self, bus):
        """Every boot re-examines the horizon; the bus dedupe is what keeps it
        idempotent, since no watermark is persisted."""
        started_id = _emit_started(bus, "job-killed")
        _emit_gateway_stopped(bus, [started_id])

        _successor(bus).startup()
        _successor(bus).startup()

        assert len(_shutdown_stales(bus)) == 1

    def test_a_run_that_landed_during_teardown_is_not_reported(self, bus):
        """The early snapshot lists runs that can still finish. Evaluated after
        the fact the answer is already known — no deferral needed."""
        landed = _emit_started(bus, "job-finishes-late")
        killed = _emit_started(bus, "job-killed")
        _emit_gateway_stopped(bus, [landed, killed])
        # cron_completed lands AFTER the gateway_stopped row, exactly as it did
        # live (05:31:30 report, 05:32:05 completion).
        _emit_completed(bus, "job-finishes-late")

        mon = _successor(bus)
        mon.startup()

        assert [e.payload["job_id"] for e in _shutdown_stales(bus)] == ["job-killed"]

    def test_a_failed_run_also_counts_as_landed(self, bus):
        landed = _emit_started(bus, "job-failed-late")
        killed = _emit_started(bus, "job-killed")
        _emit_gateway_stopped(bus, [landed, killed])
        _emit_completed(bus, "job-failed-late", event_type=EventType.CRON_FAILED)

        _successor(bus).startup()

        assert [e.payload["job_id"] for e in _shutdown_stales(bus)] == ["job-killed"]

    def test_a_later_rerun_is_not_the_killed_runs_completion(self, bus):
        """A boot between the shutdown and this pass can re-run the same job.
        That run's terminal event belongs to IT, not to the killed run — so the
        outcome search must stop at the next cron_started for the job."""
        killed = _emit_started(bus, "job-x")
        _emit_gateway_stopped(bus, [killed])
        _emit_started(bus, "job-x")          # the successor's own re-run
        _emit_completed(bus, "job-x")        # ...which finished fine

        mon = _successor(bus)
        mon.startup()

        evts = _shutdown_stales(bus)
        assert len(evts) == 1, (
            "the re-run's completion was mistaken for the killed run finishing"
        )
        assert evts[0].payload["cron_started_event_id"] == killed

    def test_age_is_measured_against_the_shutdown_not_now(self, bus):
        """age_seconds means "how far into the run did the shutdown land".

        Reconstruction happens an arbitrary time later — the gateway may have
        been down for hours — so measuring against now would report the
        DOWNTIME, not the run. cf. 2a4ece2c07, which fixed exactly this for the
        in-process path.
        """
        base = datetime.now(timezone.utc)
        started_id = _emit_started(bus, "job-killed",
                                   started_at=base - timedelta(seconds=900))
        stopped_id = _emit_gateway_stopped(bus, [started_id])
        _backdate(bus, stopped_id, base - timedelta(seconds=300))

        mon = _successor(bus)
        mon.startup()

        evts = _shutdown_stales(bus)
        assert len(evts) == 1
        assert evts[0].payload["age_seconds"] == 600, (
            f"expected 900-300=600s into the run, got "
            f"{evts[0].payload['age_seconds']}s"
        )

    def test_shutdowns_older_than_the_horizon_are_skipped_and_logged(self, bus, caplog):
        """The bus is hundreds of MB and cron events dominate it. The lookback
        is bounded — but what the bound drops is logged, not swallowed."""
        import logging

        base = datetime.now(timezone.utc)
        old_start = _emit_started(bus, "job-ancient",
                                  started_at=base - timedelta(days=3, seconds=60))
        old_stop = _emit_gateway_stopped(bus, [old_start])
        _backdate(bus, old_stop, base - timedelta(days=3))

        fresh_start = _emit_started(bus, "job-recent")
        _emit_gateway_stopped(bus, [fresh_start])

        mon = _successor(bus)
        with caplog.at_level(logging.INFO,
                             logger="events.subscribers.cron_stale_monitor"):
            mon.startup()

        assert [e.payload["job_id"] for e in _shutdown_stales(bus)] == ["job-recent"]
        assert any("horizon" in r.getMessage() for r in caplog.records), (
            f"the excluded shutdown was dropped silently: "
            f"{[r.getMessage() for r in caplog.records]}"
        )

    def test_an_unresolvable_correlation_id_is_skipped(self, bus):
        """Retention can evict the cron_started row. Skip that id, keep going."""
        killed = _emit_started(bus, "job-known")
        _emit_gateway_stopped(bus, ["evt-evicted-by-retention", killed])

        mon = _successor(bus)
        mon.startup()

        assert [e.payload["job_id"] for e in _shutdown_stales(bus)] == ["job-known"]

    def test_only_the_listed_ids_are_attributed(self, bus):
        """A restart gap means open runs the ticker never finished for
        unrelated reasons. Only what the payload lists was killed by THIS
        shutdown."""
        killed = _emit_started(bus, "job-killed")
        _emit_started(bus, "job-open-for-other-reasons")
        _emit_gateway_stopped(bus, [killed])

        _successor(bus).startup()

        assert [e.payload["job_id"] for e in _shutdown_stales(bus)] == ["job-killed"]

    def test_a_malformed_inflight_list_is_skipped(self, bus):
        killed = _emit_started(bus, "job-killed")
        bus.emit(
            event_type=EventType.GATEWAY_STOPPED,
            source="gateway",
            payload={"exit_reason": "graceful",
                     "inflight_cron_correlation_ids": "not-a-list"},
        )
        bus.emit(
            event_type=EventType.GATEWAY_STOPPED,
            source="gateway",
            payload={"exit_reason": "graceful"},  # key absent
        )
        _emit_gateway_stopped(bus, [killed])

        _successor(bus).startup()

        assert [e.payload["job_id"] for e in _shutdown_stales(bus)] == ["job-killed"]

    def test_startup_does_not_move_the_cursor_or_fire_handlers(self, bus):
        """ADR-0018: replaying history re-fires every handler and is the
        scanner flood the INSERT OR IGNORE seed exists to prevent. The
        reconstruction is a TARGETED query, never a cursor rewind."""
        started_id = _emit_started(bus, "job-killed")
        _emit_gateway_stopped(bus, [started_id])

        mon = _successor(bus)
        _set_cursor(bus, mon.subscriber_id, 0)

        mon.startup()

        assert _cursor_of(bus, mon.subscriber_id) == 0, "the cursor was rewound/advanced"
        assert mon._open_jobs == {}, "startup() consumed events through handle()"
        assert mon._started_event_ids == {}
        assert mon._pending_shutdown == []

    def test_a_failing_bus_query_does_not_break_startup(self, bus, monkeypatch):
        """startup_all() runs inline in gateway boot — this may never block it."""
        started_id = _emit_started(bus, "job-killed")
        _emit_gateway_stopped(bus, [started_id])

        mon = _successor(bus)

        def _boom(*a, **kw):
            raise RuntimeError("bus unavailable")

        monkeypatch.setattr(bus, "query", _boom)

        mon.startup()  # must not raise

        # Read the bus directly: query() is the thing that is broken.
        import sqlite3
        conn = sqlite3.connect(str(bus.db_path))
        try:
            emitted = conn.execute(
                "SELECT COUNT(*) FROM events WHERE event_type = 'cron_stale'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert emitted == 0
