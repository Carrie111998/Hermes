#!/usr/bin/env python3
"""Prepare one fork-only upstream-sync candidate for explicit review.

Safety contract:
- pulls NousResearch/hermes-agent main into lomliev/hermes-agent only;
- candidate ownership comes only from an exact private manifest;
- authored PR title/body text and branch-name patterns are never authority;
- merge conflicts remain blocked for LLM/Codex integration;
- this routine never merges, deploys, restarts, or mutates upstream.
"""
from __future__ import annotations

import argparse
import hashlib
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
from typing import Any, Mapping

# The reviewed systemd rail invokes this file with ``-I -S -B``. Isolated
# mode intentionally omits the script directory from ``sys.path``; re-add only
# this already digest-attested sibling directory.
_RUNTIME_DIR = Path(__file__).resolve().parent
if str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

from auto_sync_hardening import (
    CandidateManifestError,
    append_candidate_manifest,
    append_candidate_terminal_receipt,
    blocker_fingerprint,
    build_prepared_candidate_manifest,
    candidate_manifest_lock,
    classify_stale_candidate,
    clear_blocker_delivery_state,
    decide_blocker_delivery,
    publish_candidate_manifest,
    recover_candidate_manifest,
)

FORK_REPO = "lomliev/hermes-agent"
FORK_OWNER = "lomliev"
UPSTREAM_REPO = "NousResearch/hermes-agent"
FORK_BRANCH = "main"
UPSTREAM_BRANCH = "main"
FORK_GIT_URL = "https://github.com/lomliev/hermes-agent.git"
UPSTREAM_GIT_URL = "https://github.com/NousResearch/hermes-agent.git"
AUTOMATION_GIT_NAME = "Muncho Fork Sync"
AUTOMATION_GIT_EMAIL = "muncho-fork-sync@users.noreply.github.com"
BRANCH_PREFIX = "codex/upstream-sync-auto-"
HERMES_HOME = Path(
    os.environ.get("HERMES_HOME", "/opt/adventico-ai-platform/hermes-home")
)
GH = Path(
    os.environ.get(
        "FORK_UPSTREAM_AUTO_SYNC_GH",
        str(HERMES_HOME / "bin" / "gh-hermes"),
    )
)
STATE_DIR = Path(
    os.environ.get(
        "FORK_UPSTREAM_AUTO_SYNC_STATE_DIR",
        "/opt/adventico-ai-platform/canonical-brain/state/private/upstream_sync_monitor",
    )
)
WORKTREE_ROOT = Path(
    os.environ.get(
        "FORK_UPSTREAM_AUTO_SYNC_WORKTREE_ROOT",
        "/opt/adventico-ai-platform/canonical-brain/state/private/upstream_sync_worktrees",
    )
)
REPORT_DIR = Path(
    os.environ.get(
        "FORK_UPSTREAM_AUTO_SYNC_REPORT_DIR",
        "/opt/adventico-ai-platform/canonical-brain/state/reports",
    )
)
MONITOR_LATEST = STATE_DIR / "fork-upstream-drift-latest.json"
AUTO_STATE = STATE_DIR / "auto-sync-pr-state.json"
BLOCKER_DEDUPE_STATE = STATE_DIR / "auto-sync-blocker-dedupe.json"
EXECUTE_ENV = "FORK_UPSTREAM_AUTO_SYNC_EXECUTE_APPROVED"
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REGISTERED_SECRET_ENV_NAMES = ("GH_TOKEN", "GITHUB_TOKEN")


@dataclass
class CmdResult:
    cmd: list[str]
    rc: int
    stdout: str
    stderr: str


def now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def redact_command_output(value: str) -> str:
    """Redact only exact credential values from registered secret fields."""

    registered = {
        secret
        for name in _REGISTERED_SECRET_ENV_NAMES
        if (secret := os.environ.get(name))
    }
    redacted = value
    for secret in sorted(registered, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout: int | None = None,
) -> CmdResult:
    cp = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    result = CmdResult(
        cmd=cmd,
        rc=cp.returncode,
        stdout=redact_command_output(cp.stdout),
        stderr=redact_command_output(cp.stderr),
    )
    if check and cp.returncode != 0:
        raise RuntimeError(
            f"command failed rc={cp.returncode}: {' '.join(cmd)}\n{result.stderr}"
        )
    return result


def gh_json(args: list[str]) -> Any:
    if not GH.exists():
        raise RuntimeError(f"reviewed GitHub CLI missing at {GH}")
    cp = run([str(GH), *args], timeout=120)
    return json.loads(cp.stdout or "null")


def _require_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_PATTERN.fullmatch(value) is None:
        raise RuntimeError(f"invalid_{field}")
    return value


def _single_protocol_line(result: CmdResult, field: str) -> str:
    lines = result.stdout.splitlines()
    if len(lines) != 1:
        raise RuntimeError(f"invalid_{field}")
    return lines[0]


