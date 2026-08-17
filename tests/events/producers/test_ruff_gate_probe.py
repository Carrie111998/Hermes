"""Tests for events.producers.ruff_gate_probe — RuffGateProbe.

The probe lints the whole agent-src tree on a 15-minute Scheduled Task and
emits DEVFLOW_BUILD_FAILED on the RISING edge of a red gate. Added 2026-08-17
after a ruff F-group break reached main through the pre-commit gate's blind
spots (rebase replay / cherry-pick / merge commits fire the hook zero times).

The edge core is exercised against an injected sampler and wall clock — no
ruff subprocess, no git. The two parser tests below run REAL ruff against a
throwaway tmp_path tree, because the exit-code contract (0 clean / 1 red /
>=2 unmeasurable) is the thing most likely to shift under a ruff upgrade.
"""

import json
import subprocess
import sys

import pytest

import events.producers.ruff_gate_probe as probe_module

from events.bus import EventBus
from events.schema import EventType, Priority
from events.producers.ruff_gate_probe import (
    RuffGateProbe,
    RuffSample,
    operation_in_progress,
    run_ruff,
)

# Spawning ruff/git is the load-sensitive part on this host; the suite-wide
# per-test cap can be outrun under the nightly gate's concurrent workers.
pytestmark = pytest.mark.timeout(300)


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "notifications" / "ruff_gate_state.json"


@pytest.fixture
def repo(tmp_path):
    """A directory that merely has to EXIST — the sampler is injected."""
    d = tmp_path / "agent-src"
    d.mkdir()
    return d


def _failures(bus):
    return bus.query(event_type=EventType.DEVFLOW_BUILD_FAILED)


def _red(violations=2, codes=None):
    return RuffSample(
        ok=True, red=True, violations=violations,
        codes=codes if codes is not None else {"F401": 2},
        sample=["events/foo.py:12: F401 `os` imported but unused"],
    )


_GREEN = RuffSample(ok=True, red=False)


def _make(bus, repo, state_path, monkeypatch, sample, **kwargs):
    """Build a probe whose ruff run and git identity are stubbed."""
    monkeypatch.setattr(probe_module, "run_ruff", lambda *a, **k: sample)
    monkeypatch.setattr(probe_module, "operation_in_progress", lambda *a: False)
    monkeypatch.setattr(
        probe_module, "describe_checkout",
        lambda *a: {"branch": "main", "commit": "abc1234"},
    )
    return RuffGateProbe(
        bus=bus, repo_path=repo, state_path=state_path, **kwargs
    )


# --------------------------------------------------------------------------
# Edge behaviour — the reason this module exists
# --------------------------------------------------------------------------


def test_rising_edge_emits(bus, repo, state_path, monkeypatch):
    probe = _make(bus, repo, state_path, monkeypatch, _red())
    assert probe.check(now=1000.0) is not None

    events = _failures(bus)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["build_name"] == "ruff F-group gate"
    assert payload["repo"] == "agent-src"
    assert payload["branch"] == "main"
    assert payload["violations"] == 2
    assert payload["codes"] == {"F401": 2}
    assert "F401x2" in payload["error_summary"]
    assert events[0].priority is Priority.HIGH


def test_unchanged_repeat_is_silent(bus, repo, state_path, monkeypatch):
    """A red gate on a 15-minute task must not emit 96 times a day."""
    probe = _make(bus, repo, state_path, monkeypatch, _red())
    assert probe.check(now=1000.0) is not None
    for tick in range(1, 12):
        assert probe.check(now=1000.0 + tick * 900.0) is None
    assert len(_failures(bus)) == 1


def test_shape_change_re_emits(bus, repo, state_path, monkeypatch):
    probe = _make(bus, repo, state_path, monkeypatch, _red())
    probe.check(now=1000.0)

    monkeypatch.setattr(
        probe_module, "run_ruff",
        lambda *a, **k: _red(violations=3, codes={"F401": 2, "F841": 1}),
    )
    assert probe.check(now=1900.0) is not None
    assert len(_failures(bus)) == 2


