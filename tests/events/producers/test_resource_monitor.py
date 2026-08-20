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
    DEFAULT_DISK_FREE_GB_DISARM,
    DEFAULT_DISK_FREE_GB_THRESHOLD,
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
    disk_free_gb=300.0,
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
    def test_disk_below_threshold_emits(self, bus):
        # disk_free_gb_critical=0.0 isolates the low axis under test.
        monitor = ResourcePressureMonitor(
            bus, disk_free_gb_threshold=15.0, disk_free_gb_critical=0.0)
        event_id = monitor.evaluate(make_sample(disk_free_gb=12.3), now=0.0)
        assert event_id
        assert "disk_low" in _pressure_events(bus)[0].payload["reasons"]

    def test_disk_at_threshold_does_not_emit(self, bus):
        monitor = ResourcePressureMonitor(
            bus, disk_free_gb_threshold=15.0, disk_free_gb_critical=0.0)
        assert monitor.evaluate(make_sample(disk_free_gb=15.0), now=0.0) is None
        assert _pressure_events(bus) == []

    def test_disk_critical_is_a_separate_axis_from_disk_low(self, bus):
        # 35 GB: below the 45 GB early-warning axis, above the 25 GB paging one.
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(disk_free_gb=35.0), now=0.0)
        reasons = _pressure_events(bus)[0].payload["reasons"]
        assert "disk_low" in reasons
        assert "disk_critical" not in reasons

    def test_disk_critical_emits_below_25gb(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(disk_free_gb=12.0), now=0.0)
        reasons = _pressure_events(bus)[0].payload["reasons"]
        assert "disk_low" in reasons
        assert "disk_critical" in reasons

    def test_disk_critical_axis_can_unlatch(self, bus):
        # An axis with no disarm level latches forever (the phys-axis bug).
        # Breach, recover comfortably clear, then breach again -> second event.
        monitor = ResourcePressureMonitor(bus)
        assert monitor.evaluate(make_sample(disk_free_gb=12.0), now=0.0)
        assert monitor.evaluate(make_sample(disk_free_gb=300.0), now=60.0) is None
        assert monitor.evaluate(make_sample(disk_free_gb=12.0), now=120.0)
        assert len(_pressure_events(bus)) == 2

    def test_default_threshold_warns_with_a_full_churn_cycle_of_headroom(self, bus):
        # Regression 2026-08-14: the old 15 GB default fired only once the disk was
        # hours from zero, because Docker's VHDX can allocate 40-50 GB overnight.
        monitor = ResourcePressureMonitor(bus)
        assert monitor.evaluate(make_sample(disk_free_gb=35.0), now=0.0)
        assert "disk_low" in _pressure_events(bus)[0].payload["reasons"]

    def test_default_disarm_is_reachable_on_this_hardware(self, bus):
        # Regression 2026-08-14 (same day, second defect): the trigger was briefly
        # set to 60 GB with a 75 GB disarm on a box whose post-reclaim CEILING is
        # ~56.6 GB free. The axis breached on its first sample and could never
        # unlatch, re-pinging every cooldown forever. Pin the invariant that makes
        # that impossible: a fully-reclaimed disk must read as comfortably clear.
        POST_RECLAIM_CEILING_GB = 56.6
        assert DEFAULT_DISK_FREE_GB_THRESHOLD < POST_RECLAIM_CEILING_GB
        assert DEFAULT_DISK_FREE_GB_DISARM < POST_RECLAIM_CEILING_GB
        # ...and prove it end-to-end: breach, then recover to the real ceiling.
        monitor = ResourcePressureMonitor(bus)
        assert monitor.evaluate(make_sample(disk_free_gb=35.0), now=0.0)
        assert monitor.evaluate(
            make_sample(disk_free_gb=POST_RECLAIM_CEILING_GB), now=60.0) is None
        # Cleared for real, so the next breach is a fresh rising edge.
        assert monitor.evaluate(make_sample(disk_free_gb=35.0), now=120.0)
        assert len(_pressure_events(bus)) == 2


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
            disk_free_bytes=int(300 * GB),
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


