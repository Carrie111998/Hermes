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
    WatchedRepo,
    sample_code_drift,
    watched_repos,
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
        assert s.head == s.trunk

    def test_dirty_flag(self, repo):
        (repo / "a.txt").write_text("local edit", encoding="utf-8")
        assert sample_code_drift(repo).dirty is True

    def test_missing_repo_returns_none(self, tmp_path):
        """An ABSENT repo is still a silent skip — that half is correct."""
        assert sample_code_drift(tmp_path / "nope") is None

    def test_shape_property(self):
        s = DriftSample(state="behind", head="a" * 9, trunk="b" * 9,
                        behind_count=3)
        assert s.shape == ["behind", 3, 0]


class TestTrunkRefIsParameterised:
    """~/.hermes trunk is `master` and it has NO `main` (2026-07-28)."""

    def test_missing_configured_trunk_is_misconfigured_not_none(self, repo):
        """THE DEFECT, inverted.

        Before 2026-07-28 an unresolvable trunk ref returned None, which the
        monitor treats as "nothing to evaluate". Pointing this at ~/.hermes
        (no `main` branch) would therefore have reported clean FOREVER
        rather than erroring — a no-op watcher that is believed. A present
        repo whose configured trunk is missing must now be a loud sample.
        """
        _git(repo, "branch", "-m", "main", "master")   # no `main` ref left

        s = sample_code_drift(repo, "refs/heads/main")

        assert s is not None, "a present repo must never silently return None"
        assert s.state == "misconfigured"
        assert "refs/heads/main" in s.detail
        # And it must alert, not sit in the silent in_sync branch.
        assert s.state != "in_sync"
        assert s.shape != ["in_sync", 0, 0]

    def test_master_trunk_repo_detects_real_drift(self, repo):
        """Parameterising must WORK, not merely stop erroring."""
        _git(repo, "branch", "-m", "main", "master")

        s = sample_code_drift(repo, "refs/heads/master", repo_name="hermes")

        assert s.state == "behind"
        assert s.behind_count == 1
        assert s.trunk_ref == "refs/heads/master"
        assert s.repo_name == "hermes"
        assert "second landed fix" in s.missed_subjects[0]

    def test_master_trunk_repo_in_sync(self, repo):
        _git(repo, "branch", "-m", "main", "master")
        _git(repo, "checkout", "--detach", "master")
        assert sample_code_drift(repo, "refs/heads/master").state == "in_sync"

    def test_misconfigured_repo_emits_a_loud_event(self, bus, tmp_path, repo):
        """End-to-end: the misconfiguration reaches the event bus, so it
        reaches Telegram — not just a log line nobody reads."""
        _git(repo, "branch", "-m", "main", "master")
        m = make_monitor(
            bus, tmp_path,
            repo=WatchedRepo("hermes", repo, "refs/heads/main"),  # wrong on purpose
            clock=lambda: 1000.0,
        )

        assert m.check() is not None

        events = _drift_events(bus)
        assert len(events) == 1
        p = events[0].payload
        assert p["status"] == "drifting"
        assert p["state"] == "misconfigured"
        assert p["repo_name"] == "hermes"
        assert "refs/heads/main" in p["detail"]

    def test_watched_repos_pairs_path_with_its_own_trunk(self):
        """The config surface: every entry carries its OWN trunk ref, and
        ~/.hermes is actually watched."""
        by_name = {r.name: r for r in watched_repos()}

        assert set(by_name) >= {"agent-src", "hermes"}
        assert by_name["agent-src"].trunk_ref == "refs/heads/main"
        assert by_name["hermes"].trunk_ref == "refs/heads/master"
        assert by_name["hermes"].trunk_name == "master"
        assert by_name["hermes"].path != by_name["agent-src"].path
        # agent-src is NESTED in ~/.hermes; they are separate repos and must
        # not collapse onto one entry.
        assert by_name["agent-src"].path.name == "agent-src"

    def test_executed_dir_gate_uses_a_pathspec_that_actually_matches(self):
        """The gate's own fail-silent trap.

        Measured on the live ~/.hermes 2026-07-28: a bare
        'profiles/*/scripts' pathspec matches 0 files (the trailing literal
        never aligns) and a bare 'profiles/*/scripts/**' OVER-matches at 302
        (default wildmatch lets * cross '/'). Only the ':(glob)' form scopes
        correctly. A gate that matches nothing would report "no executed
        change" forever — the same fail-silent class as the bug being fixed.
        """
        from events.producers.code_drift_monitor import HERMES_EXECUTED_DIRS

        assert HERMES_EXECUTED_DIRS, "the gate must not be empty"
        for spec in HERMES_EXECUTED_DIRS:
            assert spec.startswith(":(glob)"), f"{spec} lacks the glob prefix"
            assert spec.endswith("/**"), f"{spec} lacks the recursive suffix"

    def test_each_watched_repo_gets_its_own_state_file(self, bus):
        """Episode state must not be shared: agent-src's cooldown must never
        suppress ~/.hermes's rising edge."""
        paths = {
            r.name: CodeDriftMonitor(bus, repo=r)._state_path
            for r in watched_repos()
        }
        assert len(set(paths.values())) == len(paths)
        # agent-src keeps the legacy filename so its in-flight episode
        # survives the multi-repo cutover.
        assert paths["agent-src"].name == "code_drift_state.json"