def load_monitor() -> dict[str, Any]:
    if not MONITOR_LATEST.exists():
        return {"status": "missing_monitor_state", "behind_by": None}
    value = json.loads(MONITOR_LATEST.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {"status": "invalid_monitor_state"}


def ref_sha(repo: str, branch: str) -> str:
    data = gh_json(["api", f"repos/{repo}/git/ref/heads/{branch}"])
    if not isinstance(data, dict) or not isinstance(data.get("object"), dict):
        raise RuntimeError("invalid_ref_response")
    return _require_sha(data["object"].get("sha"), "ref_sha")


def compare_refs() -> dict[str, Any]:
    comp = gh_json(
        [
            "api",
            f"repos/{UPSTREAM_REPO}/compare/{UPSTREAM_BRANCH}...lomliev:{FORK_BRANCH}",
        ]
    )
    if not isinstance(comp, dict):
        raise RuntimeError("invalid_compare_response")
    merge_base = comp.get("merge_base_commit")
    if not isinstance(merge_base, dict):
        raise RuntimeError("invalid_compare_merge_base")
    ahead_by = comp.get("ahead_by")
    behind_by = comp.get("behind_by")
    if type(ahead_by) is not int or type(behind_by) is not int:
        raise RuntimeError("invalid_compare_counts")
    status = comp.get("status")
    if not isinstance(status, str):
        raise RuntimeError("invalid_compare_status")
    compare_url = comp.get("html_url")
    if compare_url is not None and not isinstance(compare_url, str):
        raise RuntimeError("invalid_compare_url")
    return {
        "fork_main_ref": ref_sha(FORK_REPO, FORK_BRANCH),
        "upstream_main_ref": ref_sha(UPSTREAM_REPO, UPSTREAM_BRANCH),
        "merge_base": _require_sha(merge_base.get("sha"), "merge_base"),
        "ahead_by": ahead_by,
        "behind_by": behind_by,
        "compare_status": status,
        "compare_url": compare_url,
    }


def branch_name(ts: str) -> str:
    stamp = (
        ts.replace("-", "")
        .replace(":", "")
        .replace("Z", "")
        .replace("T", "-")
    )
    return f"{BRANCH_PREFIX}{stamp[:13]}"


def list_open_fork_prs() -> list[dict[str, Any]]:
    """List every open PR to fork main without interpreting authored text."""

    value = gh_json(
        [
            "pr",
            "list",
            "--repo",
            FORK_REPO,
            "--base",
            FORK_BRANCH,
            "--state",
            "open",
            "--json",
            (
                "number,url,state,headRefName,headRefOid,baseRefName,isDraft,"
                "createdAt,isCrossRepository,headRepository,headRepositoryOwner"
            ),
        ]
    )
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise RuntimeError("invalid_open_pr_list")
    return value


def pr_view(number: int) -> dict[str, Any]:
    """Fetch a later candidate only by its exact stored PR number."""

    if type(number) is not int or number <= 0:
        raise RuntimeError("invalid_candidate_pr_number")
    value = gh_json(
        [
            "pr",
            "view",
            str(number),
            "--repo",
            FORK_REPO,
            "--json",
            (
                "number,url,state,isDraft,headRefName,headRefOid,baseRefName,"
                "mergeable,mergeStateStatus,statusCheckRollup,labels,"
                "isCrossRepository,headRepository,headRepositoryOwner"
            ),
        ]
    )
    if not isinstance(value, dict):
        raise RuntimeError("invalid_candidate_pr_view")
    return value


def manifest_scope_mismatches(manifest: Mapping[str, Any]) -> list[str]:
    expected = {
        "fork_repository": FORK_REPO,
        "upstream_repository": UPSTREAM_REPO,
        "base_ref": FORK_BRANCH,
        "upstream_ref": UPSTREAM_BRANCH,
    }
    return [
        f"manifest_{field}_mismatch"
        for field, exact in expected.items()
        if manifest.get(field) != exact
    ]


def candidate_pr_mismatches(
    pr: Mapping[str, Any], manifest: Mapping[str, Any]
) -> list[str]:
    """Compare structured PR identity with the private manifest byte-for-byte."""

    if manifest.get("phase") != "published":
        return ["candidate_manifest_not_published"]
    expected = {
        "number": manifest.get("pr_number"),
        "headRefName": manifest.get("branch"),
        "headRefOid": manifest.get("head_sha"),
        "baseRefName": manifest.get("base_ref"),
    }
    mismatches = [
        f"candidate_pr_{field}_mismatch"
        for field, exact in expected.items()
        if pr.get(field) != exact
    ]
    mismatches.extend(candidate_repository_identity_mismatches(pr))
    return mismatches


def candidate_repository_identity_mismatches(
    pr: Mapping[str, Any],
) -> list[str]:
    head_repository = pr.get("headRepository")
    head_owner = pr.get("headRepositoryOwner")
    mismatches: list[str] = []
    if pr.get("isCrossRepository") is not False:
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


def compare_sha(repo: str, base: str, head: str) -> dict[str, Any] | None:
    if (
        not isinstance(base, str)
        or not isinstance(head, str)
        or _SHA_PATTERN.fullmatch(base) is None
        or _SHA_PATTERN.fullmatch(head) is None
    ):
        return None
    try:
        value = gh_json(["api", f"repos/{repo}/compare/{base}...{head}"])
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def compare_shows_head_contains_base(repo: str, base: str, head: str) -> bool:
    comp = compare_sha(repo, base, head)
    if not comp:
        return False
    behind_by = comp.get("behind_by")
    status = comp.get("status")
    return (
        type(behind_by) is int
        and behind_by == 0
        and status in {"identical", "ahead"}
    )


def candidate_commit_mismatches(
    manifest: Mapping[str, Any],
) -> list[str]:
    """Validate both exact recorded inputs as ancestors of the exact head."""

    head_sha = manifest.get("head_sha")
    base_sha = manifest.get("base_sha")
    upstream_sha = manifest.get("upstream_sha")
    if not all(
        isinstance(value, str) for value in (head_sha, base_sha, upstream_sha)
    ):
        return ["candidate_manifest_commit_identity_invalid"]
    mismatches: list[str] = []
    if not compare_shows_head_contains_base(FORK_REPO, base_sha, head_sha):
        mismatches.append("candidate_head_missing_exact_base_sha")
    if not compare_shows_head_contains_base(FORK_REPO, upstream_sha, head_sha):
        mismatches.append("candidate_head_missing_exact_upstream_sha")
    return mismatches


def stale_candidate_reason(
    manifest: Mapping[str, Any],
    pr: Mapping[str, Any],
    fresh: Mapping[str, Any],
) -> str | None:
    """Classify staleness only after exact manifest/PR identity validation."""

    if manifest_scope_mismatches(manifest) or candidate_pr_mismatches(pr, manifest):
        return None
    head_sha = manifest.get("head_sha")
    fork_sha = fresh.get("fork_main_ref")
    merge_base = fresh.get("merge_base")
    upstream_sha = manifest.get("upstream_sha")
    current_upstream_sha = fresh.get("upstream_main_ref")
    if not all(
        isinstance(value, str)
        for value in (
            head_sha,
            fork_sha,
            merge_base,
            upstream_sha,
            current_upstream_sha,
        )
    ):
        return None

    head_already_in_fork_main = compare_shows_head_contains_base(
        FORK_REPO, head_sha, fork_sha
    )
    upstream_snapshot_in_fork_merge_base = compare_shows_head_contains_base(
        UPSTREAM_REPO, upstream_sha, merge_base
    )
    current_upstream_contains_snapshot = bool(
        upstream_sha != current_upstream_sha
        and compare_shows_head_contains_base(
            UPSTREAM_REPO, upstream_sha, current_upstream_sha
        )
    )
    return classify_stale_candidate(
        head_already_in_fork_main=head_already_in_fork_main,
        upstream_snapshot_sha=upstream_sha,
        upstream_snapshot_in_fork_merge_base=(
            upstream_snapshot_in_fork_merge_base
        ),
        current_upstream_sha=current_upstream_sha,
        current_upstream_contains_snapshot=current_upstream_contains_snapshot,
    )


def apply_blocker_notification_dedupe(
    report: dict[str, Any], pr: Mapping[str, Any]
) -> bool:
    """Return True only when this factual blocker should reach the notifier."""

    blockers: list[str] = []
    for key in ("blockers", "conflicted_files"):
        value = report.get(key)
        if isinstance(value, list):
            blockers.extend(item for item in value if isinstance(item, str))
    error_type = report.get("error_type")
    if isinstance(error_type, str):
        blockers.append(f"error_type:{error_type[:80]}")
    for open_pr in report.get("open_fork_prs") or []:
        if not isinstance(open_pr, dict):
            continue
        number = open_pr.get("number")
        head = open_pr.get("headRefOid")
        if type(number) is int and isinstance(head, str):
            blockers.append(f"open_pr:{number}:{head[:40]}")

    fresh_refs = report.get("fresh_refs")
    if not isinstance(fresh_refs, dict):
        fresh_refs = {}
    candidate_head = pr.get("headRefOid")
    fork_head = fresh_refs.get("fork_main_ref")
    stable_head_sha = (
        candidate_head
        if isinstance(candidate_head, str)
        and _SHA_PATTERN.fullmatch(candidate_head)
        else (
            fork_head
            if isinstance(fork_head, str) and _SHA_PATTERN.fullmatch(fork_head)
            else None
        )
    )
    pr_number = pr.get("number")
    exact_pr_number = pr_number if type(pr_number) is int else None
    status = report.get("status")
    fingerprint = blocker_fingerprint(
        status=status if isinstance(status, str) else "blocked_unknown",
        pr_number=exact_pr_number,
        head_sha=stable_head_sha,
        blockers=blockers,
        failed_checks=[],
    )
    previous_run_at = os.environ.get("HERMES_CRON_PREVIOUS_RUN_AT")
    previous_delivery = os.environ.get("HERMES_CRON_PREVIOUS_DELIVERY")
    if previous_delivery not in {"none", "confirmed", "failed"}:
        previous_delivery = None
    if previous_run_at is not None and not (0 < len(previous_run_at) <= 80):
        previous_run_at = None
    decision = decide_blocker_delivery(
        BLOCKER_DEDUPE_STATE,
        fingerprint=fingerprint,
        observed_previous_run_at=previous_run_at,
        previous_delivery_status=previous_delivery,
    )
    report["blocker_notification"] = {
        "emit": decision["emit"],
        "selected_for_delivery": decision["emit"],
        "reason": decision["reason"],
        "suppressed_runs": decision["suppressed_runs"],
        "repeat_after_seconds": decision["repeat_after_seconds"],
        "fingerprint_prefix": fingerprint[:12],
        "delivery_confirmed_at": decision["delivery_confirmed_at"],
        "pending_delivery": decision["pending_delivery"],
        "prior_delivery_reconciled": decision["prior_delivery_reconciled"],
    }
    return bool(decision["emit"])


def worktree_for_branch(branch: str) -> Path:
    """Address worktree state by an exact branch digest, never by a prefix."""

    digest = hashlib.sha256(branch.encode("utf-8")).hexdigest()
    return WORKTREE_ROOT / f"candidate-{digest}"


def safe_rmtree(path: Path) -> None:
    root = WORKTREE_ROOT.resolve()
    target = path.resolve()
    if root not in target.parents:
        raise RuntimeError(f"refusing to remove path outside worktree root: {target}")
    if path.exists():
        shutil.rmtree(path)


def cleanup_exact_candidate_worktree(manifest: Mapping[str, Any]) -> bool:
    branch = manifest.get("branch")
    if not isinstance(branch, str):
        raise RuntimeError("candidate_manifest_branch_invalid")
    target = worktree_for_branch(branch)
    if not target.exists():
        return False
    safe_rmtree(target)
    return True


def disk_free_bytes(path: Path) -> int:
    probe = path if path.exists() else path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def changed_python_files(repo: Path, base_ref: str) -> list[str]:
    cp = run(["git", "diff", "--name-only", f"{base_ref}..HEAD"], cwd=repo)
    return [
        line
        for line in cp.stdout.splitlines()
        if line.endswith(".py") and (repo / line).exists()
    ]


def write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (
        json.dumps(dict(data), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp")
    try:
        tmp.write_text(raw, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def render_summary(report: Mapping[str, Any]) -> str:
    lines = [
        "# Fork Upstream Sync Candidate Routine",
        "",
        f"Status: `{report.get('status')}`",
        "",
        f"Time: `{report.get('created_at_utc')}`",
        "",
        "Boundaries: fork-only candidate branch/PR; explicit later merge and deploy gates; no upstream mutation.",
    ]
    fresh = report.get("fresh_refs")
    if isinstance(fresh, Mapping):
        lines += [
            "",
            "## Refs",
            f"- fork_main: `{fresh.get('fork_main_ref')}`",
            f"- upstream_main: `{fresh.get('upstream_main_ref')}`",
            f"- merge_base: `{fresh.get('merge_base')}`",
            f"- ahead_by: `{fresh.get('ahead_by')}`",
            f"- behind_by: `{fresh.get('behind_by')}`",
        ]
    if report.get("pr_url"):
        lines += ["", f"PR: {report['pr_url']}"]
    conflicts = report.get("conflicted_files")
    if isinstance(conflicts, list) and conflicts:
        lines += ["", "Conflicts:", *[f"- `{path}`" for path in conflicts]]
    message = report.get("message")
    if message is not None:
        if not isinstance(message, str):
            raise RuntimeError("report_message_invalid")
        if message:
            lines += ["", message]
    return "\n".join(lines) + "\n"


def write_report(report: Mapping[str, Any]) -> None:
    created_at = report.get("created_at_utc")
    if not isinstance(created_at, str):
        raise RuntimeError("report_created_at_missing")
    ts = created_at.replace("-", "").replace(":", "")
    write_json(STATE_DIR / "auto-sync-pr-latest.json", report)
    write_json(STATE_DIR / f"auto-sync-pr-{ts}.json", report)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "fork-upstream-auto-sync-pr-latest-public-summary.md").write_text(
        render_summary(report),
        encoding="utf-8",
    )


def finish_blocked_report(
    report: dict[str, Any], pr: Mapping[str, Any] | None = None
) -> int:
    """Persist a factual terminal blocker through the single dedupe path."""

    report["blocked"] = True
    selected = apply_blocker_notification_dedupe(report, pr or {})
    write_report(report)
    if selected:
        print(render_summary(report).rstrip())
    # Delivery deduplication controls only whether the human-facing summary is
    # emitted.  The process contract must continue to report the factual
    # blocked outcome on every run so the supervising rail cannot mistake a
    # repeated blocker for success.
    return 2


def _candidate_state_for_plan(
    open_fork_prs: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    try:
        manifest = recover_candidate_manifest(AUTO_STATE)
    except CandidateManifestError as exc:
        return None, None, [str(exc)]
    if manifest is None:
        # The private ledger is the sole candidate authority. Other open PRs
        # remain observable facts, but authored text and branch names cannot
        # make them candidates or block creation.
        return None, None, []
    scope_mismatches = manifest_scope_mismatches(manifest)
    if scope_mismatches:
        return manifest, None, scope_mismatches
    if manifest.get("phase") == "prepared":
        return manifest, None, []
    if manifest.get("phase") != "published":
        return manifest, None, ["candidate_manifest_phase_invalid"]
    try:
        candidate = pr_view(manifest["pr_number"])
    except Exception as exc:
        return manifest, None, [f"candidate_pr_lookup_failed:{type(exc).__name__}"]
    mismatches = [
        *candidate_pr_mismatches(candidate, manifest),
        *candidate_commit_mismatches(manifest),
    ]
    return manifest, candidate, mismatches


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    ts = now_utc()
    monitor = load_monitor()
    fresh = compare_refs()
    open_fork_prs = list_open_fork_prs()
    manifest, candidate, blockers = _candidate_state_for_plan(open_fork_prs)
    blocked = bool(blockers)

    if blocked:
        status = "blocked_candidate_identity_state"
    elif manifest is not None and manifest.get("phase") == "prepared":
        status = "candidate_prepared_recovery_required"
    elif candidate is not None:
        state = candidate.get("state")
        if state == "OPEN":
            status = "candidate_pr_exists_review_required_no_action"
        elif state == "CLOSED":
            status = "blocked_candidate_closed_requires_operator_reconciliation"
            blockers = ["candidate_closed_requires_operator_reconciliation"]
            blocked = True
        elif state == "MERGED":
            status = "candidate_merged_requires_reconciliation"
        else:
            status = "blocked_candidate_pr_state_unknown"
            blockers = ["candidate_pr_state_unknown"]
            blocked = True
    elif fresh["behind_by"] == 0:
        status = "no_drift_no_action"
    else:
        status = "dry_run_candidate_plan"

    return {
        "created_at_utc": ts,
        "status": status,
        "blocked": blocked,
        "blockers": blockers,
        "mode": "execute" if args.execute else "dry_run",
        "monitor_state": monitor,
        "fresh_refs": fresh,
        "open_fork_prs": open_fork_prs,
        "candidate_manifest": manifest,
        "candidate_pr": candidate,
        "proposed_branch": branch_name(ts),
        "hard_boundaries": {
            "candidate_identity": "exact_private_manifest_only",
            "display_title_or_body_authority": False,
            "branch_prefix_authority": False,
            "merge_into_fork_main": False,
            "runtime_deploy": False,
            "upstream_pr_or_push": False,
            "dashboard_update": False,
            "gateway_restart": False,
            "conflict_resolution": "llm_codex_integration_required",
        },
    }


def discover_created_candidate_pr(branch: str, head_sha: str) -> dict[str, Any]:
    """Resolve the just-created PR through exact structured identity fields."""

    value = gh_json(
        [
            "pr",
            "list",
            "--repo",
            FORK_REPO,
            "--base",
            FORK_BRANCH,
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            (
                "number,url,state,isDraft,headRefName,headRefOid,baseRefName,"
                "createdAt,isCrossRepository,headRepository,headRepositoryOwner"
            ),
        ]
    )
    if not isinstance(value, list):
        raise RuntimeError("invalid_created_candidate_list")
    exact = [
        row
        for row in value
        if isinstance(row, dict)
        and row.get("headRefName") == branch
        and row.get("headRefOid") == head_sha
        and row.get("baseRefName") == FORK_BRANCH
        and row.get("state") == "OPEN"
        and not candidate_repository_identity_mismatches(row)
        and type(row.get("number")) is int
        and row["number"] > 0
    ]
    if len(exact) != 1:
        raise RuntimeError("created_candidate_exact_identity_unavailable")
    return exact[0]


def list_exact_branch_candidate_prs(
    branch: str,
    head_sha: str,
) -> list[dict[str, Any]]:
    """Read all terminal/open PR facts for one manifest-owned exact branch."""

    value = gh_json(
        [
            "pr",
            "list",
            "--repo",
            FORK_REPO,
            "--base",
            FORK_BRANCH,
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "100",
            "--json",
            (
                "number,url,state,isDraft,headRefName,headRefOid,baseRefName,"
                "createdAt,isCrossRepository,headRepository,headRepositoryOwner"
            ),
        ]
    )
    if not isinstance(value, list) or any(
        not isinstance(row, dict) for row in value
    ):
        raise RuntimeError("invalid_exact_candidate_pr_list")
    mismatched = [
        row
        for row in value
        if row.get("headRefName") != branch
        or row.get("baseRefName") != FORK_BRANCH
        or row.get("headRefOid") != head_sha
        or candidate_repository_identity_mismatches(row)
        or type(row.get("number")) is not int
        or row["number"] <= 0
        or row.get("state") not in {"OPEN", "CLOSED", "MERGED"}
    ]
    if mismatched:
        raise RuntimeError("exact_candidate_pr_identity_mismatch")
    if len(value) > 1:
        raise RuntimeError("multiple_exact_candidate_prs")
    return value


def create_candidate_pr(
    *,
    worktree: Path,
    branch: str,
    head_sha: str,
    base_sha: str,
    upstream_sha: str,
    created_at_utc: str,
    behind_by: int,
    ahead_by: int,
) -> tuple[dict[str, Any], CmdResult]:
    title = (
        "chore: prepare fork upstream-sync candidate "
        f"{created_at_utc[:10]}"
    )
    body = (
        "Fork-only upstream-sync candidate for explicit review.\n\n"
        f"- Base fork SHA: `{base_sha}`\n"
        f"- Upstream SHA: `{upstream_sha}`\n"
        f"- behind_by before preparation: `{behind_by}`\n"
        f"- ahead_by before preparation: `{ahead_by}`\n\n"
        "The title and this body are display text only. Candidate identity "
        "is held in private canonical state. This routine does not merge, "
        "deploy, restart, or mutate upstream.\n"
    )
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False
    ) as stream:
        stream.write(body)
        body_file = stream.name
    try:
        result = run(
            [
                str(GH),
                "pr",
                "create",
                "--repo",
                FORK_REPO,
                "--base",
                FORK_BRANCH,
                "--head",
                branch,
                "--draft",
                "--title",
                title,
                "--body-file",
                body_file,
            ],
            cwd=worktree,
            timeout=120,
        )
    finally:
        Path(body_file).unlink(missing_ok=True)
    return discover_created_candidate_pr(branch, head_sha), result


def _validate_prepared_worktree(
    manifest: Mapping[str, Any],
) -> Path:
    worktree = worktree_for_branch(manifest["branch"])
    if not worktree.is_dir():
        raise RuntimeError("prepared_candidate_worktree_missing")
    head = _require_sha(
        _single_protocol_line(
            run(["git", "rev-parse", "HEAD"], cwd=worktree),
            "prepared_candidate_head",
        ),
        "prepared_candidate_head",
    )
    if head != manifest["head_sha"]:
        raise RuntimeError("prepared_candidate_worktree_head_mismatch")
    status = run(["git", "status", "--porcelain"], cwd=worktree)
    if status.stdout:
        raise RuntimeError("prepared_candidate_worktree_not_clean")
    for label, ancestor in (
        ("base", manifest["base_sha"]),
        ("upstream", manifest["upstream_sha"]),
    ):
        if (
            run(
                ["git", "merge-base", "--is-ancestor", ancestor, head],
                cwd=worktree,
                check=False,
            ).rc
            != 0
        ):
            raise RuntimeError(
                f"prepared_candidate_head_missing_exact_{label}_sha"
            )
    return worktree


def _recover_prepared_candidate(
    args: argparse.Namespace,
    report: dict[str, Any],
    manifest: Mapping[str, Any],
) -> int:
    """Resume only the exact prepared transaction; never infer ownership."""

    exact_prs = list_exact_branch_candidate_prs(
        manifest["branch"],
        manifest["head_sha"],
    )
    create_result: CmdResult | None = None
    if exact_prs:
        candidate = exact_prs[0]
    else:
        worktree = _validate_prepared_worktree(manifest)
        # A normal non-force push is idempotent for an absent/same remote ref
        # and fails closed if an external actor moved the exact branch.
        run(
            [
                "git",
                "-c",
                f"credential.https://github.com.helper=!{GH} auth git-credential",
                "push",
                FORK_GIT_URL,
                f"{manifest['head_sha']}:refs/heads/{manifest['branch']}",
            ],
            cwd=worktree,
            timeout=300,
        )
        candidate, create_result = create_candidate_pr(
            worktree=worktree,
            branch=manifest["branch"],
            head_sha=manifest["head_sha"],
            base_sha=manifest["base_sha"],
            upstream_sha=manifest["upstream_sha"],
            created_at_utc=manifest["created_at_utc"],
            behind_by=report["fresh_refs"]["behind_by"],
            ahead_by=report["fresh_refs"]["ahead_by"],
        )

    published = publish_candidate_manifest(
        manifest,
        pr_number=candidate["number"],
    )
    if candidate_pr_mismatches(candidate, published):
        raise RuntimeError("recovered_candidate_manifest_mismatch")
    append_candidate_manifest(AUTO_STATE, published)
    report["prepared_recovery"] = {
        "candidate_id": manifest["candidate_id"],
        "candidate_head": manifest["head_sha"],
        "pr_number": candidate["number"],
        "pr_create_replayed": create_result is not None,
    }
    return _execute_locked(args)


def _refresh_after_candidate_reconciliation(
    args: argparse.Namespace,
    prior_report: Mapping[str, Any],
) -> dict[str, Any]:
    refreshed = build_plan(args)
    refreshed["candidate_reconciliation"] = prior_report
    return refreshed


def _execute_locked(args: argparse.Namespace) -> int:
    report = build_plan(args)
    if report["blocked"]:
        return finish_blocked_report(report, report.get("candidate_pr"))

    manifest = report.get("candidate_manifest")
    candidate = report.get("candidate_pr")
    if (
        isinstance(manifest, dict)
        and manifest.get("phase") == "prepared"
        and candidate is None
    ):
        try:
            return _recover_prepared_candidate(args, report, manifest)
        except Exception as exc:
            report.update(
                {
                    "status": "blocked_prepared_candidate_recovery",
                    "blocked": True,
                    "blockers": ["prepared_candidate_recovery_failed"],
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "candidate_ref_frozen": True,
                }
            )
            return finish_blocked_report(report)
    if isinstance(manifest, dict) and isinstance(candidate, dict):
        state = candidate.get("state")
        if state == "CLOSED":
            report.update(
                {
                    "status": (
                        "blocked_candidate_closed_requires_operator_reconciliation"
                    ),
                    "blocked": True,
                    "blockers": [
                        "candidate_closed_requires_operator_reconciliation"
                    ],
                    "pr_number": manifest["pr_number"],
                    "candidate_ref_frozen": True,
                    "message": (
                        "The exact candidate was closed without merge. Its "
                        "private manifest and worktree evidence are preserved; "
                        "this routine will not reopen, replace, or recreate it "
                        "without explicit operator reconciliation."
                    ),
                }
            )
            return finish_blocked_report(report, candidate)
        if state == "MERGED":
            fork_main_after = report["fresh_refs"]["fork_main_ref"]
            if not compare_shows_head_contains_base(
                FORK_REPO,
                manifest["head_sha"],
                fork_main_after,
            ):
                report.update(
                    {
                        "status": (
                            "blocked_candidate_merged_without_base_proof"
                        ),
                        "blocked": True,
                        "blockers": [
                            "candidate_merged_head_missing_from_fork_main"
                        ],
                        "candidate_ref_frozen": True,
                    }
                )
                return finish_blocked_report(report, candidate)
            terminal = append_candidate_terminal_receipt(
                AUTO_STATE,
                manifest,
                observed_base_sha=fork_main_after,
                created_at_utc=report["created_at_utc"],
            )
            worktree_deleted = cleanup_exact_candidate_worktree(manifest)
            report = _refresh_after_candidate_reconciliation(
                args,
                {
                    "terminal_state": state,
                    "pr_number": manifest["pr_number"],
                    "terminal_receipt_sha256": terminal["receipt_sha256"],
                    "worktree_deleted": worktree_deleted,
                },
            )
            if report["blocked"]:
                return finish_blocked_report(report, report.get("candidate_pr"))
        elif state == "OPEN":
            tail_drift = (
                manifest.get("upstream_sha")
                != report["fresh_refs"].get("upstream_main_ref")
            )
            report["status"] = (
                "candidate_pr_exists_review_required_tail_pending_no_action"
                if tail_drift
                else "candidate_pr_exists_review_required_no_action"
            )
            report["pr_url"] = candidate.get("url")
            report["pr_number"] = manifest["pr_number"]
            report["candidate_ref_frozen"] = True
            report["later_upstream_is_tail_drift"] = tail_drift
            report["tail_drift_rebinds_candidate"] = False
            report["message"] = (
                "The exact manifest-owned fork candidate remains open and "
                "immutable. Later upstream commits are tail drift for a "
                "future reviewed candidate; this routine will not close, "
                "replace, merge, or deploy the open candidate."
            )
            clear_blocker_delivery_state(BLOCKER_DEDUPE_STATE)
            write_report(report)
            return 0

    fresh = report["fresh_refs"]
    if fresh["behind_by"] == 0:
        report["status"] = "no_drift_no_action"
        clear_blocker_delivery_state(BLOCKER_DEDUPE_STATE)
        write_report(report)
        return 0

    branch = report["proposed_branch"]
    worktree = worktree_for_branch(branch)
    if worktree.exists():
        report.update(
            {
                "status": "blocked_candidate_worktree_already_exists",
                "blockers": ["candidate_worktree_already_exists"],
                "branch": branch,
                "worktree": str(worktree),
            }
        )
        return finish_blocked_report(report)
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)

    free_bytes = disk_free_bytes(WORKTREE_ROOT)
    report["disk_free_bytes"] = free_bytes
    if free_bytes < 5 * 1024 * 1024 * 1024:
        report.update(
            {
                "status": "blocked_disk_space_low",
                "blockers": ["disk_space_below_5_gib"],
                "branch": branch,
                "worktree": str(worktree),
                "message": "Less than 5 GiB free before cloning.",
            }
        )
        return finish_blocked_report(report)

    try:
        run(["git", "clone", FORK_GIT_URL, str(worktree)], timeout=300)
        run(["git", "remote", "add", "upstream", UPSTREAM_GIT_URL], cwd=worktree)
        run(
            ["git", "fetch", "origin", FORK_BRANCH],
            cwd=worktree,
            timeout=300,
        )
        run(
            ["git", "fetch", "upstream", UPSTREAM_BRANCH],
            cwd=worktree,
            timeout=300,
        )
        fetched_fork = _require_sha(
            _single_protocol_line(
                run(["git", "rev-parse", f"origin/{FORK_BRANCH}"], cwd=worktree),
                "fetched_fork_ref",
            ),
            "fetched_fork_ref",
        )
        fetched_upstream = _require_sha(
            _single_protocol_line(
                run(
                    ["git", "rev-parse", f"upstream/{UPSTREAM_BRANCH}"],
                    cwd=worktree,
                ),
                "fetched_upstream_ref",
            ),
            "fetched_upstream_ref",
        )
        if (
            fetched_fork != fresh["fork_main_ref"]
            or fetched_upstream != fresh["upstream_main_ref"]
        ):
            report.update(
                {
                    "status": "blocked_refs_changed_during_candidate_preparation",
                    "blockers": ["fetched_refs_do_not_match_plan"],
                    "branch": branch,
                    "worktree": str(worktree),
                    "fetched_fork_ref": fetched_fork,
                    "fetched_upstream_ref": fetched_upstream,
                }
            )
            return finish_blocked_report(report)

        run(
            ["git", "checkout", "-B", branch, fresh["fork_main_ref"]],
            cwd=worktree,
        )
        merge = run(
            [
                "git",
                "merge",
                "--no-commit",
                "--no-ff",
                fresh["upstream_main_ref"],
            ],
            cwd=worktree,
            check=False,
            timeout=300,
        )
        conflicted = run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=worktree,
            check=False,
        ).stdout.splitlines()
        if merge.rc != 0 or conflicted:
            report.update(
                {
                    "status": "blocked_merge_conflicts",
                    "blockers": ["upstream_candidate_requires_llm_integration"],
                    "branch": branch,
                    "worktree": str(worktree),
                    "conflicted_files": conflicted,
                    "merge_rc": merge.rc,
                    "merge_stdout_tail": merge.stdout[-4000:],
                    "merge_stderr_tail": merge.stderr[-4000:],
                    "message": (
                        "Conflicts remain for explicit LLM/Codex integration; "
                        "no source-text auto-resolver was run."
                    ),
                }
            )
            run(["git", "merge", "--abort"], cwd=worktree, check=False)
            return finish_blocked_report(report)

        py_files = changed_python_files(worktree, fresh["fork_main_ref"])
        if py_files:
            run(
                [sys.executable, "-m", "py_compile", *py_files],
                cwd=worktree,
                timeout=300,
            )

        title = (
            f"chore: prepare fork upstream-sync candidate "
            f"{report['created_at_utc'][:10]}"
        )
        body = (
            "Fork-only upstream-sync candidate for explicit review.\n\n"
            f"- Base fork SHA: `{fresh['fork_main_ref']}`\n"
            f"- Upstream SHA: `{fresh['upstream_main_ref']}`\n"
            f"- behind_by before preparation: `{fresh['behind_by']}`\n"
            f"- ahead_by before preparation: `{fresh['ahead_by']}`\n\n"
            "The title and this body are display text only. Candidate identity "
            "is held in private canonical state. This routine does not merge, "
            "deploy, restart, or mutate upstream.\n"
        )
        run(
            [
                "git",
                "-c",
                f"user.name={AUTOMATION_GIT_NAME}",
                "-c",
                f"user.email={AUTOMATION_GIT_EMAIL}",
                "commit",
                "-m",
                title,
                "-m",
                body,
            ],
            cwd=worktree,
            timeout=300,
        )
        head = _require_sha(
            _single_protocol_line(
                run(["git", "rev-parse", "HEAD"], cwd=worktree),
                "candidate_head",
            ),
            "candidate_head",
        )
        for label, ancestor in (
            ("base", fresh["fork_main_ref"]),
            ("upstream", fresh["upstream_main_ref"]),
        ):
            ancestry = run(
                ["git", "merge-base", "--is-ancestor", ancestor, head],
                cwd=worktree,
                check=False,
            )
            if ancestry.rc != 0:
                raise RuntimeError(
                    f"candidate_head_missing_exact_{label}_sha"
                )
        candidate_id = hashlib.sha256(os.urandom(32)).hexdigest()
        prepared_manifest = build_prepared_candidate_manifest(
            candidate_id=candidate_id,
            fork_repository=FORK_REPO,
            upstream_repository=UPSTREAM_REPO,
            base_ref=FORK_BRANCH,
            upstream_ref=UPSTREAM_BRANCH,
            branch=branch,
            head_sha=head,
            base_sha=fresh["fork_main_ref"],
            upstream_sha=fresh["upstream_main_ref"],
            created_at_utc=report["created_at_utc"],
        )
        append_candidate_manifest(AUTO_STATE, prepared_manifest)
        run(
            [
                "git",
                "-c",
                f"credential.https://github.com.helper=!{GH} auth git-credential",
                "push",
                FORK_GIT_URL,
                f"HEAD:refs/heads/{branch}",
            ],
            cwd=worktree,
            timeout=300,
        )

        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False
        ) as stream:
            stream.write(body)
            body_file = stream.name
        try:
            create_result = run(
                [
                    str(GH),
                    "pr",
                    "create",
                    "--repo",
                    FORK_REPO,
                    "--base",
                    FORK_BRANCH,
                    "--head",
                    branch,
                    "--draft",
                    "--title",
                    title,
                    "--body-file",
                    body_file,
                ],
                cwd=worktree,
                timeout=120,
            )
        finally:
            Path(body_file).unlink(missing_ok=True)

        candidate = discover_created_candidate_pr(branch, head)
        manifest = publish_candidate_manifest(
            prepared_manifest,
            pr_number=candidate["number"],
        )
        mismatches = [
            *manifest_scope_mismatches(manifest),
            *candidate_pr_mismatches(candidate, manifest),
        ]
        if mismatches:
            raise RuntimeError(
                "created_candidate_manifest_mismatch:" + ",".join(mismatches)
            )
        append_candidate_manifest(AUTO_STATE, manifest)

        report.update(
            {
                "status": "sync_candidate_pr_opened_review_required",
                "blocked": False,
                "branch": branch,
                "head": head,
                "pr_url": candidate.get("url"),
                "pr_number": candidate["number"],
                "candidate_manifest_sha256": manifest["manifest_sha256"],
                "worktree": str(worktree),
                "py_compile_files": len(py_files),
                "pr_create_stdout_tail": create_result.stdout[-1000:],
                "message": (
                    "A fork-only candidate PR was opened and bound to an exact "
                    "private manifest. Merge and deploy remain separate gates."
                ),
            }
        )
        clear_blocker_delivery_state(BLOCKER_DEDUPE_STATE)
        write_report(report)
        print(render_summary(report).rstrip())
        return 0
    except Exception as exc:
        report.update(
            {
                "status": "blocked_execute_exception",
                "blockers": ["candidate_execution_exception"],
                "error_type": type(exc).__name__,
                "error": str(exc),
                "branch": branch,
                "worktree": str(worktree),
            }
        )
        return finish_blocked_report(report)


def execute(args: argparse.Namespace) -> int:
    with candidate_manifest_lock(AUTO_STATE):
        if os.environ.get(EXECUTE_ENV) != "1":
            report = build_plan(args)
            report["status"] = "blocked_execute_env_missing"
            report["blockers"] = [f"missing_{EXECUTE_ENV}"]
            report["message"] = f"Missing {EXECUTE_ENV}=1"
            return finish_blocked_report(report, report.get("candidate_pr"))
        return _execute_locked(args)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fork-only upstream-sync candidate routine"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Create a fork-only candidate PR if drift exists",
    )
    parser.add_argument(
        "--output",
        default=str(STATE_DIR / "auto-sync-pr-dry-run-latest.json"),
    )
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)

    if args.execute:
        return execute(args)

    with candidate_manifest_lock(AUTO_STATE):
        plan = build_plan(args)
    write_json(Path(args.output), plan)
    if plan["status"] == "no_drift_no_action":
        return 0
    print(json.dumps(plan, indent=2, ensure_ascii=False))
    return 2 if plan["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
