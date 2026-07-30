#!/usr/bin/env python3
"""Build one verified, fork-only SkyAI upstream-sync candidate PR.

This is an operational wrapper for the canonical SkyAI source branch.  It
never imports SkyAI business code into Muncho, never interprets customer
meaning, and never merges or deploys.  Unknown conflicts and failed fixed
verification stop the run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

_RUNTIME_DIR = Path(__file__).resolve().parent
if str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

from auto_sync_hardening import (
    CandidateManifestError,
    append_candidate_manifest,
    append_candidate_terminal_receipt,
    build_prepared_candidate_manifest,
    candidate_manifest_lock,
    publish_candidate_manifest,
    recover_candidate_manifest,
)


FORK_REPO = "lomliev/hermes-agent"
FORK_OWNER = "lomliev"
UPSTREAM_REPO = "NousResearch/hermes-agent"
FORK_GIT_URL = "https://github.com/lomliev/hermes-agent.git"
UPSTREAM_GIT_URL = "https://github.com/NousResearch/hermes-agent.git"
SOURCE_BRANCH = "codex/skyai-v2-hermes-plugin-bootstrap"
UPSTREAM_BRANCH = "main"
CANDIDATE_BRANCH_PREFIX = "codex/skyai-v2-upstream-sync-auto-"
REPORT_SCHEMA = "muncho-skyai-upstream-sync.v1"
EXECUTE_ENV = "SKYAI_UPSTREAM_SYNC_EXECUTE_APPROVED"
GH = Path(os.environ.get("SKYAI_UPSTREAM_SYNC_GH", "/usr/bin/gh"))
STATE_DIR = Path(
    os.environ.get(
        "SKYAI_UPSTREAM_SYNC_STATE_DIR",
        "/var/lib/muncho-dual-upstream-sync/skyai-state",
    )
)
AUTO_STATE = STATE_DIR / "skyai-sync-candidate-state.json"
WORKTREE_ROOT = Path(
    os.environ.get(
        "SKYAI_UPSTREAM_SYNC_WORKTREE_ROOT",
        "/var/lib/muncho-dual-upstream-sync/skyai-worktrees",
    )
)
TEST_FILES = (
    "tests/plugins/test_skyai_customer_plugin.py",
    "tests/plugins/test_skyai_customer_schema.py",
    "tests/plugins/test_skyai_customer_dev_gateway.py",
    "tests/plugins/test_skyai_customer_voice_contract.py",
    "tests/plugins/test_skyai_customer_architecture.py",
    "tests/scripts/test_skyai_v2_bootstrap_dev_profile.py",
    "tests/scripts/test_skyai_v2_compare_matrix.py",
    "tests/scripts/test_skyai_v2_upstream_sync_check.py",
    "tests/scripts/test_skyai_v2_upstream_sync_daily_report.py",
    "tests/scripts/test_skyai_v2_upstream_sync_routine.py",
)
ALLOWED_PREFIXES = (
    "plugins/skyai_customer/",
    "skills/productivity/skyai-customer-hermes-v2/",
    "docs/skyai-v1-legacy-archive.md",
    "docs/skyai-v2-",
    "docs/skyai-voice-contract-v0.1.md",
    "docs/voice/skyai-voice-joint-contract-v0.1.md",
    "tests/plugins/test_skyai_customer_",
    "tests/scripts/test_skyai_v2_bootstrap_dev_profile.py",
    "tests/scripts/test_skyai_v2_compare_matrix.py",
    "tests/scripts/test_skyai_v2_upstream_sync_",
    "scripts/skyai_v2_bootstrap_dev_profile.py",
    "scripts/skyai_v2_compare_matrix.py",
    "scripts/skyai_v2_upstream_sync_",
    "scripts/skyai_voice_",
)
AUTOMATION_GIT_NAME = "Muncho SkyAI Sync"
AUTOMATION_GIT_EMAIL = "muncho-skyai-sync@users.noreply.github.com"
MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024
_REGISTERED_SECRET_ENV_NAMES = ("GH_TOKEN", "GITHUB_TOKEN")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_PR_URL = re.compile(
    r"^https://github\.com/lomliev/hermes-agent/pull/([1-9][0-9]*)$"
)
_ACTIVE_CHECK_RUN_STATUSES = frozenset(
    {"QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED"}
)
_SUCCESSFUL_CHECK_RUN_CONCLUSIONS = frozenset(
    {"SUCCESS", "SKIPPED", "NEUTRAL"}
)
_FAILED_CHECK_RUN_CONCLUSIONS = frozenset(
    {
        "ACTION_REQUIRED",
        "CANCELLED",
        "FAILURE",
        "STALE",
        "STARTUP_FAILURE",
        "TIMED_OUT",
    }
)
_ACTIVE_STATUS_CONTEXT_STATES = frozenset({"EXPECTED", "PENDING"})
_SUCCESSFUL_STATUS_CONTEXT_STATES = frozenset({"SUCCESS"})
_FAILED_STATUS_CONTEXT_STATES = frozenset({"ERROR", "FAILURE"})


class SkyAISyncBlocked(RuntimeError):
    """Stable fail-closed outcome."""

    def __init__(self, code: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.details = dict(details or {})


def candidate_branch(candidate_id: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", candidate_id) is None:
        raise SkyAISyncBlocked("candidate_id_invalid")
    return f"{CANDIDATE_BRANCH_PREFIX}{candidate_id[:24]}"


@dataclass(frozen=True)
class CmdResult:
    returncode: int
    stdout: str
    stderr: str


def now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def redact(
    value: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Redact only exact credential values from registered secret fields."""

    secret_environment = environment if environment is not None else os.environ
    registered = {
        secret
        for name in _REGISTERED_SECRET_ENV_NAMES
        if (secret := secret_environment.get(name))
    }
    result = value
    for secret in sorted(registered, key=len, reverse=True):
        result = result.replace(secret, "[REDACTED]")
    return result


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout: int = 1200,
    environment: Mapping[str, str] | None = None,
) -> CmdResult:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if environment:
        env.update(environment)
    completed = subprocess.run(
        [str(item) for item in args],
        cwd=str(cwd) if cwd else None,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout,
    )
    result = CmdResult(
        completed.returncode,
        redact(completed.stdout, env),
        redact(completed.stderr, env),
    )
    if check and result.returncode != 0:
        raise SkyAISyncBlocked(
            "command_failed",
            details={
                "command": Path(str(args[0])).name,
                "returncode": result.returncode,
            },
        )
    return result