def test_sustained_episode_re_pings_after_cooldown(
    bus, repo, state_path, monkeypatch
):
    probe = _make(
        bus, repo, state_path, monkeypatch, _red(),
        re_alert_cooldown_seconds=6 * 3600.0,
    )
    probe.check(now=1000.0)
    assert probe.check(now=1000.0 + 3 * 3600.0) is None
    assert probe.check(now=1000.0 + 6 * 3600.0) is not None
    assert len(_failures(bus)) == 2


def test_falling_edge_clears_state_without_emitting(
    bus, repo, state_path, monkeypatch
):
    """No all-clear event, but the NEXT break must re-alert."""
    probe = _make(bus, repo, state_path, monkeypatch, _red())
    probe.check(now=1000.0)

    monkeypatch.setattr(probe_module, "run_ruff", lambda *a, **k: _GREEN)
    assert probe.check(now=1900.0) is None
    assert len(_failures(bus)) == 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["alerting"] is False

    monkeypatch.setattr(probe_module, "run_ruff", lambda *a, **k: _red())
    assert probe.check(now=2800.0) is not None
    assert len(_failures(bus)) == 2


def test_episode_survives_a_new_process(bus, repo, state_path, monkeypatch):
    """Each Scheduled Task tick is a FRESH process — the debounce only works
    if it rehydrates from disk rather than from instance memory."""
    _make(bus, repo, state_path, monkeypatch, _red()).check(now=1000.0)
    assert _make(bus, repo, state_path, monkeypatch, _red()).check(now=1900.0) is None
    assert len(_failures(bus)) == 1


# --------------------------------------------------------------------------
# Spurious-alert guards
# --------------------------------------------------------------------------


def test_skips_while_a_rebase_is_in_progress(bus, repo, state_path, monkeypatch):
    probe = _make(bus, repo, state_path, monkeypatch, _red())
    monkeypatch.setattr(probe_module, "operation_in_progress", lambda *a: True)
    assert probe.check(now=1000.0) is None
    assert _failures(bus) == []
    assert not state_path.exists()


def test_unmeasurable_ruff_does_not_alert_or_disturb_state(
    bus, repo, state_path, monkeypatch
):
    """ruff exiting >=2 is a broken probe, not a red gate — and must not
    fabricate a recovery for an episode already in flight."""
    probe = _make(bus, repo, state_path, monkeypatch, _red())
    probe.check(now=1000.0)

    monkeypatch.setattr(
        probe_module, "run_ruff",
        lambda *a, **k: RuffSample(ok=False, detail="ruff exited 2: bad config"),
    )
    assert probe.check(now=1900.0) is None
    assert len(_failures(bus)) == 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["alerting"] is True


def test_missing_repo_is_a_skip(bus, tmp_path, state_path, monkeypatch):
    probe = _make(
        bus, tmp_path / "gone", state_path, monkeypatch, _red(),
    )
    assert probe.check(now=1000.0) is None
    assert _failures(bus) == []


# --------------------------------------------------------------------------
# Real-ruff exit-code contract
# --------------------------------------------------------------------------


def _write_ruff_tree(root, body):
    (root / "ruff.toml").write_text(
        'line-length = 100\n[lint]\nselect = ["F"]\n', encoding="utf-8"
    )
    (root / "mod.py").write_text(body, encoding="utf-8")


@pytest.mark.skipif(
    subprocess.run(
        [sys.executable, "-m", "ruff", "--version"], capture_output=True
    ).returncode != 0,
    reason="ruff not installed",
)
def test_run_ruff_reads_a_clean_tree_as_green(tmp_path):
    _write_ruff_tree(tmp_path, "x = 1\nprint(x)\n")
    sample = run_ruff(tmp_path)
    assert sample.ok is True
    assert sample.red is False


@pytest.mark.skipif(
    subprocess.run(
        [sys.executable, "-m", "ruff", "--version"], capture_output=True
    ).returncode != 0,
    reason="ruff not installed",
)
def test_run_ruff_parses_a_real_f_group_violation(tmp_path):
    _write_ruff_tree(tmp_path, "import os\n")
    sample = run_ruff(tmp_path)
    assert sample.ok is True
    assert sample.red is True
    assert sample.codes.get("F401") == 1
    assert any("F401" in line for line in sample.sample)


def test_operation_in_progress_is_false_on_a_non_repo(tmp_path):
    assert operation_in_progress(tmp_path) is False
