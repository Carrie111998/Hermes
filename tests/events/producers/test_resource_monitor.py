"""Tests for events.producers.resource_monitor — ResourcePressureMonitor.

The monitor samples Windows commit charge / pagefile allocation / C: free on
the gateway poll loop and emits a RESOURCE_PRESSURE event on the rising edge
of any pressure trigger. Added 2026-06-11 after the pagefile-expansion disk
burst (commit 98.4%, pagefile 36->54.4 GB in ~22 min) went un-alerted.

Sampling is injected (sampler + clock callables) so the evaluation core is
tested deterministically — no real hardware, no sleeps, runs on any platform.
"""

import sys

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.producers.resource_monitor import (
    ResourcePressureMonitor,
    ResourceSample,
    sample_resources,
)

GB = 1024 ** 3


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


def make_sample(
    commit_pct=50.0,
    pagefile_gb=20.0,
    disk_free_gb=100.0,
    commit_limit_gb=47.0,
    phys_pct=50.0,
    phys_total_gb=64.0,
):
    """Build a ResourceSample, healthy by default; override one axis to stress it."""
    limit = int(commit_limit_gb * GB)
    used = int(limit * commit_pct / 100.0)
    phys_total = int(phys_total_gb * GB)
    phys_avail = phys_total - int(phys_total * phys_pct / 100.0)
    return ResourceSample(
        commit_used_bytes=used,
        commit_limit_bytes=limit,
        pagefile_allocated_bytes=int(pagefile_gb * GB),
        disk_free_bytes=int(disk_free_gb * GB),
        phys_total_bytes=phys_total,
        phys_avail_bytes=phys_avail,
    )


def _pressure_events(bus):
    return bus.query(event_type=EventType.RESOURCE_PRESSURE)


class TestNoFalsePositive:
    def test_healthy_sample_emits_nothing(self, bus):
        monitor = ResourcePressureMonitor(bus)
        result = monitor.evaluate(make_sample(), now=0.0)
        assert result is None
        assert _pressure_events(bus) == []


class TestCommitThreshold:
    def test_commit_above_85pct_emits(self, bus):
        monitor = ResourcePressureMonitor(bus)
        event_id = monitor.evaluate(make_sample(commit_pct=98.4), now=0.0)
        assert event_id
        events = _pressure_events(bus)
        assert len(events) == 1
        assert "commit_high" in events[0].payload["reasons"]

    def test_commit_at_threshold_does_not_emit(self, bus):
        # Strictly greater-than: exactly 85% is not yet "pressure".
        monitor = ResourcePressureMonitor(bus)
        assert monitor.evaluate(make_sample(commit_pct=85.0), now=0.0) is None
        assert _pressure_events(bus) == []

    def test_emitted_event_is_high_priority(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(commit_pct=99.0), now=0.0)
        assert _pressure_events(bus)[0].priority is Priority.HIGH


class TestDiskThreshold:
    def test_disk_below_15gb_emits(self, bus):
        monitor = ResourcePressureMonitor(bus)
        event_id = monitor.evaluate(make_sample(disk_free_gb=12.3), now=0.0)
        assert event_id
        assert "disk_low" in _pressure_events(bus)[0].payload["reasons"]

    def test_disk_at_threshold_does_not_emit(self, bus):
        monitor = ResourcePressureMonitor(bus)
        assert monitor.evaluate(make_sample(disk_free_gb=15.0), now=0.0) is None
        assert _pressure_events(bus) == []