def git(
    *args: str,
    cwd: Path,
    check: bool = True,
    timeout: int = 1200,
) -> CmdResult:
    return run(("git", *args), cwd=cwd, check=check, timeout=timeout)


def gh_json(args: Sequence[str], *, cwd: Path) -> Any:
    if GH != Path("/usr/bin/gh") or not GH.is_file():
        raise SkyAISyncBlocked("github_cli_not_exact")
    result = run((str(GH), *args), cwd=cwd, timeout=180)
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise SkyAISyncBlocked("github_cli_invalid_json") from exc


def safe_rmtree(path: Path) -> None:
    root = WORKTREE_ROOT.resolve()
    target = path.resolve()
    if root not in target.parents:
        raise SkyAISyncBlocked("unsafe_worktree_path")
    if path.exists():
        shutil.rmtree(path)


def write_report(report: Mapping[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    payload = json.dumps(dict(report), indent=2, ensure_ascii=False) + "\n"
    stamp = str(report["created_at_utc"]).replace("-", "").replace(":", "")
    for target in (
        STATE_DIR / f"skyai-sync-{stamp}.json",
        STATE_DIR / "skyai-sync-latest.json",
    ):
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)


def ref_exists(repo: Path, ref: str) -> bool:
    return (
        git("show-ref", "--verify", "--quiet", ref, cwd=repo, check=False).returncode
        == 0
    )


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return (
        git(
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            cwd=repo,
            check=False,
        ).returncode
        == 0
    )


def rev_parse(repo: Path, ref: str) -> str:
    value = git("rev-parse", ref, cwd=repo).stdout.strip()
    if _SHA40.fullmatch(value) is None:
        raise SkyAISyncBlocked("invalid_git_sha")
    return value


def ahead_behind(repo: Path, source: str, upstream: str) -> tuple[int, int]:
    value = git(
        "rev-list",
        "--left-right",
        "--count",
        f"{source}...{upstream}",
        cwd=repo,
    ).stdout.split()
    if len(value) != 2:
        raise SkyAISyncBlocked("invalid_ahead_behind")
    return int(value[0]), int(value[1])


def clone_refs(
    repo: Path,
    candidate_branch_name: str | None = None,
) -> None:
    git("clone", "--no-checkout", FORK_GIT_URL, str(repo), cwd=repo.parent)
    git("remote", "add", "upstream", UPSTREAM_GIT_URL, cwd=repo)
    git("fetch", "--prune", "origin", SOURCE_BRANCH, cwd=repo)
    git("fetch", "--prune", "upstream", UPSTREAM_BRANCH, cwd=repo)
    if candidate_branch_name is not None:
        git("fetch", "origin", candidate_branch_name, cwd=repo, check=False)


def exact_branch_candidate_prs(
    repo: Path,
    *,
    expected_head: str,
    expected_branch: str,
) -> list[dict[str, Any]]:
    data = gh_json(
        (
            "pr",
            "list",
            "--repo",
            FORK_REPO,
            "--base",
            SOURCE_BRANCH,
            "--head",
            expected_branch,
            "--state",
            "all",
            "--limit",
            "100",
            "--json",
            (
                "number,url,state,isDraft,headRefName,headRefOid,baseRefName,"
                "isCrossRepository,headRepository,headRepositoryOwner"
            ),
        ),
        cwd=repo,
    )
    if not isinstance(data, list) or any(
        not isinstance(item, dict) for item in data
    ):
        raise SkyAISyncBlocked("candidate_pr_list_invalid")
    mismatched = [
        item
        for item in data
        if item.get("headRefName") != expected_branch
        or item.get("baseRefName") != SOURCE_BRANCH
        or item.get("headRefOid") != expected_head
        or candidate_repository_identity_mismatches(item)
        or type(item.get("number")) is not int
        or item["number"] <= 0
        or item.get("state") not in {"OPEN", "CLOSED", "MERGED"}
    ]
    if mismatched:
        raise SkyAISyncBlocked("candidate_pr_identity_mismatch")
    if len(data) > 1:
        raise SkyAISyncBlocked("multiple_candidate_prs")
    return [dict(item) for item in data]


def reserved_candidate_prs(
    repo: Path,
    candidate_branch_name: str,
) -> list[dict[str, Any]]:
    """Return exact reserved-resource collisions without claiming ownership."""

    data = gh_json(
        (
            "pr",
            "list",
            "--repo",
            FORK_REPO,
            "--base",
            SOURCE_BRANCH,
            "--head",
            candidate_branch_name,
            "--state",
            "all",
            "--limit",
            "100",
            "--json",
            (
                "number,url,state,headRefName,headRefOid,baseRefName,"
                "isCrossRepository,headRepository,headRepositoryOwner"
            ),
        ),
        cwd=repo,
    )
    if not isinstance(data, list) or any(
        not isinstance(item, dict)
        or item.get("headRefName") != candidate_branch_name
        or item.get("baseRefName") != SOURCE_BRANCH
        or candidate_repository_identity_mismatches(item)
        for item in data
    ):
        raise SkyAISyncBlocked("reserved_candidate_pr_facts_invalid")
    return [dict(item) for item in data]


def candidate_pr_view(repo: Path, number: int) -> dict[str, Any]:
    if type(number) is not int or number <= 0:
        raise SkyAISyncBlocked("candidate_pr_number_invalid")
    data = gh_json(
        (
            "pr",
            "view",
            str(number),
            "--repo",
            FORK_REPO,
            "--json",
            (
                "number,url,state,isDraft,headRefName,headRefOid,baseRefName,"
                "isCrossRepository,headRepository,headRepositoryOwner"
            ),
        ),
        cwd=repo,
    )
    if not isinstance(data, dict):
        raise SkyAISyncBlocked("candidate_pr_view_invalid")
    return dict(data)


def manifest_scope_mismatches(manifest: Mapping[str, Any]) -> list[str]:
    expected = {
        "fork_repository": FORK_REPO,
        "upstream_repository": UPSTREAM_REPO,
        "base_ref": SOURCE_BRANCH,
        "upstream_ref": UPSTREAM_BRANCH,
    }
    mismatches = [
        f"manifest_{field}_mismatch"
        for field, exact in expected.items()
        if manifest.get(field) != exact
    ]
    try:
        expected_branch = candidate_branch(str(manifest.get("candidate_id")))
    except SkyAISyncBlocked:
        mismatches.append("manifest_candidate_id_mismatch")
    else:
        if manifest.get("branch") != expected_branch:
            mismatches.append("manifest_branch_mismatch")
    return mismatches


def candidate_manifest_pr_mismatches(
    manifest: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> list[str]:
    expected = {
        "number": manifest.get("pr_number"),
        "headRefName": manifest.get("branch"),
        "headRefOid": manifest.get("head_sha"),
        "baseRefName": manifest.get("base_ref"),
    }
    mismatches = [
        f"candidate_pr_{field}_mismatch"
        for field, exact in expected.items()
        if candidate.get(field) != exact
    ]
    mismatches.extend(candidate_repository_identity_mismatches(candidate))
    return mismatches


def candidate_repository_identity_mismatches(
    candidate: Mapping[str, Any],
) -> list[str]:
    head_repository = candidate.get("headRepository")
    head_owner = candidate.get("headRepositoryOwner")
    mismatches: list[str] = []
    if candidate.get("isCrossRepository") is not False:
        mismatches.append("candidate_pr_cross_repository_mismatch")
    if (
        not isinstance(head_repository, Mapping)
        or head_repository.get("nameWithOwner") != FORK_REPO
    ):
        mismatches.append("candidate_pr_head_repository_mismatch")
    if (
        not isinstance(head_owner, Mapping)
        or head_owner.get("login") != FORK_OWNER
    ):
        mismatches.append("candidate_pr_head_owner_mismatch")
    return mismatches


def inspect_open_candidate(
    repo: Path,
    *,
    candidate_ref: str,
    source_ref: str,
    upstream_ref: str,
    candidate_exists: bool,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Read one active candidate without changing its exact reviewed head."""

    if not candidates:
        return None
    if len(candidates) != 1:
        raise SkyAISyncBlocked("multiple_candidate_prs")
    if not candidate_exists:
        raise SkyAISyncBlocked("candidate_pr_ref_missing")
    head = rev_parse(repo, candidate_ref)
    if candidates[0].get("headRefOid") != head:
        raise SkyAISyncBlocked("candidate_pr_head_mismatch")
    source_tail_ahead, source_tail_behind = ahead_behind(
        repo,
        candidate_ref,
        source_ref,
    )
    upstream_tail_ahead, upstream_tail_behind = ahead_behind(
        repo,
        candidate_ref,
        upstream_ref,
    )
    return {
        "head": head,
        "source_tail_ahead": source_tail_ahead,
        "source_tail_behind": source_tail_behind,
        "upstream_tail_ahead": upstream_tail_ahead,
        "upstream_tail_behind": upstream_tail_behind,
    }


def conflicted_files(repo: Path) -> list[str]:
    return sorted(
        item
        for item in git(
            "diff",
            "--name-only",
            "--diff-filter=U",
            cwd=repo,
            check=False,
        ).stdout.splitlines()
        if item
    )


def merge_exact(repo: Path, ref: str, message: str) -> None:
    result = git(
        "merge",
        "--no-commit",
        "--no-ff",
        ref,
        cwd=repo,
        check=False,
        timeout=600,
    )
    if result.returncode != 0:
        conflicts = conflicted_files(repo)
        git("merge", "--abort", cwd=repo, check=False)
        raise SkyAISyncBlocked(
            "merge_conflicts",
            details={"conflicted_files": conflicts},
        )
    staged = git("diff", "--cached", "--name-only", cwd=repo).stdout.strip()
    if staged:
        git("commit", "-m", message, cwd=repo)


def conflict_markers(repo: Path, upstream_ref: str) -> list[str]:
    changed = git(
        "diff",
        "--name-only",
        f"{upstream_ref}...HEAD",
        cwd=repo,
    ).stdout.splitlines()
    found: list[str] = []
    for relative in changed:
        path = repo / relative
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        if any(line.startswith(("<<<<<<< ", ">>>>>>> ", "||||||| ")) for line in lines):
            found.append(relative)
    return sorted(found)


def boundary_files(repo: Path, upstream_ref: str) -> tuple[list[str], list[str]]:
    changed = sorted(
        {
            item
            for item in git(
                "diff",
                "--name-only",
                f"{upstream_ref}...HEAD",
                cwd=repo,
            ).stdout.splitlines()
            if item
        }
    )
    disallowed = [
        path
        for path in changed
        if not any(
            path == prefix or path.startswith(prefix)
            for prefix in ALLOWED_PREFIXES
        )
    ]
    return changed, disallowed


def run_static_checks(repo: Path, upstream_ref: str) -> list[dict[str, Any]]:
    """Run non-executing checks only; candidate code executes in GitHub CI."""

    changed, disallowed = boundary_files(repo, upstream_ref)
    checks: list[dict[str, Any]] = [
        {
            "name": "boundary",
            "passed": not disallowed,
            "changed_files": len(changed),
            "disallowed_files": disallowed,
        }
    ]
    if disallowed:
        raise SkyAISyncBlocked(
            "verification_failed",
            details={
                "failed_check": "boundary",
                "disallowed_files": disallowed,
            },
        )
    diff_check = git(
        "diff",
        "--check",
        f"{upstream_ref}...HEAD",
        cwd=repo,
        check=False,
    )
    checks.append(
        {
            "name": "diff_check",
            "passed": diff_check.returncode == 0,
            "returncode": diff_check.returncode,
        }
    )
    if diff_check.returncode != 0:
        raise SkyAISyncBlocked(
            "verification_failed",
            details={
                "failed_check": "diff_check",
                "returncode": diff_check.returncode,
            },
        )
    markers = conflict_markers(repo, upstream_ref)
    checks.append(
        {
            "name": "conflict_markers",
            "passed": not markers,
            "returncode": 0 if not markers else 1,
        }
    )
    if markers:
        raise SkyAISyncBlocked(
            "verification_failed",
            details={
                "failed_check": "conflict_markers",
                "conflict_marker_files": markers,
            },
        )
    return checks


def candidate_ci_status(
    repo: Path,
    pr_url: str,
    expected_head: str,
    expected_branch: str,
) -> dict[str, Any]:
    match = _PR_URL.fullmatch(pr_url) if type(pr_url) is str else None
    if match is None:
        raise SkyAISyncBlocked("candidate_pr_url_invalid")
    number = match.group(1)
    view = gh_json(
        (
            "pr",
            "view",
            number,
            "--repo",
            FORK_REPO,
            "--json",
            (
                "state,headRefOid,baseRefName,headRefName,statusCheckRollup,"
                "isCrossRepository,headRepository,headRepositoryOwner"
            ),
        ),
        cwd=repo,
    )
    if (
        not isinstance(view, dict)
        or view.get("state") != "OPEN"
        or view.get("baseRefName") != SOURCE_BRANCH
        or view.get("headRefName") != expected_branch
        or candidate_repository_identity_mismatches(view)
    ):
        raise SkyAISyncBlocked("candidate_pr_identity_invalid")
    if view.get("headRefOid") != expected_head:
        return {
            "status": "PARTIAL",
            "outcome": "candidate_ci_pending",
            "check": {
                "name": "github_ci",
                "passed": None,
                "active": 0,
                "failure_like": 0,
                "completed": 0,
                "reason": "head_not_visible_yet",
            },
        }
    rollup = view.get("statusCheckRollup")
    if not isinstance(rollup, list) or not rollup:
        return {
            "status": "PARTIAL",
            "outcome": "candidate_ci_pending",
            "check": {
                "name": "github_ci",
                "passed": None,
                "active": 0,
                "failure_like": 0,
                "completed": 0,
                "reason": "checks_not_started",
            },
        }
    active = 0
    failure_like = 0
    success_like = 0
    invalid = 0
    for item in rollup:
        if not isinstance(item, dict):
            invalid += 1
            continue
        has_status = "status" in item
        has_state = "state" in item
        if has_status == has_state:
            invalid += 1
            continue
        if has_status:
            status = item.get("status")
            conclusion = item.get("conclusion")
            if (
                type(status) is str
                and status in _ACTIVE_CHECK_RUN_STATUSES
                and (conclusion is None or conclusion == "")
            ):
                active += 1
            elif (
                status == "COMPLETED"
                and type(conclusion) is str
                and conclusion in _SUCCESSFUL_CHECK_RUN_CONCLUSIONS
            ):
                success_like += 1
            elif (
                status == "COMPLETED"
                and type(conclusion) is str
                and conclusion in _FAILED_CHECK_RUN_CONCLUSIONS
            ):
                failure_like += 1
            else:
                invalid += 1
            continue
        state = item.get("state")
        if type(state) is str and state in _ACTIVE_STATUS_CONTEXT_STATES:
            active += 1
        elif (
            type(state) is str
            and state in _SUCCESSFUL_STATUS_CONTEXT_STATES
        ):
            success_like += 1
        elif type(state) is str and state in _FAILED_STATUS_CONTEXT_STATES:
            failure_like += 1
        else:
            invalid += 1
    check = {
        "name": "github_ci",
        "passed": (
            False
            if failure_like or invalid
            else True
            if not active and success_like
            else None
        ),
        "active": active,
        "failure_like": failure_like,
        "completed": success_like + failure_like,
        "invalid": invalid,
    }
    if invalid:
        return {
            "status": "BLOCKED",
            "outcome": "candidate_ci_protocol_invalid",
            "blocker": "github_ci_protocol_invalid",
            "check": check,
        }
    if failure_like:
        return {
            "status": "BLOCKED",
            "outcome": "candidate_ci_failed",
            "blocker": "github_ci_failed",
            "check": check,
        }
    if active or not success_like:
        return {
            "status": "PARTIAL",
            "outcome": "candidate_ci_pending",
            "check": check,
        }
    return {
        "status": "PASS",
        "outcome": "candidate_pr_ci_green",
        "check": check,
    }


def push_candidate(
    repo: Path,
    head: str,
    candidate_branch_name: str,
) -> None:
    credential_helper = f"!{GH} auth git-credential"
    git(
        "-c",
        f"credential.https://github.com.helper={credential_helper}",
        "push",
        FORK_GIT_URL,
        f"{head}:refs/heads/{candidate_branch_name}",
        cwd=repo,
        timeout=600,
    )


def ensure_pr(
    repo: Path,
    *,
    source_sha: str,
    upstream_sha: str,
    candidate_branch_name: str,
    existing: list[dict[str, Any]],
) -> str:
    if len(existing) > 1:
        raise SkyAISyncBlocked("multiple_candidate_prs")
    if existing:
        url = existing[0].get("url")
        if type(url) is not str or _PR_URL.fullmatch(url) is None:
            raise SkyAISyncBlocked("candidate_pr_url_invalid")
        return url
    body = (
        "Automated SkyAI fork-only upstream-sync candidate.\n\n"
        f"- canonical SkyAI source: `{source_sha}`\n"
        f"- upstream main: `{upstream_sha}`\n"
        "- verification: fixed SkyAI, voice, architecture, schema, and sync tests\n\n"
        "Safety: no auto-merge, deploy, runtime, frontend, PBX, or public-upstream "
        "mutation is authorized by this PR."
    )
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
    ) as stream:
        stream.write(body)
        body_path = Path(stream.name)
    try:
        result = run(
            (
                str(GH),
                "pr",
                "create",
                "--repo",
                FORK_REPO,
                "--base",
                SOURCE_BRANCH,
                "--head",
                candidate_branch_name,
                "--draft",
                "--title",
                f"chore(skyai): sync upstream {upstream_sha[:12]}",
                "--body-file",
                str(body_path),
            ),
            cwd=repo,
            timeout=180,
        )
    finally:
        body_path.unlink(missing_ok=True)
    stdout_match = re.fullmatch(
        (
            r"(https://github\.com/lomliev/hermes-agent/"
            r"pull/[1-9][0-9]*)\n"
        ),
        result.stdout,
    )
    if stdout_match is None:
        raise SkyAISyncBlocked("candidate_pr_create_output_invalid")
    return stdout_match.group(1)


def _validate_prepared_repo(
    repo: Path,
    manifest: Mapping[str, Any],
) -> None:
    if not repo.is_dir():
        raise SkyAISyncBlocked("prepared_candidate_worktree_missing")
    if git("remote", "get-url", "origin", cwd=repo).stdout.strip() != FORK_GIT_URL:
        raise SkyAISyncBlocked("prepared_candidate_origin_mismatch")
    if (
        git("remote", "get-url", "upstream", cwd=repo).stdout.strip()
        != UPSTREAM_GIT_URL
    ):
        raise SkyAISyncBlocked("prepared_candidate_upstream_mismatch")
    if rev_parse(repo, "HEAD") != manifest["head_sha"]:
        raise SkyAISyncBlocked("prepared_candidate_head_mismatch")
    if git("status", "--porcelain", cwd=repo).stdout:
        raise SkyAISyncBlocked("prepared_candidate_worktree_not_clean")
    for field in ("base_sha", "upstream_sha"):
        if not is_ancestor(repo, manifest[field], manifest["head_sha"]):
            raise SkyAISyncBlocked(
                f"prepared_candidate_head_missing_{field}"
            )


def _refresh_prepared_repo(repo: Path, candidate_branch_name: str) -> None:
    git("fetch", "--prune", "origin", SOURCE_BRANCH, cwd=repo)
    git("fetch", "--prune", "upstream", UPSTREAM_BRANCH, cwd=repo)
    git("fetch", "origin", candidate_branch_name, cwd=repo, check=False)


def _recover_prepared_candidate(
    repo: Path,
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    _validate_prepared_repo(repo, manifest)
    exact = exact_branch_candidate_prs(
        repo,
        expected_head=manifest["head_sha"],
        expected_branch=manifest["branch"],
    )
    if exact:
        candidate = exact[0]
    else:
        push_candidate(repo, manifest["head_sha"], manifest["branch"])
        ensure_pr(
            repo,
            source_sha=manifest["base_sha"],
            upstream_sha=manifest["upstream_sha"],
            candidate_branch_name=manifest["branch"],
            existing=[],
        )
        exact = exact_branch_candidate_prs(
            repo,
            expected_head=manifest["head_sha"],
            expected_branch=manifest["branch"],
        )
        if len(exact) != 1:
            raise SkyAISyncBlocked("candidate_pr_publication_unconfirmed")
        candidate = exact[0]
    published = publish_candidate_manifest(
        manifest,
        pr_number=candidate["number"],
    )
    if candidate_manifest_pr_mismatches(published, candidate):
        raise SkyAISyncBlocked("candidate_pr_manifest_mismatch")
    append_candidate_manifest(AUTO_STATE, published)
    return published, candidate


def _execute_locked() -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "created_at_utc": now_utc(),
        "status": "BLOCKED",
        "outcome": "not_started",
        "source_branch": SOURCE_BRANCH,
        "candidate_branch": None,
        "fork_repository": FORK_REPO,
        "upstream_repository_read_only": UPSTREAM_REPO,
        "auto_merge": False,
        "deploy": False,
        "force_push": False,
        "runtime_mutation": False,
        "provider_or_model_invoked": False,
    }
    if os.environ.get(EXECUTE_ENV) != "1":
        report.update(
            {"outcome": "fail_closed", "blocker": "execute_approval_missing"}
        )
        write_report(report)
        return report

    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(WORKTREE_ROOT, 0o700)
    if shutil.disk_usage(WORKTREE_ROOT).free < MIN_FREE_BYTES:
        report.update({"outcome": "fail_closed", "blocker": "disk_space_low"})
        write_report(report)
        return report
    repo = WORKTREE_ROOT / "skyai-upstream-sync"
    # Preserve an existing interrupted worktree until exact ledger recovery
    # proves that it is not the sole local copy of a prepared head.
    keep_prepared_worktree = repo.is_dir()

    try:
        manifest = recover_candidate_manifest(AUTO_STATE)
        if manifest is not None:
            scope_mismatches = manifest_scope_mismatches(manifest)
            if scope_mismatches:
                raise SkyAISyncBlocked(
                    "candidate_manifest_scope_mismatch",
                    details={"manifest_mismatches": scope_mismatches},
                )
            report["candidate_branch"] = manifest["branch"]
        if manifest is not None and manifest["phase"] == "prepared":
            keep_prepared_worktree = True
            manifest, _candidate = _recover_prepared_candidate(repo, manifest)
            keep_prepared_worktree = False

        if manifest is None or not repo.is_dir():
            safe_rmtree(repo)
            clone_refs(
                repo,
                manifest["branch"] if manifest is not None else None,
            )
            keep_prepared_worktree = False
        else:
            _refresh_prepared_repo(repo, manifest["branch"])
            if manifest["phase"] == "published":
                keep_prepared_worktree = False

        source_ref = f"origin/{SOURCE_BRANCH}"
        upstream_ref = f"upstream/{UPSTREAM_BRANCH}"
        source_sha = rev_parse(repo, source_ref)
        upstream_sha = rev_parse(repo, upstream_ref)
        ahead, behind = ahead_behind(repo, source_ref, upstream_ref)
        report.update(
            {
                "source_sha": source_sha,
                "upstream_sha": upstream_sha,
                "head_ahead": ahead,
                "head_behind": behind,
            }
        )
        if manifest is not None:
            candidate_ref = f"origin/{manifest['branch']}"
            candidate_exists = ref_exists(
                repo,
                f"refs/remotes/{candidate_ref}",
            )
            if manifest["phase"] != "published":
                raise SkyAISyncBlocked("candidate_manifest_phase_invalid")
            candidate = candidate_pr_view(repo, manifest["pr_number"])
            mismatches = candidate_manifest_pr_mismatches(
                manifest,
                candidate,
            )
            if mismatches:
                raise SkyAISyncBlocked(
                    "candidate_pr_manifest_mismatch",
                    details={"candidate_mismatches": mismatches},
                )

            state = candidate.get("state")
            if state == "CLOSED":
                raise SkyAISyncBlocked(
                    "candidate_closed_requires_operator_reconciliation",
                    details={
                        "candidate_sha": manifest["head_sha"],
                        "pr_number": manifest["pr_number"],
                    },
                )
            if state == "MERGED":
                if not is_ancestor(repo, manifest["head_sha"], source_ref):
                    raise SkyAISyncBlocked(
                        "candidate_merged_without_source_proof"
                    )
                terminal = append_candidate_terminal_receipt(
                    AUTO_STATE,
                    manifest,
                    observed_base_sha=source_sha,
                    created_at_utc=report["created_at_utc"],
                )
                report.update(
                    {
                        "status": "PASS",
                        "outcome": "candidate_merged_reconciled",
                        "candidate_sha": manifest["head_sha"],
                        "pr_number": manifest["pr_number"],
                        "terminal_receipt_sha256": terminal[
                            "receipt_sha256"
                        ],
                    }
                )
                write_report(report)
                return report
            if state != "OPEN":
                raise SkyAISyncBlocked("candidate_pr_state_invalid")
            if (
                not candidate_exists
                or rev_parse(repo, candidate_ref) != manifest["head_sha"]
            ):
                raise SkyAISyncBlocked("candidate_remote_head_mismatch")

            source_tail_ahead, source_tail_behind = ahead_behind(
                repo,
                candidate_ref,
                source_ref,
            )
            upstream_tail_ahead, upstream_tail_behind = ahead_behind(
                repo,
                candidate_ref,
                upstream_ref,
            )
            pr_url = candidate.get("url")
            if type(pr_url) is not str:
                raise SkyAISyncBlocked("candidate_pr_url_invalid")
            ci = candidate_ci_status(
                repo,
                pr_url,
                manifest["head_sha"],
                manifest["branch"],
            )
            status = ci["status"]
            outcome = ci["outcome"]
            if (
                status == "PASS"
                and (source_tail_behind > 0 or upstream_tail_behind > 0)
            ):
                status = "PARTIAL"
                outcome = "candidate_pr_ci_green_tail_pending"
            report.update(
                {
                    "status": status,
                    "outcome": outcome,
                    "candidate_sha": manifest["head_sha"],
                    "candidate_source_tail_ahead": source_tail_ahead,
                    "candidate_source_tail_behind": source_tail_behind,
                    "candidate_upstream_tail_ahead": upstream_tail_ahead,
                    "candidate_upstream_tail_behind": upstream_tail_behind,
                    "checks": [ci["check"]],
                    "pr_url": pr_url,
                }
            )
            if ci.get("blocker"):
                report["blocker"] = ci["blocker"]
            write_report(report)
            return report

        if behind == 0:
            report.update({"status": "PASS", "outcome": "up_to_date"})
            write_report(report)
            return report

        candidate_id = os.urandom(32).hex()
        candidate_branch_name = candidate_branch(candidate_id)
        report["candidate_branch"] = candidate_branch_name
        git(
            "fetch",
            "origin",
            candidate_branch_name,
            cwd=repo,
            check=False,
        )
        candidate_ref = f"origin/{candidate_branch_name}"
        candidate_exists = ref_exists(
            repo,
            f"refs/remotes/{candidate_ref}",
        )
        if candidate_exists or reserved_candidate_prs(
            repo,
            candidate_branch_name,
        ):
            raise SkyAISyncBlocked(
                "unowned_reserved_candidate_resource_exists"
            )

        git(
            "checkout",
            "-B",
            candidate_branch_name,
            source_ref,
            cwd=repo,
        )
        git("config", "user.name", AUTOMATION_GIT_NAME, cwd=repo)
        git("config", "user.email", AUTOMATION_GIT_EMAIL, cwd=repo)

        checks: list[dict[str, Any]] = []
        if not is_ancestor(repo, upstream_ref, "HEAD"):
            merge_exact(
                repo,
                upstream_ref,
                f"Merge upstream main into SkyAI ({upstream_sha[:12]})",
            )
        checks = run_static_checks(repo, upstream_ref)

        head = rev_parse(repo, "HEAD")
        prepared = build_prepared_candidate_manifest(
            candidate_id=candidate_id,
            fork_repository=FORK_REPO,
            upstream_repository=UPSTREAM_REPO,
            base_ref=SOURCE_BRANCH,
            upstream_ref=UPSTREAM_BRANCH,
            branch=candidate_branch_name,
            head_sha=head,
            base_sha=source_sha,
            upstream_sha=upstream_sha,
            created_at_utc=report["created_at_utc"],
        )
        keep_prepared_worktree = True
        append_candidate_manifest(AUTO_STATE, prepared)
        push_candidate(repo, head, candidate_branch_name)
        pr_url = ensure_pr(
            repo,
            source_sha=source_sha,
            upstream_sha=upstream_sha,
            candidate_branch_name=candidate_branch_name,
            existing=[],
        )
        exact_prs = exact_branch_candidate_prs(
            repo,
            expected_head=head,
            expected_branch=candidate_branch_name,
        )
        if len(exact_prs) != 1:
            raise SkyAISyncBlocked("candidate_pr_publication_unconfirmed")
        published = publish_candidate_manifest(
            prepared,
            pr_number=exact_prs[0]["number"],
        )
        if candidate_manifest_pr_mismatches(published, exact_prs[0]):
            raise SkyAISyncBlocked("candidate_pr_manifest_mismatch")
        append_candidate_manifest(AUTO_STATE, published)
        keep_prepared_worktree = False
        ci = candidate_ci_status(
            repo,
            pr_url,
            head,
            candidate_branch_name,
        )
        report.update(
            {
                "status": ci["status"],
                "outcome": ci["outcome"],
                "candidate_sha": head,
                "checks": [*checks, ci["check"]],
                "pr_url": pr_url,
            }
        )
        if ci.get("blocker"):
            report["blocker"] = ci["blocker"]
    except SkyAISyncBlocked as exc:
        report.update(
            {
                "status": "BLOCKED",
                "outcome": "fail_closed",
                "blocker": exc.code,
                **exc.details,
            }
        )
    except (
        CandidateManifestError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ):
        report.update(
            {
                "status": "BLOCKED",
                "outcome": "fail_closed",
                "blocker": "unexpected_operational_error",
            }
        )
    finally:
        if not keep_prepared_worktree:
            safe_rmtree(repo)

    write_report(report)
    return report


def execute() -> dict[str, Any]:
    with candidate_manifest_lock(AUTO_STATE):
        return _execute_locked()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(
            json.dumps(
                {
                    "schema": REPORT_SCHEMA,
                    "status": "BLOCKED",
                    "blocker": "execute_flag_required",
                }
            )
        )
        return 2
    report = execute()
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
