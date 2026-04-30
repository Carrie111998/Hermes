"""Tests for events.producers.devflow_pr_build_poller (GitHub API polling).

Added 2026-04-30 alongside the GitHub-poller producer that fills the
PR/build slice of the devflow_firehose Telegram topic. Spec at
docs/superpowers/specs/2026-04-30-devflow-pr-build-events.md (Option 1).

The poller fetches recent PRs + their check-runs from the GitHub REST
API for a configured set of repos and forwards transitions through the
existing ``PrBuildStateTracker``. Tests use injected fetcher callables
so no real HTTP is involved.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from events.bus import EventBus
from events.schema import EventType


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "state" / "devflow_pr_build_state.json"


def _make_config(tmp_path, state_path, repos=("owner/repo-a",)):
    from events.producers.devflow_pr_build_poller import PollerConfig
    return PollerConfig(
        repos=list(repos),
        token="ghp_fake",
        state_path=state_path,
    )


# ------------------------------------------------------------- state derivation

class TestDerivePrState:
    def test_open_pr_with_no_reviewers(self):
        from events.producers.devflow_pr_build_poller import derive_pr_state
        assert derive_pr_state({"state": "open", "merged_at": None}) == "open"

    def test_open_pr_with_requested_reviewers(self):
        from events.producers.devflow_pr_build_poller import derive_pr_state
        pr = {"state": "open", "merged_at": None,
              "requested_reviewers": [{"login": "reviewer1"}]}
        assert derive_pr_state(pr) == "review_requested"

    def test_merged_pr(self):
        from events.producers.devflow_pr_build_poller import derive_pr_state
        pr = {"state": "closed", "merged_at": "2026-04-30T12:00:00Z"}
        assert derive_pr_state(pr) == "merged"

    def test_closed_without_merge(self):
        from events.producers.devflow_pr_build_poller import derive_pr_state
        assert derive_pr_state({"state": "closed", "merged_at": None}) == "closed"


class TestDeriveBuildState:
    def test_in_progress_check_run(self):
        from events.producers.devflow_pr_build_poller import derive_build_state
        assert derive_build_state({"status": "in_progress"}) == "in_progress"

    def test_queued_check_run(self):
        from events.producers.devflow_pr_build_poller import derive_build_state
        assert derive_build_state({"status": "queued"}) == "queued"

    def test_completed_success(self):
        from events.producers.devflow_pr_build_poller import derive_build_state
        run = {"status": "completed", "conclusion": "success"}
        assert derive_build_state(run) == "succeeded"

    def test_completed_failure(self):
        from events.producers.devflow_pr_build_poller import derive_build_state
        run = {"status": "completed", "conclusion": "failure"}
        assert derive_build_state(run) == "failed"

    def test_completed_timed_out_is_failed(self):
        from events.producers.devflow_pr_build_poller import derive_build_state
        run = {"status": "completed", "conclusion": "timed_out"}
        assert derive_build_state(run) == "failed"

    def test_completed_skipped_returns_empty(self):
        """Skipped/neutral conclusions don't map to an event."""
        from events.producers.devflow_pr_build_poller import derive_build_state
        run = {"status": "completed", "conclusion": "skipped"}
        assert derive_build_state(run) == ""


# ------------------------------------------------------------- run_poll happy path