class TestEscalationWithinAnEpisode:
    """A NEW axis breaching mid-episode is new information, not a re-ping.

    Regression 2026-08-14 (third defect of the day). ``rising_edge`` was
    ``not was_in_episode`` — one global boolean over ALL axes — so once any
    axis latched, an axis that breached later was folded into the running
    episode and had to wait out ``re_alert_cooldown_seconds``.

    That was harmless while every axis routed the same way. The two-stage disk
    work made it a paging bug: disk_critical (25 GB) sits BELOW disk_low
    (45 GB), so on the failure mode this axis exists for — a disk that fills
    monotonically until a human frees space — disk_low ALWAYS latches first.
    The critical page was therefore never prompt, and a dip that recovered
    inside the cooldown was never sent at all. Only a gateway that restarted
    while already below 25 GB could page immediately, which is exactly the
    shape every pre-existing disk_critical test used.
    """

    def test_disk_critical_pages_immediately_during_a_disk_low_episode(self, bus):
        monitor = ResourcePressureMonitor(bus, re_alert_cooldown_seconds=900.0)
        # 40 GB: the disk_low early warning opens the episode.
        assert monitor.evaluate(make_sample(disk_free_gb=40.0), now=0.0)
        # Still only disk_low — a re-ping inside the cooldown stays suppressed.
        assert monitor.evaluate(make_sample(disk_free_gb=32.0), now=60.0) is None
        # 20 GB crosses the 25 GB paging axis. That is a NEW axis, so it must
        # emit now rather than 840s later when the cooldown happens to elapse.
        assert monitor.evaluate(make_sample(disk_free_gb=20.0), now=120.0)
        events = _pressure_events(bus)
        assert len(events) == 2
        assert "disk_critical" not in events[0].payload["reasons"]
        assert "disk_critical" in events[1].payload["reasons"]

    def test_disk_critical_dip_inside_the_cooldown_is_not_lost(self, bus):
        # Worse than a delayed page: under the old rule a breach that recovered
        # before the cooldown elapsed produced NO disk_critical event ever.
        monitor = ResourcePressureMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert monitor.evaluate(make_sample(disk_free_gb=40.0), now=0.0)
        assert monitor.evaluate(make_sample(disk_free_gb=20.0), now=120.0)
        # Recovered comfortably clear of both axes — episode over.
        assert monitor.evaluate(make_sample(disk_free_gb=60.0), now=300.0) is None
        reasons = [r for e in _pressure_events(bus) for r in e.payload["reasons"]]
        assert "disk_critical" in reasons

    def test_escalation_is_not_disk_specific(self, bus):
        # The rule is per-axis, not a disk special case: phys breaching during
        # a running commit episode is also new information.
        monitor = ResourcePressureMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert monitor.evaluate(make_sample(commit_pct=99.0), now=0.0)
        assert monitor.evaluate(
            make_sample(commit_pct=99.0, phys_pct=96.4), now=60.0)
        assert len(_pressure_events(bus)) == 2

    def test_already_latched_axis_re_breaching_is_not_an_edge(self, bus):
        # The other half of the contract: only a NEWLY latched axis escalates.
        # An axis that hovers in its band and re-breaches is the 2026-06-11
        # storm shape and must stay cooldown-gated.
        monitor = ResourcePressureMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert monitor.evaluate(make_sample(commit_pct=88.2), now=0.0)
        # 84% is below the 85% trigger but above the 80% disarm — still latched.
        assert monitor.evaluate(make_sample(commit_pct=84.0), now=60.0) is None
        # Re-crossing is the SAME axis, already latched: no fresh edge.
        assert monitor.evaluate(make_sample(commit_pct=85.3), now=120.0) is None
        assert len(_pressure_events(bus)) == 1

    def test_escalated_event_routes_to_action_required_end_to_end(self, bus):
        # Ties the producer to the deployed routing policy. Every other
        # disk_critical routing test hand-builds an Event, so nothing proved a
        # REAL emitted payload carries the shape classify()'s hook reads —
        # nor that the hook still wins after 19a8dd9abd moved the base spec
        # for resource_pressure from watchdog_alerts to security_and_system.
        from events.routing_policy import Attention, classify

        monitor = ResourcePressureMonitor(bus, re_alert_cooldown_seconds=900.0)
        monitor.evaluate(make_sample(disk_free_gb=40.0), now=0.0)
        monitor.evaluate(make_sample(disk_free_gb=20.0), now=120.0)
        low, critical = _pressure_events(bus)

        # The WARN stage stays legible in security_and_system and never pages.
        low_route = classify(low)
        assert low_route.attention is Attention.WARN
        assert low_route.topic_key == "security_and_system"
        assert low_route.wa_tier is None

        # The critical stage overrides that default topic and pages.
        crit_route = classify(critical)
        assert crit_route.attention is Attention.ACT
        assert crit_route.topic_key == "action_required"
        assert crit_route.wa_tier == "urgent"
        assert crit_route.batch is False