@pytest.fixture
def gated_repo(tmp_path):
    """A ~/.hermes-shaped repo: trunk `master`, an EXECUTED dir (scripts/)
    and an inert one (docs/), HEAD detached one commit behind."""
    repo = tmp_path / "hermes"
    (repo / "scripts").mkdir(parents=True)
    (repo / "docs").mkdir()
    _git(repo, "init", "-b", "master")
    (repo / "scripts" / "run.ps1").write_text("v1", encoding="utf-8")
    (repo / "docs" / "notes.md").write_text("v1", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "--detach", "HEAD")
    return repo


def _land_on_master(repo, relpath, text, msg):
    """Land a commit on master while HEAD stays detached (the incident)."""
    head = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "master")
    p = repo / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)
    _git(repo, "checkout", "--detach", head)


def _git_out(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()


HERMES_DIRS = (":(glob)scripts/**", ":(glob)ops/**",
               ":(glob)profiles/*/scripts/**")


class TestExecutedDirGate:
    """~/.hermes carries heavy non-executed churn (docs, artifacts, backups).
    Paging on that trains the operator to ignore the channel — which is how
    the 2026-07-28 drift stayed invisible for three days. Detection stays
    on; only ALERTING narrows to executed code."""

    def test_inert_drift_is_detected_but_does_not_alert(self, gated_repo):
        _land_on_master(gated_repo, "docs/notes.md", "v2", "docs only")

        s = sample_code_drift(gated_repo, "refs/heads/master",
                              repo_name="hermes", executed_dirs=HERMES_DIRS)

        assert s.state == "behind", "drift must still be DETECTED"
        assert s.behind_count == 1
        assert s.executed_gated is True
        assert s.executed_changed is False
        assert s.alerts is False, "docs-only drift must not page"

    def test_executed_drift_alerts_and_names_the_files(self, gated_repo):
        _land_on_master(gated_repo, "scripts/run.ps1", "v2", "fix the runner")

        s = sample_code_drift(gated_repo, "refs/heads/master",
                              repo_name="hermes", executed_dirs=HERMES_DIRS)

        assert s.state == "behind"
        assert s.executed_changed is True
        assert s.alerts is True
        assert "scripts/run.ps1" in s.executed_files

    def test_profile_scripts_are_inside_the_gate(self, gated_repo):
        """The pathspec form that silently matches nothing would drop this."""
        _land_on_master(gated_repo, "profiles/main/scripts/job.py", "x",
                        "profile script")

        s = sample_code_drift(gated_repo, "refs/heads/master",
                              repo_name="hermes", executed_dirs=HERMES_DIRS)

        assert s.executed_changed is True, \
            "profiles/*/scripts must be inside the executed gate"
        assert s.alerts is True

    def test_ungated_repo_alerts_on_any_drift(self, repo):
        """agent-src has no gate: the editable install imports everything."""
        s = sample_code_drift(repo)
        assert s.executed_gated is False
        assert s.alerts is True

    def test_misconfigured_is_never_gated_into_silence(self, gated_repo):
        """If the trunk ref does not resolve we cannot compute an executed
        diff at all — silence there would recreate the fail-silent hole."""
        s = sample_code_drift(gated_repo, "refs/heads/main",
                              repo_name="hermes", executed_dirs=HERMES_DIRS)
        assert s.state == "misconfigured"
        assert s.alerts is True

    def test_inert_drift_emits_no_event(self, bus, tmp_path, gated_repo):
        _land_on_master(gated_repo, "docs/notes.md", "v2", "docs only")
        m = make_monitor(
            bus, tmp_path, clock=lambda: 0.0,
            repo=WatchedRepo("hermes", gated_repo, "refs/heads/master",
                             executed_dirs=HERMES_DIRS),
        )
        assert m.check() is None
        assert _drift_events(bus) == []

    def test_executed_drift_emits_an_event(self, bus, tmp_path, gated_repo):
        _land_on_master(gated_repo, "ops/deploy.ps1", "x", "ops change")
        m = make_monitor(
            bus, tmp_path, clock=lambda: 0.0,
            repo=WatchedRepo("hermes", gated_repo, "refs/heads/master",
                             executed_dirs=HERMES_DIRS),
        )
        assert m.check() is not None
        p = _drift_events(bus)[0].payload
        assert p["state"] == "behind"
        assert p["executed_gated"] is True
        assert "ops/deploy.ps1" in p["executed_files"]

    def test_going_inert_resolves_honestly_not_as_in_sync(self, bus, tmp_path):
        """A gated repo can stop alerting without ever merging. The closure
        ping must not claim a sync that did not happen."""
        m = make_monitor(bus, tmp_path, clock=lambda: 0.0)
        m.evaluate(behind(2), now=0.0)

        inert = DriftSample(state="behind", head="a" * 9, trunk="b" * 9,
                            behind_count=2, executed_gated=True,
                            executed_changed=False, repo_name="hermes")
        assert inert.alerts is False
        assert m.evaluate(inert, now=60.0) is not None

        p = _drift_events(bus)[-1].payload
        assert p["status"] == "resolved"
        assert p["inert"] is True
        assert p["state"] == "behind"   # still not merged — stated plainly


def behind(n=1, dirty=False):
    return DriftSample(state="behind", head="a" * 9, trunk="b" * 9,
                       behind_count=n, dirty=dirty,
                       missed_subjects=tuple(f"c{i} fix {i}" for i in range(min(n, 5))))


def in_sync():
    return DriftSample(state="in_sync", head="b" * 9, trunk="b" * 9)


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