class TestPagefileGrowthTrigger:
    def test_growth_over_2gb_within_window_emits(self, bus):
        monitor = ResourcePressureMonitor(bus)
        # Baseline at t=0 (everything else healthy) — records the window anchor.
        assert monitor.evaluate(make_sample(pagefile_gb=20.0), now=0.0) is None
        # +3 GB five minutes later, still inside the 10-min window -> trigger.
        event_id = monitor.evaluate(make_sample(pagefile_gb=23.0), now=300.0)
        assert event_id
        payload = _pressure_events(bus)[0].payload
        assert "pagefile_growth" in payload["reasons"]
        assert payload["pagefile_growth_gb_10min"] == pytest.approx(3.0, abs=0.01)

    def test_growth_under_threshold_does_not_emit(self, bus):
        monitor = ResourcePressureMonitor(bus)
        assert monitor.evaluate(make_sample(pagefile_gb=20.0), now=0.0) is None
        # +1.5 GB is below the 2 GB trigger.
        assert monitor.evaluate(make_sample(pagefile_gb=21.5), now=120.0) is None
        assert _pressure_events(bus) == []

    def test_growth_outside_window_is_pruned(self, bus):
        monitor = ResourcePressureMonitor(bus)
        # Baseline at t=0.
        assert monitor.evaluate(make_sample(pagefile_gb=20.0), now=0.0) is None
        # +3 GB but 11 minutes later: the 20 GB anchor has aged out of the
        # 10-min window, so min-in-window == current and growth reads ~0.
        assert monitor.evaluate(make_sample(pagefile_gb=23.0), now=660.0) is None
        assert _pressure_events(bus) == []

    def test_first_sample_never_triggers_growth(self, bus):
        # A huge first reading must not look like growth from a zero baseline.
        monitor = ResourcePressureMonitor(bus)
        assert monitor.evaluate(make_sample(pagefile_gb=54.0), now=0.0) is None
        assert _pressure_events(bus) == []