class TestHysteresis:
    """Disarm-level hysteresis (added 2026-06-12): a breached axis re-arms only
    once *comfortably* clear of its trigger, so hovering right at a threshold
    cannot alert-storm past the re-alert cooldown.
    """

    def test_threshold_hover_storm_is_suppressed(self, bus):
        # Live incident 2026-06-11 22:52-23:21Z: commit charge oscillated right
        # at the 85% trigger and fired SIX alerts in 29 minutes (payload
        # commit_pct 88.2, 85.3, 85.0, 85.3, 85.3, 85.0) — every dip below the
        # trigger re-armed the edge, so every re-cross bypassed the cooldown.
        monitor = ResourcePressureMonitor(bus, re_alert_cooldown_seconds=900.0)
        trace = [
            (0, 88.2), (2, 84.9), (5, 85.3), (7, 84.8), (10, 85.04), (12, 84.9),
            (15, 85.3), (17, 84.7), (20, 85.3), (24, 84.9), (27, 85.04), (29, 84.9),
        ]
        fired = [
            minute for minute, pct in trace
            if monitor.evaluate(make_sample(commit_pct=pct), now=minute * 60.0)
        ]
        # The 84.x dips sit inside the hysteresis band (above the 80% disarm),
        # so the episode never re-arms: one rising edge + one cooldown re-ping.
        assert fired == [0, 15]
        assert len(_pressure_events(bus)) == 2

    def test_band_dip_does_not_rearm_edge(self, bus):
        monitor = ResourcePressureMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert monitor.evaluate(make_sample(commit_pct=88.0), now=0.0)
        # 80.5% is below the trigger but above the 80% disarm: same episode.
        assert monitor.evaluate(make_sample(commit_pct=80.5), now=60.0) is None
        # Re-cross inside the cooldown stays quiet — NOT a fresh rising edge.
        assert monitor.evaluate(make_sample(commit_pct=88.0), now=120.0) is None
        assert len(_pressure_events(bus)) == 1

    def test_comfortable_clearance_rearms_immediately(self, bus):
        monitor = ResourcePressureMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert monitor.evaluate(make_sample(commit_pct=88.0), now=0.0)
        # 79% is below the 80% disarm: genuine recovery ends the episode.
        assert monitor.evaluate(make_sample(commit_pct=79.0), now=60.0) is None
        # The next breach is a fresh emergency — fires immediately, no cooldown.
        assert monitor.evaluate(make_sample(commit_pct=88.0), now=120.0)
        assert len(_pressure_events(bus)) == 2

    def test_disk_band_holds_episode(self, bus):
        # Breaches at 35 GB, not 12: this test is about the disk_LOW band, and
        # the fixture must stay inside the band of the axis it breaches. 12 GB
        # also trips disk_critical (25/40), for which the 48 GB in-band sample
        # below is *comfortably clear* — so a 12 -> 48 -> 12 walk is a genuine
        # recovery and re-breach of the paging axis, which must re-emit. No
        # single value is in-band for both axes; their bands do not overlap.
        # Before the per-axis rising edge landed this file asserted the
        # opposite, quietly pinning the escalation bug as correct behaviour.
        monitor = ResourcePressureMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert monitor.evaluate(make_sample(disk_free_gb=35.0), now=0.0)
        # 48 GB free is between the 45 GB trigger and the 52 GB disarm: holds.
        assert monitor.evaluate(make_sample(disk_free_gb=48.0), now=60.0) is None
        assert monitor.evaluate(make_sample(disk_free_gb=35.0), now=120.0) is None
        # Recovery above the disarm clears; the next breach fires immediately.
        assert monitor.evaluate(make_sample(disk_free_gb=60.0), now=180.0) is None
        assert monitor.evaluate(make_sample(disk_free_gb=35.0), now=240.0)
        assert len(_pressure_events(bus)) == 2

    def test_pagefile_growth_band_holds_episode(self, bus):
        monitor = ResourcePressureMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert monitor.evaluate(make_sample(pagefile_gb=20.0), now=0.0) is None
        # +2.5 GB inside the 10-min window crosses the 2 GB trigger.
        assert monitor.evaluate(make_sample(pagefile_gb=22.5), now=60.0)
        # Growth reads 1.5 GB: under the trigger, above the 1 GB disarm — holds.
        assert monitor.evaluate(make_sample(pagefile_gb=21.5), now=120.0) is None
        assert monitor.evaluate(make_sample(pagefile_gb=22.5), now=180.0) is None
        # By t=700 the 20 GB anchor aged out: growth reads 0.5 GB (< disarm).
        assert monitor.evaluate(make_sample(pagefile_gb=22.0), now=700.0) is None
        # A fresh burst (+3 GB over the surviving window) fires immediately.
        assert monitor.evaluate(make_sample(pagefile_gb=25.0), now=760.0)
        assert len(_pressure_events(bus)) == 2

    def test_phys_band_holds_episode_and_clears(self, bus):
        # REGRESSION (2026-08-11): the phys axis (2026-07-16) postdates the
        # original disarm set (2026-06-12). When the two were finally brought
        # together, phys had a trigger but NO disarm level — so "phys_high"
        # could enter ``_latched`` via ``reasons`` yet never leave through
        # ``comfortably_clear``. One phys breach would latch the episode
        # forever: ``was_in_episode`` stays True, no later rising edge ever
        # fires, and the monitor silently degrades to cooldown-only re-pings.
        # The last two asserts are what fail without DEFAULT_PHYS_PCT_DISARM.
        monitor = ResourcePressureMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert monitor.evaluate(make_sample(phys_pct=96.4), now=0.0)
        # 89% is below the 92% trigger but above the 87% disarm: episode holds.
        assert monitor.evaluate(make_sample(phys_pct=89.0), now=60.0) is None
        assert monitor.evaluate(make_sample(phys_pct=96.4), now=120.0) is None
        # 85% clears comfortably -> episode really ends...
        assert monitor.evaluate(make_sample(phys_pct=85.0), now=180.0) is None
        # ...so the next breach is a fresh rising edge, NOT gated by cooldown.
        assert monitor.evaluate(make_sample(phys_pct=96.4), now=240.0)
        assert len(_pressure_events(bus)) == 2

    def test_unbreached_axis_in_band_does_not_hold_episode(self, bus):
        # Guard for the latch design: only axes that actually BREACHED hold the
        # episode open. Disk hovers in its band (48 GB) throughout but never
        # crossed its 45 GB trigger, so commit clearing comfortably ends the
        # episode — disk must not suppress the next fresh commit emergency.
        # NB: this fixture must stay IN THE BAND. It was scaled to 170.0 on
        # 2026-08-14, which is comfortably clear rather than in-band, and the
        # test passed while exercising none of the behaviour it documents.
        monitor = ResourcePressureMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert monitor.evaluate(
            make_sample(commit_pct=88.0, disk_free_gb=48.0), now=0.0)
        assert monitor.evaluate(
            make_sample(commit_pct=70.0, disk_free_gb=48.0), now=60.0) is None
        assert monitor.evaluate(
            make_sample(commit_pct=88.0, disk_free_gb=48.0), now=120.0)
        assert len(_pressure_events(bus)) == 2

    def test_custom_disarm_levels_are_honored(self, bus):
        monitor = ResourcePressureMonitor(
            bus, commit_pct_disarm=84.0, re_alert_cooldown_seconds=900.0,
        )
        assert monitor.evaluate(make_sample(commit_pct=88.0), now=0.0)
        # 83% clears the custom 84% disarm (the default 80% would have held).
        assert monitor.evaluate(make_sample(commit_pct=83.0), now=60.0) is None
        assert monitor.evaluate(make_sample(commit_pct=88.0), now=120.0)
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


