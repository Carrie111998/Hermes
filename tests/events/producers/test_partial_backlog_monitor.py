"""Tests for events.producers.partial_backlog_monitor — PartialBacklogMonitor.

Mirrors tests/events/producers/test_resource_monitor.py: the sampler + clock are
injected so the edge/cooldown core is tested deterministically — no real mailbox,
no sleeps. Added 2026-07-14 after the 07-13 partial pileup sat ~a day un-alerted.
"""
import json
import os
import time

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.producers.partial_backlog_monitor import (
    PartialBacklogMonitor,
    PartialBacklogSample,
    sample_partial_backlog,
)


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


def make_sample(count=5, oldest_age_seconds=300.0, sample_job_ids=None):
    return PartialBacklogSample(
        count=count,
        oldest_age_seconds=oldest_age_seconds,
        sample_job_ids=sample_job_ids or [f"job-{i}" for i in range(min(count, 3))],
    )


def _backlog_events(bus):
    return bus.query(event_type=EventType.TRACKER_PARTIAL_BACKLOG)


class TestNoFalsePositive:
    def test_below_threshold_emits_nothing(self, bus):
        m = PartialBacklogMonitor(bus)
        assert m.evaluate(make_sample(count=2), now=0.0) is None
        assert _backlog_events(bus) == []

    def test_at_threshold_does_not_emit(self, bus):
        # Strictly greater-than: exactly 3 is not yet a backlog.
        m = PartialBacklogMonitor(bus)
        assert m.evaluate(make_sample(count=3), now=0.0) is None
        assert _backlog_events(bus) == []


class TestRisingEdge:
    def test_above_threshold_emits(self, bus):
        m = PartialBacklogMonitor(bus)
        assert m.evaluate(make_sample(count=4), now=0.0)
        assert len(_backlog_events(bus)) == 1

    def test_emitted_event_is_high_priority(self, bus):
        m = PartialBacklogMonitor(bus)
        m.evaluate(make_sample(count=9), now=0.0)
        assert _backlog_events(bus)[0].priority is Priority.HIGH

    def test_source_is_applier(self, bus):
        m = PartialBacklogMonitor(bus)
        m.evaluate(make_sample(count=9), now=0.0)
        assert _backlog_events(bus)[0].source == "tracker-intent-applier"


class TestEdgeTriggerAndCooldown:
    def test_sustained_backlog_emits_once_within_cooldown(self, bus):
        m = PartialBacklogMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert m.evaluate(make_sample(count=6), now=0.0)
        assert m.evaluate(make_sample(count=6), now=60.0) is None
        assert len(_backlog_events(bus)) == 1

    def test_sustained_backlog_re_emits_after_cooldown(self, bus):
        m = PartialBacklogMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert m.evaluate(make_sample(count=6), now=0.0)
        assert m.evaluate(make_sample(count=6), now=901.0)
        assert len(_backlog_events(bus)) == 2

    def test_falling_edge_re_arms(self, bus):
        m = PartialBacklogMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert m.evaluate(make_sample(count=6), now=0.0)
        # Backlog drains to <= threshold: falling edge resets the episode.
        assert m.evaluate(make_sample(count=1), now=60.0) is None
        # New rise fires immediately, NOT gated by the prior cooldown.
        assert m.evaluate(make_sample(count=6), now=120.0)
        assert len(_backlog_events(bus)) == 2


class TestPayload:
    def test_payload_shape(self, bus):
        m = PartialBacklogMonitor(bus)
        m.evaluate(
            make_sample(count=7, oldest_age_seconds=1234.5,
                        sample_job_ids=["a", "b", "c"]),
            now=0.0,
        )
        p = _backlog_events(bus)[0].payload
        assert p["count"] == 7
        assert p["threshold"] == 3
        assert p["oldest_age_seconds"] == pytest.approx(1234.5, abs=0.1)
        assert p["capped_count"] == 3
        assert p["sample_job_ids"] == ["a", "b", "c"]


class TestCheckIntegration:
    def test_check_uses_injected_sampler_and_emits(self, bus):
        m = PartialBacklogMonitor(bus, sampler=lambda: make_sample(count=9))
        assert m.check()
        assert len(_backlog_events(bus)) == 1

    def test_check_noop_when_sampler_returns_none(self, bus):
        m = PartialBacklogMonitor(bus, sampler=lambda: None)
        assert m.check() is None
        assert _backlog_events(bus) == []

    def test_check_swallows_sampler_exceptions(self, bus):
        def boom():
            raise OSError("stat failed")
        m = PartialBacklogMonitor(bus, sampler=boom)
        assert m.check() is None
        assert _backlog_events(bus) == []


class TestRealSampler:
    def test_missing_dir_is_empty_sample(self, tmp_path):
        s = sample_partial_backlog(tmp_path / "nope", now=1000.0)
        assert s.count == 0
        assert s.sample_job_ids == []

    def test_counts_and_samples_job_ids(self, tmp_path):
        partial = tmp_path / "partial"
        partial.mkdir()
        for i in range(4):
            p = partial / f"20260713T10000{i}_APPROVAL_INTENT_main.json"
            p.write_text(json.dumps({"job_id": f"job-{i}"}), encoding="utf-8")
            past = time.time() - (100 + i)
            os.utime(p, (past, past))
        # A non-intent file must be ignored (shared mailbox).
        (partial / "note.json").write_text("{}", encoding="utf-8")
        s = sample_partial_backlog(partial, now=time.time())
        assert s.count == 4
        assert set(s.sample_job_ids) == {"job-0", "job-1", "job-2", "job-3"}
        assert s.oldest_age_seconds >= 100

    def test_sample_cap_bounds_job_ids(self, tmp_path):
        partial = tmp_path / "partial"
        partial.mkdir()
        for i in range(15):
            (partial / f"20260713T1000{i:02d}_APPROVAL_INTENT_main.json").write_text(
                json.dumps({"job_id": f"job-{i}"}), encoding="utf-8")
        s = sample_partial_backlog(partial, now=time.time(), sample_cap=10)
        assert s.count == 15
        assert len(s.sample_job_ids) == 10