class TestRunPollEmitsTransitions:
    def test_open_pr_emits_pr_opened(self, bus, tmp_path, state_path):
        from events.producers.devflow_pr_build_poller import run_poll

        prs_by_repo = {
            "owner/repo-a": [{
                "number": 42,
                "state": "open",
                "merged_at": None,
                "title": "Add feature X",
                "user": {"login": "diego"},
                "html_url": "https://github.com/owner/repo-a/pull/42",
                "head": {"sha": "abc123"},
            }],
        }

        def fake_fetch_prs(repo, config):
            return prs_by_repo.get(repo, [])

        def fake_fetch_builds(repo, sha, config):
            return []

        config = _make_config(tmp_path, state_path)
        summary = run_poll(bus, config, fetch_prs=fake_fetch_prs,
                          fetch_builds=fake_fetch_builds)

        assert summary.repos_polled == 1
        assert summary.prs_observed == 1
        assert summary.transitions_emitted == 1
        events = bus.query(event_type=EventType.DEVFLOW_PR_OPENED)
        assert len(events) == 1
        assert events[0].payload["pr_number"] == 42
        assert events[0].payload["author"] == "diego"
        assert events[0].payload["url"] == "https://github.com/owner/repo-a/pull/42"

    def test_merged_pr_emits_pr_merged(self, bus, tmp_path, state_path):
        from events.producers.devflow_pr_build_poller import run_poll

        prs = [{
            "number": 7,
            "state": "closed",
            "merged_at": "2026-04-30T12:00:00Z",
            "title": "Fix bug",
            "user": {"login": "diego"},
            "merged_by": {"login": "reviewer"},
            "html_url": "https://github.com/owner/repo-a/pull/7",
            "head": {"sha": "def456"},
        }]
        config = _make_config(tmp_path, state_path)
        summary = run_poll(bus, config,
                          fetch_prs=lambda r, c: prs,
                          fetch_builds=lambda r, s, c: [])

        assert summary.transitions_emitted == 1
        events = bus.query(event_type=EventType.DEVFLOW_PR_MERGED)
        assert len(events) == 1
        assert events[0].payload["pr_number"] == 7
        assert events[0].payload["merged_by"] == "reviewer"

    def test_check_run_failure_emits_build_failed(self, bus, tmp_path, state_path):
        from events.producers.devflow_pr_build_poller import run_poll

        prs = [{
            "number": 1,
            "state": "open",
            "merged_at": None,
            "title": "WIP",
            "user": {"login": "diego"},
            "head": {"sha": "sha-1"},
        }]
        builds = [{
            "id": 9001,
            "name": "ci/build",
            "status": "completed",
            "conclusion": "failure",
            "head_sha": "sha-1",
            "html_url": "https://github.com/owner/repo-a/runs/9001",
            "output": {"title": "Compile error"},
        }]
        config = _make_config(tmp_path, state_path)
        summary = run_poll(bus, config,
                          fetch_prs=lambda r, c: prs,
                          fetch_builds=lambda r, s, c: builds)

        events = bus.query(event_type=EventType.DEVFLOW_BUILD_FAILED)
        assert len(events) == 1
        evt = events[0]
        assert evt.payload["build_id"] == "9001"
        assert evt.payload["build_name"] == "ci/build"
        assert evt.payload["error_summary"] == "Compile error"
        assert evt.payload["commit_sha"] == "sha-1"

    def test_repeated_poll_does_not_re_emit_same_state(self, bus, tmp_path, state_path):
        """Two ticks observing the same open PR must produce one event total."""
        from events.producers.devflow_pr_build_poller import run_poll

        prs = [{
            "number": 5,
            "state": "open",
            "merged_at": None,
            "title": "T",
            "user": {"login": "diego"},
            "head": {"sha": "abc"},
        }]
        config = _make_config(tmp_path, state_path)
        run_poll(bus, config, fetch_prs=lambda r, c: prs,
                 fetch_builds=lambda r, s, c: [])
        run_poll(bus, config, fetch_prs=lambda r, c: prs,
                 fetch_builds=lambda r, s, c: [])

        assert len(bus.query(event_type=EventType.DEVFLOW_PR_OPENED)) == 1


class TestRunPollSkipsClosedPrCheckRuns:
    def test_merged_or_closed_pr_skips_check_run_fetch(self, bus, tmp_path, state_path):
        """Closed/merged PRs should not trigger a /check-runs fetch — keeps
        per-tick API cost bounded as old PRs accumulate in lookback."""
        from events.producers.devflow_pr_build_poller import run_poll

        prs = [{
            "number": 1,
            "state": "closed",
            "merged_at": "2026-04-30T12:00:00Z",
            "title": "T",
            "user": {"login": "diego"},
            "head": {"sha": "abc"},
        }]
        builds_calls = []

        def spy_fetch_builds(repo, sha, config):
            builds_calls.append((repo, sha))
            return []

        config = _make_config(tmp_path, state_path)
        run_poll(bus, config, fetch_prs=lambda r, c: prs,
                 fetch_builds=spy_fetch_builds)

        assert builds_calls == []  # no fetch issued for merged PR