class TestSeverityBands:
    """Severity bands (2026-08-14).

    ``normalize_for_fingerprint`` collapses digit runs, so ``C: free: 0.0 GB``
    and ``C: free: 56.63 GB`` were the SAME message and a dying disk was
    suppressed by the same sliding window as a healthy one (measured: one
    fingerprint covered 101 events below 5 GiB and 13 at exactly 0.0 GiB).
    A band label is LETTERS, which the fingerprint can see.
    """

    def test_first_disk_breach_carries_the_low_band(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(disk_free_gb=30.0), now=0.0)
        p = _pressure_events(bus)[0].payload
        assert p["disk_band"] == "low"
        assert p["disk_band_edge_gb"] == 45

    def test_a_drop_past_several_edges_announces_only_the_deepest(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(disk_free_gb=2.0), now=0.0)
        events = _pressure_events(bus)
        assert len(events) == 1
        assert events[0].payload["disk_band"] == "imminent"
        assert events[0].payload["disk_band_edge_gb"] == 3

    def test_a_non_disk_episode_has_no_band(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(phys_pct=95.0), now=0.0)
        p = _pressure_events(bus)[0].payload
        assert p["reasons"] == ["phys_high"]
        assert p["disk_band"] is None


class TestBandRatchetAndChangeStamp:
    """The ratchet decides when a band may ANNOUNCE, and ``change`` records why
    each emission exists so the subscribers can drop pure repeats without the
    bus losing its sampling."""

    def _changes(self, bus):
        return [e.payload["change"] for e in _pressure_events(bus)]

    def test_a_deeper_edge_announces_without_waiting_for_the_cooldown(self, bus):
        # Both samples are already below disk_critical (25), so no axis NEWLY
        # breaches and the deepening band is the only thing that changed --
        # isolating band_change from the rising edge that outranks it.
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(disk_free_gb=20.0), now=0.0)
        monitor.evaluate(make_sample(disk_free_gb=10.0), now=60.0)
        events = _pressure_events(bus)
        assert [e.payload["disk_band"] for e in events] == ["critical", "severe"]
        assert self._changes(bus) == ["rising_edge", "band_change"]

    def test_hovering_inside_a_band_never_re_announces_it(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(disk_free_gb=10.0), now=0.0)
        monitor.evaluate(make_sample(disk_free_gb=9.0), now=1000.0)
        monitor.evaluate(make_sample(disk_free_gb=11.0), now=2000.0)
        assert self._changes(bus) == [
            "rising_edge", "sustained_repeat", "sustained_repeat"]

    def test_an_edge_re_arms_once_free_space_recovers_past_the_factor(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(disk_free_gb=10.0), now=0.0)
        monitor.evaluate(make_sample(disk_free_gb=20.0), now=1000.0)
        monitor.evaluate(make_sample(disk_free_gb=10.0), now=2000.0)
        last = _pressure_events(bus)[-1].payload
        assert last["disk_band"] == "severe"
        assert last["change"] == "band_change"

    def test_a_dip_short_of_the_rearm_factor_does_not_re_announce(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(disk_free_gb=10.0), now=0.0)
        monitor.evaluate(make_sample(disk_free_gb=13.0), now=1000.0)
        monitor.evaluate(make_sample(disk_free_gb=10.0), now=2000.0)
        assert "band_change" not in self._changes(bus)[1:]

    def test_an_axis_dropping_out_is_a_reasons_change(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(disk_free_gb=30.0, phys_pct=95.0), now=0.0)
        monitor.evaluate(make_sample(disk_free_gb=30.0, phys_pct=50.0), now=60.0)
        last = _pressure_events(bus)[-1].payload
        assert last["reasons"] == ["disk_low"]
        assert last["change"] == "reasons_change"

    def test_a_new_episode_announces_its_band_again(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(disk_free_gb=10.0), now=0.0)
        monitor.evaluate(make_sample(disk_free_gb=60.0), now=1000.0)
        monitor.evaluate(make_sample(disk_free_gb=10.0), now=2000.0)
        assert self._changes(bus) == ["rising_edge", "rising_edge"]
        assert _pressure_events(bus)[-1].payload["disk_band"] == "severe"


