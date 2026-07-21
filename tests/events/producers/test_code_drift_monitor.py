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
