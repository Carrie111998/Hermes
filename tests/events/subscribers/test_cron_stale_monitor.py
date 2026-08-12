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

        # ±60s, not ±1s. The backdated timestamp is fixed at emit time but the
        # monitor samples datetime.now() later, so a 1s margin only holds if
        # emit + sqlite UPDATE + construction + poll all complete inside one
        # second — under gate load they do not, and the "within" case tipped to
        # age_seconds == threshold and alerted. 60s still discriminates: the
        # per-job thresholds are 1200/1800/2100/2400/3600, so every one of them
        # stays on the correct side of the 1200s default.
        margin = 60
        within = datetime.now(timezone.utc) - timedelta(seconds=threshold - margin)
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

        past = datetime.now(timezone.utc) - timedelta(seconds=threshold + margin)
        _emit_started(bus, job_name, started_at=past)
        mon.poll()

        stale = _stale_events(bus)
        assert len(stale) == 1
        assert stale[0].payload["job_name"] == job_name
        assert stale[0].payload["threshold_seconds"] == threshold
