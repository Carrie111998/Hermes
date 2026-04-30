"""GitHub API poller -> DEVFLOW_PR_* / DEVFLOW_BUILD_* events.

Option 1 from docs/superpowers/specs/2026-04-30-devflow-pr-build-events.md.

The poller fetches the most recent PRs (and their HEAD-sha check-runs)
from the GitHub REST API for a configured set of repos, derives a
state string for each, and forwards the observation through the
``PrBuildStateTracker`` -- which emits the right transition event only
when state actually changes.

It runs as a single-tick function (``run_poll``); no internal cadence,
no daemon. The cron wrapper at ``~/.hermes/scripts/devflow_pr_build_poll.py``
calls it on whatever cadence the cron entry sets (default */15).

Configuration is intentionally optional. If neither ``HERMES_GITHUB_TOKEN``
(nor the standard ``GITHUB_TOKEN`` fallback) is set, or if the repos
config file doesn't exist, ``load_config`` returns ``None`` and the
wrapper exits clean -- so the cron entry can ship enabled without
spamming errors before the user has set the token.

Stdlib ``urllib`` is used deliberately to avoid pulling ``requests``
into ``events/`` purely for one HTTP call. The fetcher functions are
tiny and parameterised so tests inject fakes without monkey-patching.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional

from events.bus import EventBus
from events.paths import _root
from events.producers.devflow_pr_build import PrBuildStateTracker

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
DEFAULT_USER_AGENT = "hermes-devflow-pr-poller/1.0"


class PollerConfigError(RuntimeError):
    """Raised when repos.json exists but is unparseable."""


class GitHubAPIError(RuntimeError):
    """Raised on any HTTP/network failure talking to GitHub."""


@dataclass
class PollerConfig:
    repos: List[str]
    token: str
    state_path: Path
    pr_lookback_count: int = 50
    timeout_seconds: float = 30.0
    user_agent: str = DEFAULT_USER_AGENT


@dataclass
class PollSummary:
    repos_polled: int = 0
    prs_observed: int = 0
    builds_observed: int = 0
    transitions_emitted: int = 0
    errors: List[str] = field(default_factory=list)


# ------------------------------------------------------------- state mapping

def derive_pr_state(pr: dict) -> str:
    """Map a GitHub PR JSON to the state string the tracker understands.

    Order matters: a PR can be ``state=closed`` AND ``merged_at`` set --
    that's a merge, not a close. Check merged first.
    """
    if pr.get("merged_at"):
        return "merged"
    if pr.get("state") == "closed":
        return "closed"
    if pr.get("requested_reviewers") or pr.get("requested_teams"):
        return "review_requested"
    return "open"


def derive_build_state(check_run: dict) -> str:
    """Map a GitHub check-run JSON to the state string the tracker understands.

    Returns "" for skipped/neutral conclusions -- the tracker treats an
    empty state as "no event to emit" but still persists it so a later
    transition fires correctly.
    """
    status = check_run.get("status") or ""
    if status == "completed":
        conclusion = check_run.get("conclusion") or ""
        if conclusion == "success":
            return "succeeded"
        if conclusion in ("failure", "timed_out", "cancelled", "action_required"):
            return "failed"
        return ""
    if status in ("queued", "in_progress"):
        return status
    return ""


# ------------------------------------------------------------- HTTP

def _http_get_json(url: str, token: str, timeout: float, user_agent: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": user_agent,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise GitHubAPIError(f"GET {url} -> HTTP {status}")
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise GitHubAPIError(f"GET {url} -> HTTP {e.code} ({e.reason})") from e
    except urllib.error.URLError as e:
        raise GitHubAPIError(f"GET {url} -> {e.reason}") from e
    except (TimeoutError, ConnectionError) as e:
        raise GitHubAPIError(f"GET {url} -> {e}") from e


def fetch_recent_prs(repo: str, config: PollerConfig) -> List[dict]:
    url = (
        f"{GITHUB_API}/repos/{repo}/pulls"
        f"?state=all&sort=updated&direction=desc"
        f"&per_page={config.pr_lookback_count}"
    )
    data = _http_get_json(url, config.token, config.timeout_seconds, config.user_agent)
    return data if isinstance(data, list) else []


def fetch_check_runs(repo: str, head_sha: str, config: PollerConfig) -> List[dict]:
    url = f"{GITHUB_API}/repos/{repo}/commits/{head_sha}/check-runs"
    data = _http_get_json(url, config.token, config.timeout_seconds, config.user_agent)
    if isinstance(data, dict):
        runs = data.get("check_runs")
        return runs if isinstance(runs, list) else []
    return []


# ------------------------------------------------------------- config

def default_repos_path() -> Path:
    return _root() / "devflow" / "repos.json"


def default_state_path() -> Path:
    return _root() / "events" / "devflow_pr_build_state.json"


def load_config(
    *,
    repos_path: Optional[Path] = None,
    state_path: Optional[Path] = None,
) -> Optional[PollerConfig]:
    """Load config from env + repos.json. Returns None if not configured.

    A None return is the green-path "no-op" signal -- the cron wrapper
    treats it as "not configured yet, exit clean". An exception is only
    raised for configs that ARE present but malformed (so silent typos
    don't waste the user's day).
    """
    token = os.environ.get("HERMES_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return None
    repos_path = repos_path or default_repos_path()
    if not repos_path.exists():
        return None
    try:
        raw = json.loads(repos_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise PollerConfigError(f"{repos_path} unreadable: {e}")
    repos = raw.get("repos") if isinstance(raw, dict) else None
    if not repos:
        return None
    return PollerConfig(
        repos=list(repos),
        token=token,
        state_path=state_path or default_state_path(),
    )


# ------------------------------------------------------------- poll

PrFetcher = Callable[[str, PollerConfig], List[dict]]
BuildFetcher = Callable[[str, str, PollerConfig], List[dict]]


def run_poll(
    bus: EventBus,
    config: PollerConfig,
    *,
    fetch_prs: Optional[PrFetcher] = None,
    fetch_builds: Optional[BuildFetcher] = None,
) -> PollSummary:
    """Single-tick poll of all configured repos. Idempotent across ticks
    via ``PrBuildStateTracker``.

    Per-repo and per-PR errors are collected into ``summary.errors`` but
    do not abort the poll -- a 404 on one repo or a transient 503 on
    one check-runs call must not silence the rest of the firehose.
    """
    fetch_prs = fetch_prs or fetch_recent_prs
    fetch_builds = fetch_builds or fetch_check_runs

    summary = PollSummary()
    tracker = PrBuildStateTracker(config.state_path)

    for repo in config.repos:
        try:
            prs = fetch_prs(repo, config)
        except GitHubAPIError as e:
            summary.errors.append(f"{repo} pulls: {e}")
            continue
        summary.repos_polled += 1

        for pr in prs:
            pr_number = pr.get("number")
            if pr_number is None:
                continue
            summary.prs_observed += 1

            state = derive_pr_state(pr)
            run_id = f"github-pr-{repo}-{pr_number}"
            extra = _pr_extras(pr)
            try:
                emitted = tracker.observe_pr(
                    bus,
                    repo=repo,
                    pr_number=pr_number,
                    state=state,
                    run_id=run_id,
                    title=pr.get("title", "") or "",
                    author=(pr.get("user") or {}).get("login"),
                    **extra,
                )
            except Exception as e:  # producer / bus error
                summary.errors.append(f"{repo}#{pr_number} observe_pr: {e}")
                continue
            if emitted:
                summary.transitions_emitted += 1

            if state in ("merged", "closed"):
                continue
            head_sha = (pr.get("head") or {}).get("sha")
            if not head_sha:
                continue
            try:
                runs = fetch_builds(repo, head_sha, config)
            except GitHubAPIError as e:
                summary.errors.append(f"{repo}#{pr_number} check-runs: {e}")
                continue

            for run in runs:
                build_id = str(run.get("id") or "")
                if not build_id:
                    continue
                summary.builds_observed += 1
                bstate = derive_build_state(run)
                if not bstate:
                    continue
                bextra = _build_extras(run, bstate)
                try:
                    emitted = tracker.observe_build(
                        bus,
                        repo=repo,
                        build_id=build_id,
                        state=bstate,
                        run_id=f"github-build-{repo}-{build_id}",
                        build_name=run.get("name") or "",
                        **bextra,
                    )
                except Exception as e:
                    summary.errors.append(
                        f"{repo} build {build_id} observe_build: {e}"
                    )
                    continue
                if emitted:
                    summary.transitions_emitted += 1

    return summary


def _pr_extras(pr: dict) -> dict:
    extra: dict = {}
    if pr.get("html_url"):
        extra["url"] = pr["html_url"]
    if pr.get("merged_at"):
        merged_by = (pr.get("merged_by") or {}).get("login")
        if merged_by:
            extra["merged_by"] = merged_by
    reviewers = pr.get("requested_reviewers") or []
    reviewer_logins = [r.get("login") for r in reviewers if r.get("login")]
    if reviewer_logins:
        extra["reviewers"] = reviewer_logins
    branch = (pr.get("head") or {}).get("ref")
    if branch:
        extra["branch"] = branch
    base = (pr.get("base") or {}).get("ref")
    if base:
        extra["base_branch"] = base
    return extra


def _build_extras(run: dict, bstate: str) -> dict:
    extra: dict = {}
    if run.get("html_url"):
        extra["url"] = run["html_url"]
    if run.get("head_sha"):
        extra["commit_sha"] = run["head_sha"]
    if bstate == "failed":
        title = (run.get("output") or {}).get("title")
        extra["error_summary"] = title or run.get("conclusion") or ""
    return extra


__all__ = [
    "GitHubAPIError",
    "PollerConfig",
    "PollerConfigError",
    "PollSummary",
    "default_repos_path",
    "default_state_path",
    "derive_build_state",
    "derive_pr_state",
    "fetch_check_runs",
    "fetch_recent_prs",
    "load_config",
    "run_poll",
]