class TestRunPollErrorIsolation:
    def test_pr_fetch_error_on_one_repo_does_not_stop_others(self, bus, tmp_path, state_path):
        from events.producers.devflow_pr_build_poller import (
            GitHubAPIError, run_poll,
        )

        def fake_fetch_prs(repo, config):
            if repo == "owner/broken":
                raise GitHubAPIError("boom")
            return [{
                "number": 1, "state": "open", "merged_at": None,
                "title": "T", "user": {"login": "diego"},
                "head": {"sha": "abc"},
            }]

        config = _make_config(tmp_path, state_path,
                              repos=("owner/broken", "owner/works"))
        summary = run_poll(bus, config,
                          fetch_prs=fake_fetch_prs,
                          fetch_builds=lambda r, s, c: [])

        assert summary.repos_polled == 1  # only the working one
        assert summary.transitions_emitted == 1
        assert any("boom" in e for e in summary.errors)

    def test_check_run_fetch_error_does_not_stop_pr_emission(self, bus, tmp_path, state_path):
        from events.producers.devflow_pr_build_poller import (
            GitHubAPIError, run_poll,
        )

        prs = [{
            "number": 1, "state": "open", "merged_at": None,
            "title": "T", "user": {"login": "diego"},
            "head": {"sha": "abc"},
        }]

        def fail_builds(*a, **kw):
            raise GitHubAPIError("network")

        config = _make_config(tmp_path, state_path)
        summary = run_poll(bus, config,
                          fetch_prs=lambda r, c: prs,
                          fetch_builds=fail_builds)

        # PR transition still emits even though build fetch failed
        assert summary.transitions_emitted == 1
        assert any("network" in e for e in summary.errors)


# ------------------------------------------------------------- config loading

class TestLoadConfig:
    def test_returns_none_when_token_missing(self, tmp_path, monkeypatch):
        from events.producers.devflow_pr_build_poller import load_config
        monkeypatch.delenv("HERMES_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        repos_path = tmp_path / "repos.json"
        repos_path.write_text(json.dumps({"repos": ["o/r"]}))
        assert load_config(repos_path=repos_path) is None

    def test_returns_none_when_repos_file_missing(self, tmp_path, monkeypatch):
        from events.producers.devflow_pr_build_poller import load_config
        monkeypatch.setenv("HERMES_GITHUB_TOKEN", "ghp_fake")
        repos_path = tmp_path / "missing.json"
        assert load_config(repos_path=repos_path) is None

    def test_returns_none_when_repos_list_empty(self, tmp_path, monkeypatch):
        from events.producers.devflow_pr_build_poller import load_config
        monkeypatch.setenv("HERMES_GITHUB_TOKEN", "ghp_fake")
        repos_path = tmp_path / "repos.json"
        repos_path.write_text(json.dumps({"repos": []}))
        assert load_config(repos_path=repos_path) is None

    def test_parses_valid_config(self, tmp_path, monkeypatch):
        from events.producers.devflow_pr_build_poller import load_config
        monkeypatch.setenv("HERMES_GITHUB_TOKEN", "ghp_fake")
        repos_path = tmp_path / "repos.json"
        repos_path.write_text(json.dumps({"repos": ["o/r1", "o/r2"]}))
        cfg = load_config(repos_path=repos_path)
        assert cfg is not None
        assert cfg.repos == ["o/r1", "o/r2"]
        assert cfg.token == "ghp_fake"

    def test_falls_back_to_GITHUB_TOKEN(self, tmp_path, monkeypatch):
        from events.producers.devflow_pr_build_poller import load_config
        monkeypatch.delenv("HERMES_GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "gh-from-fallback")
        repos_path = tmp_path / "repos.json"
        repos_path.write_text(json.dumps({"repos": ["o/r"]}))
        cfg = load_config(repos_path=repos_path)
        assert cfg is not None
        assert cfg.token == "gh-from-fallback"
