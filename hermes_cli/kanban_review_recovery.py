"""Trustworthy recovery evidence for blocked Kanban implementations.

This module is deliberately independent of the Kanban database.  It turns a
worker-supplied handoff into a small, immutable candidate and verifies that the
candidate still describes an open, non-draft pull request whose exact head has
completed green checks.  The database layer owns the state transition and
uses the helpers here outside its write transaction so provider latency never
holds the board lock.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Optional

_log = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH_RE = re.compile(r"^[^\x00-\x1f\x7f\s]{1,255}$")
_PR_URL_RE = re.compile(
    r"^https://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)/?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReviewEvidence:
    """The immutable source identity of an implementation review handoff."""

    provider: str
    repository: str
    branch: str
    head_sha: str
    pr_url: str
    pr_number: int
    base_branch: Optional[str] = None
    base_sha: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe canonical representation."""
        return asdict(self)

    def __getitem__(self, key: str) -> Any:
        """Allow provider test doubles to consume evidence like a mapping."""
        return self.as_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _nested(mapping: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    for key in keys:
        child = _as_mapping(mapping.get(key))
        if child is not None:
            return child
    return {}


def _repository_from_url(url: str) -> tuple[str | None, int | None]:
    match = _PR_URL_RE.fullmatch(url)
    if not match:
        return None, None
    return f"{match.group(1)}/{match.group(2)}", int(match.group(3))


def _canonical_pr_url(repository: str, number: int) -> str:
    return f"https://github.com/{repository}/pull/{number}"


def extract_review_evidence(metadata: Any) -> tuple[ReviewEvidence | None, str | None]:
    """Extract and validate structured PR identity from completion metadata.

    For compatibility with workers from different prompt revisions, the
    canonical ``review_evidence`` object is preferred, while a flat metadata
    object and a nested ``pr`` object are accepted as input.  The returned
    dataclass is canonical and is what the database persists; free-form
    comments are never consulted here.
    """
    root = _as_mapping(metadata)
    if root is None:
        return None, "structured review evidence is missing from metadata"

    candidate = _as_mapping(root.get("review_evidence"))
    if candidate is None:
        candidate = _as_mapping(root.get("review"))
    if candidate is None:
        candidate = root
    pr = _nested(candidate, "pr", "pull_request")
    head = _nested(candidate, "head")
    if not head:
        head = _nested(pr, "head")
    base = _nested(candidate, "base")
    if not base:
        base = _nested(pr, "base")

    pr_url = _first(candidate, "pr_url", "pull_request_url", "url")
    if pr_url is None:
        pr_url = _first(pr, "url", "html_url")
    if not isinstance(pr_url, str):
        pr_url = ""
    pr_url = pr_url.strip()

    url_repository, url_number = _repository_from_url(pr_url)
    repository = _first(candidate, "repository", "repo", "repository_name")
    if repository is None:
        repository = _first(pr, "repository", "repo")
    if not isinstance(repository, str):
        repository = url_repository or ""
    repository = repository.strip().removesuffix(".git")
    if (
        url_repository is not None
        and repository.casefold() != url_repository.casefold()
    ):
        return None, "review evidence repository does not match pr_url"
    if not _REPOSITORY_RE.fullmatch(repository):
        return None, "review evidence requires repository=owner/name"
    # Preserve the URL's spelling only for validation; GitHub paths are
    # case-insensitive and the canonical URL makes retries compare equal.
    repository = "/".join(part for part in repository.split("/"))

    number_value = _first(candidate, "pr_number", "pull_request_number", "number")
    if number_value is None:
        number_value = _first(pr, "number")
    try:
        pr_number = int(number_value if number_value is not None else (url_number or 0))
    except (TypeError, ValueError, OverflowError):
        return None, "review evidence requires a positive pr_number"
    if pr_number <= 0:
        return None, "review evidence requires a positive pr_number"
    if url_number is not None and url_number != pr_number:
        return None, "review evidence pr_number does not match pr_url"
    canonical_url = _canonical_pr_url(repository, pr_number)
    if pr_url and _repository_from_url(pr_url)[0] is None:
        return None, "review evidence pr_url must be a canonical GitHub pull URL"

    provider = _first(candidate, "provider", "vcs") or "github"
    if not isinstance(provider, str) or provider.strip().casefold() != "github":
        return None, "only the GitHub review provider is supported"
    provider = "github"

    branch = _first(candidate, "branch", "head_branch", "head_ref")
    if branch is None:
        branch = _first(head, "ref", "branch")
    if not isinstance(branch, str) or not _BRANCH_RE.fullmatch(branch.strip()):
        return None, "review evidence requires a non-empty branch"
    branch = branch.strip()

    head_sha = _first(candidate, "head_sha", "commit_sha", "sha")
    if head_sha is None:
        head_sha = _first(head, "sha", "oid")
    if not isinstance(head_sha, str) or not _SHA_RE.fullmatch(head_sha.strip()):
        return None, "review evidence requires a 40-character head_sha"
    head_sha = head_sha.strip().lower()

    base_branch = _first(candidate, "base_branch", "base_ref")
    if base_branch is None:
        base_branch = _first(base, "ref", "branch")
    if base_branch is not None:
        if not isinstance(base_branch, str) or not _BRANCH_RE.fullmatch(
            base_branch.strip()
        ):
            return None, "review evidence base_branch is malformed"
        base_branch = base_branch.strip()

    base_sha = _first(candidate, "base_sha", "base_commit_sha")
    if base_sha is None:
        base_sha = _first(base, "sha", "oid")
    if base_sha is not None:
        if not isinstance(base_sha, str) or not _SHA_RE.fullmatch(base_sha.strip()):
            return None, "review evidence base_sha is malformed"
        base_sha = base_sha.strip().lower()

    return ReviewEvidence(
        provider=provider,
        repository=repository,
        branch=branch,
        head_sha=head_sha,
        pr_url=canonical_url,
        pr_number=pr_number,
        base_branch=base_branch,
        base_sha=base_sha,
    ), None


def _run_gh_api(endpoint: str) -> tuple[dict[str, Any] | None, str | None]:
    """Read one GitHub API endpoint through the user's authenticated gh CLI.

    stderr is intentionally not returned: gh may include credential or remote
    configuration details in failures, and provider diagnostics are durable in
    the Kanban event log.
    """
    gh = shutil.which("gh")
    if not gh:
        return None, "GitHub provider is unavailable: gh CLI is not installed"
    try:
        completed = subprocess.run(
            [gh, "api", endpoint, "--hostname", "github.com"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, "GitHub provider query could not be executed"
    if completed.returncode != 0:
        return None, "GitHub provider query failed"
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        return None, "GitHub provider returned invalid JSON"
    if not isinstance(payload, dict):
        return None, "GitHub provider returned an unexpected response"
    return payload, None


def fetch_live_review_state(evidence: ReviewEvidence) -> dict[str, Any]:
    """Fetch the PR and exact-head check state from GitHub.

    A compact error object is returned on every failure so callers can keep the
    task blocked with an actionable, non-secret diagnostic.
    """
    pr, error = _run_gh_api(f"repos/{evidence.repository}/pulls/{evidence.pr_number}")
    if error:
        return {"provider": "github", "state": "unknown", "diagnostic": error}
    checks, checks_error = _run_gh_api(
        f"repos/{evidence.repository}/commits/{evidence.head_sha}/check-runs"
    )
    if checks_error:
        return {
            "provider": "github",
            "pr": pr,
            "state": "unknown",
            "diagnostic": checks_error,
        }
    statuses, statuses_error = _run_gh_api(
        f"repos/{evidence.repository}/commits/{evidence.head_sha}/status"
    )
    # The check-runs endpoint is authoritative for Actions/check apps. Statuses
    # are included when available because older integrations report only commit
    # statuses. A failed statuses query is not fatal if check-runs is present.
    checks_payload = checks or {}
    statuses_payload = statuses or {}
    state: dict[str, Any] = {
        "provider": "github",
        "pr": pr,
        "check_runs": checks_payload.get("check_runs", []),
        "check_run_count": checks_payload.get("total_count"),
    }
    if statuses_error is None and statuses is not None:
        state["statuses"] = statuses_payload.get("statuses", [])
    return state


def _check_items_green(items: Any, *, check_run: bool) -> tuple[bool, str | None]:
    if not isinstance(items, list) or not items:
        return False, "provider reports no completed checks for the exact head"
    for item in items:
        if not isinstance(item, Mapping):
            return False, "provider returned an unknown check record"
        if check_run:
            status = str(item.get("status") or "").casefold()
            conclusion = str(item.get("conclusion") or "").casefold()
            if status != "completed" or conclusion != "success":
                return False, "exact-head CI is pending, red, or otherwise not green"
        elif str(item.get("state") or "").casefold() != "success":
            return (
                False,
                "exact-head commit status is pending, red, or otherwise not green",
            )
    return True, None


def verify_live_review_state(
    evidence: ReviewEvidence,
    state: Mapping[str, Any] | None = None,
    *,
    provider: Callable[[ReviewEvidence], Mapping[str, Any]] | None = None,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Verify open/non-draft/exact-head/green state and return an audit snapshot."""
    if state is None:
        provider = provider or fetch_live_review_state
        try:
            supplied = provider(evidence)
        except Exception:
            _log.debug("review provider query failed", exc_info=True)
            supplied = {"state": "unknown", "diagnostic": "provider query failed"}
        state = supplied if isinstance(supplied, Mapping) else {}

    diagnostic = state.get("diagnostic")
    if diagnostic or str(state.get("state") or "").casefold() == "unknown":
        return (
            False,
            str(diagnostic or "live provider state is unknown"),
            {
                "provider": evidence.provider,
                "result": "unknown",
            },
        )

    pr = _as_mapping(state.get("pr")) or state
    if str(pr.get("state") or "").casefold() != "open":
        return (
            False,
            "pull request is not open",
            {"provider": evidence.provider, "result": "closed"},
        )
    if pr.get("draft") is not False:
        return (
            False,
            "pull request is draft or provider did not confirm non-draft",
            {
                "provider": evidence.provider,
                "result": "draft",
            },
        )

    returned_number = pr.get("number")
    if returned_number is not None:
        try:
            if int(returned_number) != evidence.pr_number:
                return (
                    False,
                    "provider pull request number does not match immutable evidence",
                    {
                        "provider": evidence.provider,
                        "result": "diverged",
                    },
                )
        except (TypeError, ValueError, OverflowError):
            return (
                False,
                "provider pull request number is unknown",
                {
                    "provider": evidence.provider,
                    "result": "unknown",
                },
            )

    head = _as_mapping(pr.get("head")) or {}
    provider_sha = str(head.get("sha") or pr.get("head_sha") or "").lower()
    if provider_sha != evidence.head_sha:
        return (
            False,
            "pull request head diverged from immutable head_sha",
            {
                "provider": evidence.provider,
                "result": "diverged",
                "expected_head_sha": evidence.head_sha,
                "provider_head_sha": provider_sha or None,
            },
        )
    provider_branch = str(head.get("ref") or pr.get("branch") or "")
    if provider_branch != evidence.branch:
        return (
            False,
            "pull request branch diverged from immutable branch",
            {
                "provider": evidence.provider,
                "result": "diverged",
            },
        )
    base = _as_mapping(pr.get("base")) or {}
    if (
        evidence.base_branch
        and str(base.get("ref") or pr.get("base_branch") or "") != evidence.base_branch
    ):
        return (
            False,
            "pull request base branch diverged from immutable evidence",
            {
                "provider": evidence.provider,
                "result": "diverged",
            },
        )
    if (
        evidence.base_sha
        and str(base.get("sha") or pr.get("base_sha") or "").lower()
        != evidence.base_sha
    ):
        return (
            False,
            "pull request base head diverged from immutable evidence",
            {
                "provider": evidence.provider,
                "result": "diverged",
            },
        )

    check_runs = state.get("check_runs")
    statuses = state.get("statuses")
    if check_runs:
        ok, reason = _check_items_green(check_runs, check_run=True)
        if not ok:
            return (
                False,
                reason,
                {"provider": evidence.provider, "result": "checks_not_green"},
            )
    elif statuses is not None:
        ok, reason = _check_items_green(statuses, check_run=False)
        if not ok:
            return (
                False,
                reason,
                {"provider": evidence.provider, "result": "checks_not_green"},
            )
    else:
        return (
            False,
            "live provider returned no exact-head check state",
            {
                "provider": evidence.provider,
                "result": "unknown",
            },
        )

    return (
        True,
        None,
        {
            "provider": evidence.provider,
            "result": "open_non_draft_exact_head_green",
            "repository": evidence.repository,
            "pr_number": evidence.pr_number,
            "head_sha": evidence.head_sha,
            "branch": evidence.branch,
            "checked_at": int(time.time()),
        },
    )
