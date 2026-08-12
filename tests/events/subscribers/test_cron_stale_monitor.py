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
