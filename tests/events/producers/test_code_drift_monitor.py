"""Tests for events.producers.code_drift_monitor — CodeDriftMonitor.

The monitor probes the shared detached checkout (~/.hermes/agent-src) with
read-only git and emits CODE_DRIFT on the rising edge of HEAD != main.
Added 2026-07-21 after three restart cycles ran stale code (2026-07-20/21).

The edge core takes an injected sampler + wall clock; only the
sample_code_drift() unit tests below touch real git, against a throwaway
tmp_path repo.
"""

import subprocess

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.producers.code_drift_monitor import (
    CodeDriftMonitor,
    DriftSample,
    sample_code_drift,
)


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


def _drift_events(bus):
    return bus.query(event_type=EventType.CODE_DRIFT)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True, timeout=30,
    )


@pytest.fixture
def repo(tmp_path):
    """Throwaway repo: two commits on main, HEAD detached at the first
    (i.e. the deployed checkout LAGS main by 1 — the incident shape)."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("one", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "first")
    (repo / "a.txt").write_text("two", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "second landed fix")
    _git(repo, "checkout", "--detach", "HEAD~1")
    return repo


class TestSampleCodeDrift:
    def test_behind_detached_checkout(self, repo):
        s = sample_code_drift(repo)
        assert s.state == "behind"
        assert s.behind_count == 1
        assert s.ahead_count == 0
        assert s.dirty is False
        assert len(s.missed_subjects) == 1
        assert "second landed fix" in s.missed_subjects[0]

    def test_in_sync(self, repo):
        _git(repo, "checkout", "--detach", "main")
        s = sample_code_drift(repo)
        assert s.state == "in_sync"
        assert s.head == s.main

    def test_dirty_flag(self, repo):
        (repo / "a.txt").write_text("local edit", encoding="utf-8")
        assert sample_code_drift(repo).dirty is True

    def test_missing_repo_returns_none(self, tmp_path):
        assert sample_code_drift(tmp_path / "nope") is None

    def test_shape_property(self):
        s = DriftSample(state="behind", head="a" * 9, main="b" * 9,
                        behind_count=3)
        assert s.shape == ["behind", 3, 0]


def behind(n=1, dirty=False):
    return DriftSample(state="behind", head="a" * 9, main="b" * 9,
                       behind_count=n, dirty=dirty,
                       missed_subjects=tuple(f"c{i} fix {i}" for i in range(min(n, 5))))


def in_sync():
    return DriftSample(state="in_sync", head="b" * 9, main="b" * 9)


def make_monitor(bus, tmp_path, **kw):
    kw.setdefault("state_path", tmp_path / "code_drift_state.json")
    kw.setdefault("check_interval_seconds", 900.0)
    return CodeDriftMonitor(bus, **kw)


class TestRisingEdge:
    def test_first_drift_emits_full_payload(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path)
        assert m.evaluate(behind(3), now=1000.0)
        events = _drift_events(bus)
        assert len(events) == 1
        p = events[0].payload
        assert p["status"] == "drifting"
        assert p["state"] == "behind"
        assert p["behind_count"] == 3
        assert p["dirty"] is False
        assert len(p["missed_subjects"]) == 3
        assert events[0].priority is Priority.HIGH

    def test_same_shape_within_cooldown_is_silent(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path)
        m.evaluate(behind(3), now=1000.0)
        assert m.evaluate(behind(3), now=1000.0 + 3600) is None
        assert len(_drift_events(bus)) == 1

    def test_in_sync_never_alerted_is_silent(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path)
        assert m.evaluate(in_sync(), now=1000.0) is None
        assert _drift_events(bus) == []


class TestSustainedEpisode:
    def test_re_pings_after_cooldown(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path)
        m.evaluate(behind(3), now=0.0)
        assert m.evaluate(behind(3), now=6 * 3600.0) is not None
        assert len(_drift_events(bus)) == 2

    def test_shape_change_bypasses_cooldown(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path)
        m.evaluate(behind(3), now=0.0)
        # Two more commits land on main 10 min later: alert NOW.
        assert m.evaluate(behind(5), now=600.0) is not None
        assert _drift_events(bus)[-1].payload["behind_count"] == 5


class TestFallingEdge:
    def test_resolved_emitted_once(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path)
        m.evaluate(behind(1), now=0.0)
        assert m.evaluate(in_sync(), now=60.0) is not None
        assert m.evaluate(in_sync(), now=120.0) is None
        events = _drift_events(bus)
        assert len(events) == 2
        assert events[-1].payload["status"] == "resolved"

    def test_relapse_after_resolve_fires_immediately(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path)
        m.evaluate(behind(1), now=0.0)
        m.evaluate(in_sync(), now=60.0)
        # New drift 2 min later — rising edge again, no cooldown wait.
        assert m.evaluate(behind(1), now=180.0) is not None
        assert len(_drift_events(bus)) == 3


class TestRestartSurvival:
    def test_resolved_fires_from_persisted_state(self, bus, tmp_path):
        """The common remediation is FF-then-restart: the fresh process must
        still emit the resolved ping."""
        m1 = make_monitor(bus, tmp_path)
        m1.evaluate(behind(2), now=0.0)
        # Gateway restarts onto the FF'd checkout: brand-new monitor, same
        # state file, checkout now in sync.
        m2 = make_monitor(bus, tmp_path)
        assert m2.evaluate(in_sync(), now=300.0) is not None
        assert _drift_events(bus)[-1].payload["status"] == "resolved"

    def test_restart_mid_episode_stays_quiet(self, bus, tmp_path):
        """Restart WITHOUT the FF (still drifting, same shape, inside the
        cooldown): no duplicate alert."""
        m1 = make_monitor(bus, tmp_path)
        m1.evaluate(behind(2), now=0.0)
        m2 = make_monitor(bus, tmp_path)
        assert m2.evaluate(behind(2), now=600.0) is None
        assert len(_drift_events(bus)) == 1


class TestCheckGating:
    def test_none_sample_is_noop(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path,
                         sampler=lambda: None, clock=lambda: 0.0)
        assert m.check() is None
        assert _drift_events(bus) == []

    def test_check_respects_interval(self, bus, tmp_path):
        calls = []
        t = {"now": 0.0}
        m = make_monitor(
            bus, tmp_path,
            sampler=lambda: calls.append(1) or in_sync(),
            clock=lambda: t["now"],
        )
        m.check()
        t["now"] = 60.0
        m.check()          # inside the 15-min gate — no second git probe
        assert len(calls) == 1
        t["now"] = 901.0
        m.check()
        assert len(calls) == 2

    def test_sampler_exception_never_raises(self, bus, tmp_path):
        def boom():
            raise RuntimeError("git exploded")
        m = make_monitor(bus, tmp_path, sampler=boom, clock=lambda: 0.0)
        assert m.check() is None