class TestPhysMemoryThreshold:
    """Physical-RAM axis — added 2026-07-16 after a paging storm (phys 96.4%,
    laptop-monitor healthy-count collapsed twice, Docker/PG down) produced
    ZERO resource_pressure events because commit stayed 50-73% all day."""

    def test_phys_above_92pct_emits(self, bus):
        monitor = ResourcePressureMonitor(bus)
        event_id = monitor.evaluate(make_sample(phys_pct=96.4), now=0.0)
        assert event_id
        events = _pressure_events(bus)
        assert len(events) == 1
        assert "phys_high" in events[0].payload["reasons"]

    def test_phys_at_threshold_does_not_emit(self, bus):
        # Strictly greater-than, mirroring the commit_pct axis.
        monitor = ResourcePressureMonitor(bus)
        assert monitor.evaluate(make_sample(phys_pct=92.0), now=0.0) is None
        assert _pressure_events(bus) == []

    def test_2026_07_16_storm_shape_emits(self, bus):
        # The exact blind spot: commit healthy (50-73%), phys storming.
        monitor = ResourcePressureMonitor(bus)
        event_id = monitor.evaluate(
            make_sample(commit_pct=65.0, phys_pct=96.4), now=0.0,
        )
        assert event_id
        reasons = _pressure_events(bus)[0].payload["reasons"]
        assert reasons == ["phys_high"]

    def test_payload_carries_phys_metrics_and_threshold(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(
            make_sample(phys_pct=96.4, phys_total_gb=64.0), now=0.0,
        )
        p = _pressure_events(bus)[0].payload
        assert p["phys_used_pct"] == pytest.approx(96.4, abs=0.1)
        assert p["phys_available_gb"] == pytest.approx(64.0 * 0.036, abs=0.1)
        assert p["thresholds"]["phys_pct"] == 92.0

    def test_phys_pressure_shares_edge_trigger_and_cooldown(self, bus):
        monitor = ResourcePressureMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert monitor.evaluate(make_sample(phys_pct=97.0), now=0.0)
        # Sustained storm inside the cooldown stays quiet (no Telegram spam).
        assert monitor.evaluate(make_sample(phys_pct=97.0), now=60.0) is None
        assert len(_pressure_events(bus)) == 1
        # Re-pings once the cooldown elapses.
        assert monitor.evaluate(make_sample(phys_pct=97.0), now=901.0)
        assert len(_pressure_events(bus)) == 2

    def test_sample_without_phys_fields_is_safe(self, bus):
        # Pre-2026-07-16 constructor shape (no phys args) must keep working
        # and never trigger the phys axis (phys_pct reads 0.0).
        sample = ResourceSample(
            commit_used_bytes=int(23 * GB),
            commit_limit_bytes=int(47 * GB),
            pagefile_allocated_bytes=int(20 * GB),
            disk_free_bytes=int(100 * GB),
        )
        assert sample.phys_pct == 0.0
        monitor = ResourcePressureMonitor(bus)
        assert monitor.evaluate(sample, now=0.0) is None
        assert _pressure_events(bus) == []


class TestEdgeTriggerAndCooldown:
    def test_sustained_pressure_emits_once_within_cooldown(self, bus):
        monitor = ResourcePressureMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert monitor.evaluate(make_sample(commit_pct=99.0), now=0.0)
        # Still under pressure 60s later — no re-emit inside the cooldown.
        assert monitor.evaluate(make_sample(commit_pct=99.0), now=60.0) is None
        assert len(_pressure_events(bus)) == 1

    def test_sustained_pressure_re_emits_after_cooldown(self, bus):
        monitor = ResourcePressureMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert monitor.evaluate(make_sample(commit_pct=99.0), now=0.0)
        # Worsening/sustained incident re-pings once the cooldown elapses.
        assert monitor.evaluate(make_sample(commit_pct=99.0), now=901.0)
        assert len(_pressure_events(bus)) == 2

    def test_recovery_then_relapse_emits_again_immediately(self, bus):
        monitor = ResourcePressureMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert monitor.evaluate(make_sample(commit_pct=99.0), now=0.0)
        # Pressure clears — falling edge resets the episode.
        assert monitor.evaluate(make_sample(commit_pct=50.0), now=60.0) is None
        # New rising edge fires immediately, NOT gated by the prior cooldown.
        assert monitor.evaluate(make_sample(commit_pct=99.0), now=120.0)
        assert len(_pressure_events(bus)) == 2


class TestPayloadContents:
    def test_payload_carries_all_metrics(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(
            make_sample(commit_pct=98.4, commit_limit_gb=85.6,
                        pagefile_gb=54.4, disk_free_gb=12.3),
            now=0.0,
        )
        p = _pressure_events(bus)[0].payload
        assert p["commit_pct"] == pytest.approx(98.4, abs=0.1)
        assert p["commit_limit_gb"] == pytest.approx(85.6, abs=0.1)
        assert p["disk_c_free_gb"] == pytest.approx(12.3, abs=0.1)
        assert p["pagefile_allocated_gb"] == pytest.approx(54.4, abs=0.1)
        assert "thresholds" in p and p["thresholds"]["commit_pct"] == 85.0

    def test_multiple_triggers_listed_together(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(commit_pct=99.0, disk_free_gb=5.0), now=0.0)
        reasons = _pressure_events(bus)[0].payload["reasons"]
        assert "commit_high" in reasons and "disk_low" in reasons

    def test_source_is_system(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(commit_pct=99.0), now=0.0)
        assert _pressure_events(bus)[0].source == "system"


class TestCheckIntegration:
    def test_check_uses_injected_sampler_and_emits(self, bus):
        monitor = ResourcePressureMonitor(
            bus, sampler=lambda: make_sample(commit_pct=99.0),
        )
        event_id = monitor.check()
        assert event_id
        assert len(_pressure_events(bus)) == 1

    def test_check_noop_when_sampler_returns_none(self, bus):
        # Non-Windows host or a failed ctypes/disk read -> sampler yields None.
        monitor = ResourcePressureMonitor(bus, sampler=lambda: None)
        assert monitor.check() is None
        assert _pressure_events(bus) == []

    def test_check_swallows_sampler_exceptions(self, bus):
        def boom():
            raise OSError("GlobalMemoryStatusEx failed")

        monitor = ResourcePressureMonitor(bus, sampler=boom)
        # A sampler blow-up must never crash the gateway poll loop.
        assert monitor.check() is None
        assert _pressure_events(bus) == []


class TestRealSampler:
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only metrics")
    def test_real_sampler_returns_sane_sample_or_none(self):
        sample = sample_resources()
        # May be None if ctypes/disk read fails; if present, fields are sane.
        if sample is not None:
            assert sample.commit_limit_bytes > 0
            assert 0 <= sample.commit_used_bytes <= sample.commit_limit_bytes
            assert sample.pagefile_allocated_bytes >= 0
            assert sample.disk_free_bytes >= 0
            assert 0.0 <= sample.commit_pct <= 100.0
            assert sample.phys_total_bytes > 0
            assert 0 <= sample.phys_avail_bytes <= sample.phys_total_bytes
            assert 0.0 <= sample.phys_pct <= 100.0

    @pytest.mark.skipif(sys.platform == "win32", reason="non-Windows path")
    def test_real_sampler_is_none_off_windows(self):
        assert sample_resources() is None
