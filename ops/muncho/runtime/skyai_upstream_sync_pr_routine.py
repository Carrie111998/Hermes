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


FORK_REPO = "lomliev/hermes-agent"
UPSTREAM_REPO = "NousResearch/hermes-agent"
FORK_GIT_URL = "https://github.com/lomliev/hermes-agent.git"
UPSTREAM_GIT_URL = "https://github.com/NousResearch/hermes-agent.git"
SOURCE_BRANCH = "codex/skyai-v2-hermes-plugin-bootstrap"
UPSTREAM_BRANCH = "main"
CANDIDATE_BRANCH = "codex/skyai-v2-upstream-sync-auto"
REPORT_SCHEMA = "muncho-skyai-upstream-sync.v1"
EXECUTE_ENV = "SKYAI_UPSTREAM_SYNC_EXECUTE_APPROVED"
GH = Path(os.environ.get("SKYAI_UPSTREAM_SYNC_GH", "/usr/bin/gh"))
STATE_DIR = Path(
    os.environ.get(
        "SKYAI_UPSTREAM_SYNC_STATE_DIR",
        "/var/lib/muncho-dual-upstream-sync/skyai-state",
    )
)
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
_TOKEN_PATTERN = re.compile(
    r"(?i)(?:github_pat_[A-Za-z0-9_]{10,}|gh[pousr]_[A-Za-z0-9_]{10,})"
)
_SHA40 = re.compile(r"^[0-9a-f]{40}$")


class SkyAISyncBlocked(RuntimeError):
    """Stable fail-closed outcome."""

    def __init__(self, code: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.details = dict(details or {})


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


def redact(value: str) -> str:
    result = value
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        secret = os.environ.get(name)
        if secret:
            result = result.replace(secret, "[REDACTED]")
    return _TOKEN_PATTERN.sub("[REDACTED]", result)


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
        redact(completed.stdout),
        redact(completed.stderr),
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


def clone_refs(repo: Path) -> None:
    git("clone", "--no-checkout", FORK_GIT_URL, str(repo), cwd=repo.parent)
    git("remote", "add", "upstream", UPSTREAM_GIT_URL, cwd=repo)
    git("fetch", "--prune", "origin", SOURCE_BRANCH, cwd=repo)
    git("fetch", "--prune", "upstream", UPSTREAM_BRANCH, cwd=repo)
    git("fetch", "origin", CANDIDATE_BRANCH, cwd=repo, check=False)


def open_candidate_prs(repo: Path) -> list[dict[str, Any]]:
    data = gh_json(
        (
            "pr",
            "list",
            "--repo",
            FORK_REPO,
            "--base",
            SOURCE_BRANCH,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,url,headRefName,headRefOid,baseRefName",
        ),
        cwd=repo,
    )
    if not isinstance(data, list):
        raise SkyAISyncBlocked("github_pr_list_invalid")
    return [
        dict(item)
        for item in data
        if isinstance(item, dict)
        and item.get("headRefName") == CANDIDATE_BRANCH
        and item.get("baseRefName") == SOURCE_BRANCH
    ]


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


def candidate_ci_status(repo: Path, pr_url: str, expected_head: str) -> dict[str, Any]:
    number = pr_url.rstrip("/").split("/")[-1]
    if not number.isdigit():
        raise SkyAISyncBlocked("candidate_pr_url_invalid")
    view = gh_json(
        (
            "pr",
            "view",
            number,
            "--repo",
            FORK_REPO,
            "--json",
            "state,headRefOid,baseRefName,headRefName,statusCheckRollup",
        ),
        cwd=repo,
    )
    if (
        not isinstance(view, dict)
        or view.get("state") != "OPEN"
        or view.get("baseRefName") != SOURCE_BRANCH
        or view.get("headRefName") != CANDIDATE_BRANCH
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
    for item in rollup:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").upper()
        conclusion = str(item.get("conclusion") or "").upper()
        if status != "COMPLETED":
            active += 1
        elif conclusion in {"SUCCESS", "SKIPPED", "NEUTRAL"}:
            success_like += 1
        else:
            failure_like += 1
    check = {
        "name": "github_ci",
        "passed": (
            False
            if failure_like
            else True
            if not active and success_like
            else None
        ),
        "active": active,
        "failure_like": failure_like,
        "completed": success_like + failure_like,
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


def push_candidate(repo: Path, head: str) -> None:
    credential_helper = f"!{GH} auth git-credential"
    git(
        "-c",
        f"credential.https://github.com.helper={credential_helper}",
        "push",
        FORK_GIT_URL,
        f"{head}:refs/heads/{CANDIDATE_BRANCH}",
        cwd=repo,
        timeout=600,
    )


def ensure_pr(
    repo: Path,
    *,
    source_sha: str,
    upstream_sha: str,
    existing: list[dict[str, Any]],
) -> str:
    if len(existing) > 1:
        raise SkyAISyncBlocked("multiple_candidate_prs")
    if existing:
        return str(existing[0].get("url") or "")
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
                CANDIDATE_BRANCH,
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
    return result.stdout.strip().splitlines()[-1]


def execute() -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "created_at_utc": now_utc(),
        "status": "BLOCKED",
        "outcome": "not_started",
        "source_branch": SOURCE_BRANCH,
        "candidate_branch": CANDIDATE_BRANCH,
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
    safe_rmtree(repo)

    try:
        clone_refs(repo)
        source_ref = f"origin/{SOURCE_BRANCH}"
        upstream_ref = f"upstream/{UPSTREAM_BRANCH}"
        candidate_ref = f"origin/{CANDIDATE_BRANCH}"
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
        if behind == 0:
            report.update({"status": "PASS", "outcome": "up_to_date"})
            write_report(report)
            return report

        prs = open_candidate_prs(repo)
        if len(prs) > 1:
            raise SkyAISyncBlocked("multiple_candidate_prs")
        candidate_exists = ref_exists(
            repo,
            f"refs/remotes/{candidate_ref}",
        )
        start_ref = candidate_ref if candidate_exists else source_ref
        git(
            "checkout",
            "-B",
            CANDIDATE_BRANCH,
            start_ref,
            cwd=repo,
        )
        git("config", "user.name", AUTOMATION_GIT_NAME, cwd=repo)
        git("config", "user.email", AUTOMATION_GIT_EMAIL, cwd=repo)

        candidate_current = (
            candidate_exists
            and is_ancestor(repo, source_ref, "HEAD")
            and is_ancestor(repo, upstream_ref, "HEAD")
        )
        checks: list[dict[str, Any]] = []
        if not candidate_current:
            if not is_ancestor(repo, source_ref, "HEAD"):
                merge_exact(
                    repo,
                    source_ref,
                    "Merge current canonical SkyAI source into sync candidate",
                )
            if not is_ancestor(repo, upstream_ref, "HEAD"):
                merge_exact(
                    repo,
                    upstream_ref,
                    f"Merge upstream main into SkyAI ({upstream_sha[:12]})",
                )
            checks = run_static_checks(repo, upstream_ref)

        head = rev_parse(repo, "HEAD")
        push_candidate(repo, head)
        pr_url = ensure_pr(
            repo,
            source_sha=source_sha,
            upstream_sha=upstream_sha,
            existing=prs,
        )
        ci = candidate_ci_status(repo, pr_url, head)
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
    except (OSError, subprocess.SubprocessError, ValueError):
        report.update(
            {
                "status": "BLOCKED",
                "outcome": "fail_closed",
                "blocker": "unexpected_operational_error",
            }
        )
    finally:
        safe_rmtree(repo)

    write_report(report)
    return report


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