class TestCommitAndPhysBands:
    """Commit and phys carry severity bands too — added 2026-08-20.

    The 2026-08-14 band work fixed the disk axis and stopped there, so commit
    and phys kept the exact pathology the bands exist to cure. Measured on
    2026-08-20: the box reached 99.1% commit (126.09/127.20 GB), the monitor
    emitted 24 ``commit_high`` events that day, and only 8 were delivered —
    the 96.0% sample among the silent ones. ``band_changed`` was computed from
    ``disk_band_for`` alone, so once commit latched at its 85% trigger every
    later sample stamped ``sustained_repeat``, which subscribers drop bus-only.
    An escalation from 85% to 99% was structurally undeliverable.
    """

    def _changes(self, bus):
        return [e.payload["change"] for e in _pressure_events(bus)]

    def test_commit_escalation_is_delivered_not_a_sustained_repeat(self, bus):
        """The 2026-08-20 incident, reduced: commit climbs inside one episode."""
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(commit_pct=86.0), now=0.0)
        monitor.evaluate(make_sample(commit_pct=96.5), now=60.0)
        changes = self._changes(bus)
        assert changes == ["rising_edge", "band_change"]
        assert "sustained_repeat" not in changes
        assert _pressure_events(bus)[-1].payload["commit_band"] == "critical"

    def test_first_commit_breach_carries_the_high_band(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(commit_pct=86.0), now=0.0)
        p = _pressure_events(bus)[0].payload
        assert p["commit_band"] == "high"
        assert p["commit_band_edge_pct"] == 85

    def test_a_climb_past_several_commit_edges_announces_only_the_deepest(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(commit_pct=99.5), now=0.0)
        events = _pressure_events(bus)
        assert len(events) == 1
        assert events[0].payload["commit_band"] == "exhausted"

    def test_hovering_inside_a_commit_band_never_re_announces(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(commit_pct=93.0), now=0.0)
        monitor.evaluate(make_sample(commit_pct=94.0), now=60.0)
        monitor.evaluate(make_sample(commit_pct=93.5), now=120.0)
        assert self._changes(bus) == ["rising_edge"]

    def test_a_commit_dip_inside_the_band_does_not_rearm_the_edge(self, bus):
        """Mirrors the disk ratchet: only a comfortable recovery re-arms."""
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(commit_pct=96.0), now=0.0)
        monitor.evaluate(make_sample(commit_pct=93.0), now=60.0)
        monitor.evaluate(make_sample(commit_pct=96.0), now=120.0)
        assert "band_change" not in self._changes(bus)[1:]

    def test_phys_escalation_is_delivered(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(phys_pct=93.0), now=0.0)
        monitor.evaluate(make_sample(phys_pct=98.5), now=60.0)
        changes = self._changes(bus)
        assert changes == ["rising_edge", "band_change"]
        assert _pressure_events(bus)[-1].payload["phys_band"] == "critical"

    def test_a_non_commit_episode_has_no_commit_band(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(disk_free_gb=30.0), now=0.0)
        p = _pressure_events(bus)[0].payload
        assert p["reasons"] == ["disk_low"]
        assert p["commit_band"] is None
        assert p["phys_band"] is None

    def test_a_new_episode_announces_the_commit_band_again(self, bus):
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(commit_pct=96.0), now=0.0)
        monitor.evaluate(make_sample(commit_pct=50.0), now=1000.0)
        monitor.evaluate(make_sample(commit_pct=96.0), now=2000.0)
        assert self._changes(bus) == ["rising_edge", "rising_edge"]

    def test_axes_band_independently(self, bus):
        """A commit edge crossed while disk sits still is its own message."""
        monitor = ResourcePressureMonitor(bus)
        monitor.evaluate(make_sample(commit_pct=86.0, disk_free_gb=30.0), now=0.0)
        monitor.evaluate(make_sample(commit_pct=96.5, disk_free_gb=30.0), now=60.0)
        last = _pressure_events(bus)[-1].payload
        assert last["change"] == "band_change"
        assert last["commit_band"] == "critical"
        assert last["disk_band"] == "low"

    def test_every_commit_band_edge_sits_above_the_disarm(self, bus):
        """The 2026-08-14 lesson: an unreachable disarm latches an axis forever."""
        from events.producers.resource_monitor import (
            COMMIT_BANDS, PHYS_BANDS,
            DEFAULT_COMMIT_PCT_DISARM, DEFAULT_PHYS_PCT_DISARM,
        )
        assert all(edge > DEFAULT_COMMIT_PCT_DISARM for edge, _ in COMMIT_BANDS)
        assert all(edge > DEFAULT_PHYS_PCT_DISARM for edge, _ in PHYS_BANDS)
        # Ascending, and the shallowest edge IS the trigger, as disk's is.
        assert [e for e, _ in COMMIT_BANDS] == sorted(e for e, _ in COMMIT_BANDS)
        assert [e for e, _ in PHYS_BANDS] == sorted(e for e, _ in PHYS_BANDS)

    def test_a_band_edge_is_exclusive_like_the_trigger(self):
        """``pct_band_for`` uses strict ``>``, matching how the axis triggers
        (``commit_pct > commit_pct_threshold``). A reading sitting EXACTLY on an
        edge has not crossed it, so 96.0 is still ``severe``; 85.0 does not
        breach at all, exactly as the trigger does not."""
        from events.producers.resource_monitor import COMMIT_BANDS, pct_band_for
        assert pct_band_for(96.0, COMMIT_BANDS)[0] == "severe"
        assert pct_band_for(96.1, COMMIT_BANDS)[0] == "critical"
        assert pct_band_for(85.0, COMMIT_BANDS) == (None, None)
