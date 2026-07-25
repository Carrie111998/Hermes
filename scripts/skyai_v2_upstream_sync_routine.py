#!/usr/bin/env python3
"""Build and publish a fail-closed SkyAI upstream-sync candidate.

The routine is deliberately mechanical. It fetches Git refs, creates an
isolated worktree, merges the canonical SkyAI source branch and upstream main,
runs the fixed SkyAI verification suite, and optionally pushes one rolling
candidate branch/PR in the SkyAI fork.

It never merges the candidate into the canonical source branch, deploys a
runtime, changes PBX/frontend state, force-pushes, or interprets customer
meaning.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Iterator, Sequence


SOURCE_BRANCH = "codex/skyai-v2-hermes-plugin-bootstrap"
UPSTREAM_REMOTE = "origin"
UPSTREAM_BRANCH = "main"
FORK_REMOTE = "fork"
CANDIDATE_BRANCH = "codex/skyai-v2-upstream-sync-auto"
REPORT_SCHEMA = "skyai-v2-upstream-sync-report.v1"

SKYAI_TEST_FILES = (
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


class SyncBlocked(RuntimeError):
    """A fail-closed condition that requires review."""

    def __init__(self, reason: str, *, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


@dataclass(frozen=True)
class CmdResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class SyncConfig:
    repo: Path
    state_dir: Path
    worktree_root: Path
    source_branch: str = SOURCE_BRANCH
    upstream_remote: str = UPSTREAM_REMOTE
    upstream_branch: str = UPSTREAM_BRANCH
    fork_remote: str = FORK_REMOTE
    candidate_branch: str = CANDIDATE_BRANCH
    execute: bool = False
    push_pr: bool = False
    fetch: bool = True


Runner = Callable[..., CmdResult]
CheckRunner = Callable[[Path, str], list[dict[str, Any]]]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def run_command(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    timeout: int = 900,
) -> CmdResult:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    completed = subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout,
    )
    result = CmdResult(
        args=tuple(str(arg) for arg in args),
        returncode=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )
    if check and result.returncode != 0:
        raise SyncBlocked(
            "command_failed",
            details={
                "command": Path(result.args[0]).name,
                "returncode": result.returncode,
            },
        )
    return result


def git(
    config: SyncConfig,
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
    timeout: int = 900,
    runner: Runner = run_command,
) -> CmdResult:
    return runner(
        ("git", *args),
        cwd=cwd or config.repo,
        check=check,
        timeout=timeout,
    )


def _safe_ref(ref: str) -> str:
    if not ref or ref.startswith("-") or not re.fullmatch(r"[A-Za-z0-9._/-]+", ref):
        raise SyncBlocked("invalid_git_ref")
    return ref


def _safe_automation_branch(branch: str) -> str:
    branch = _safe_ref(branch)
    if not branch.startswith("codex/skyai-v2-upstream-sync-auto"):
        raise SyncBlocked("candidate_branch_outside_automation_namespace")
    return branch


def parse_github_repo(remote_url: str) -> str:
    patterns = (
        r"^https://github\.com/([^/]+/[^/]+?)(?:\.git)?$",
        r"^git@github\.com:([^/]+/[^/]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/([^/]+/[^/]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, remote_url.strip())
        if match:
            return match.group(1)
    raise SyncBlocked("fork_remote_is_not_a_supported_github_repo")


def _git_output(
    config: SyncConfig,
    *args: str,
    cwd: Path | None = None,
    runner: Runner = run_command,
) -> str:
    return git(config, *args, cwd=cwd, runner=runner).stdout


def ref_exists(
    config: SyncConfig,
    ref: str,
    *,
    runner: Runner = run_command,
) -> bool:
    result = git(
        config,
        "show-ref",
        "--verify",
        "--quiet",
        ref,
        check=False,
        runner=runner,
    )
    return result.returncode == 0


def is_ancestor(
    config: SyncConfig,
    ancestor: str,
    descendant: str,
    *,
    cwd: Path | None = None,
    runner: Runner = run_command,
) -> bool:
    result = git(
        config,
        "merge-base",
        "--is-ancestor",
        ancestor,
        descendant,
        cwd=cwd,
        check=False,
        runner=runner,
    )
    return result.returncode == 0


def ensure_clean_source(config: SyncConfig, *, runner: Runner = run_command) -> None:
    status = _git_output(config, "status", "--porcelain", runner=runner)
    if status:
        raise SyncBlocked("canonical_source_worktree_is_dirty")


def fetch_refs(config: SyncConfig, *, runner: Runner = run_command) -> None:
    git(
        config,
        "fetch",
        "--prune",
        config.upstream_remote,
        config.upstream_branch,
        runner=runner,
    )
    git(
        config,
        "fetch",
        "--prune",
        config.fork_remote,
        config.source_branch,
        runner=runner,
    )
    candidate = _safe_automation_branch(config.candidate_branch)
    git(
        config,
        "fetch",
        config.fork_remote,
        candidate,
        check=False,
        runner=runner,
    )


def upstream_counts(
    config: SyncConfig,
    source_ref: str,
    upstream_ref: str,
    *,
    runner: Runner = run_command,
) -> tuple[int, int]:
    output = _git_output(
        config,
        "rev-list",
        "--left-right",
        "--count",
        f"{source_ref}...{upstream_ref}",
        runner=runner,
    )
    ahead, behind = output.split()
    return int(ahead), int(behind)


def _merge_ref(
    config: SyncConfig,
    worktree: Path,
    ref: str,
    message: str,
    *,
    runner: Runner = run_command,
) -> None:
    merge = git(
        config,
        "merge",
        "--no-commit",
        "--no-ff",
        ref,
        cwd=worktree,
        check=False,
        runner=runner,
    )
    if merge.returncode == 0:
        staged = _git_output(
            config,
            "diff",
            "--cached",
            "--name-only",
            cwd=worktree,
            runner=runner,
        )
        if staged:
            git(config, "commit", "-m", message, cwd=worktree, runner=runner)
        return

    conflicted = _git_output(
        config,
        "diff",
        "--name-only",
        "--diff-filter=U",
        cwd=worktree,
        runner=runner,
    ).splitlines()
    git(config, "merge", "--abort", cwd=worktree, check=False, runner=runner)
    raise SyncBlocked(
        "merge_conflicts",
        details={"conflicted_files": sorted(path for path in conflicted if path)},
    )


def build_candidate(
    config: SyncConfig,
    *,
    source_ref: str,
    upstream_ref: str,
    start_ref: str,
    runner: Runner = run_command,
    check_runner: CheckRunner | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    branch = _safe_automation_branch(config.candidate_branch)
    worktree = config.worktree_root / branch.replace("/", "-")
    if worktree.exists():
        raise SyncBlocked(
            "automation_worktree_path_already_exists",
            details={"worktree": str(worktree)},
        )

    git(config, "worktree", "prune", runner=runner)
    if ref_exists(config, f"refs/heads/{branch}", runner=runner):
        git(config, "branch", "-D", branch, check=False, runner=runner)
    git(
        config,
        "worktree",
        "add",
        "-b",
        branch,
        str(worktree),
        start_ref,
        runner=runner,
    )

    try:
        if not is_ancestor(
            config, source_ref, "HEAD", cwd=worktree, runner=runner
        ):
            _merge_ref(
                config,
                worktree,
                source_ref,
                "Merge current canonical SkyAI source into automated sync candidate",
                runner=runner,
            )
        if not is_ancestor(
            config, upstream_ref, "HEAD", cwd=worktree, runner=runner
        ):
            upstream_sha = _git_output(config, "rev-parse", upstream_ref, runner=runner)
            _merge_ref(
                config,
                worktree,
                upstream_ref,
                f"Merge upstream main into SkyAI v2 ({upstream_sha[:12]})",
                runner=runner,
            )
        checks = (check_runner or run_skyai_checks)(worktree, upstream_ref)
        head = _git_output(config, "rev-parse", "HEAD", cwd=worktree, runner=runner)
        return head, checks
    finally:
        git(
            config,
            "worktree",
            "remove",
            "--force",
            str(worktree),
            check=False,
            runner=runner,
        )


def verification_check_name(command: Sequence[str]) -> str:
    names = {Path(str(part)).name for part in command}
    if "skyai_v2_upstream_sync_check.py" in names:
        return "boundary"
    if "run_tests.sh" in names:
        return "tests"
    if tuple(command[:3]) == ("git", "diff", "--check"):
        return "diff_check"
    return "verification"


def run_skyai_checks(worktree: Path, upstream_ref: str) -> list[dict[str, Any]]:
    commands: tuple[tuple[str, ...], ...] = (
        (
            sys.executable,
            "scripts/skyai_v2_upstream_sync_check.py",
            upstream_ref,
        ),
        (
            "scripts/run_tests.sh",
            *SKYAI_TEST_FILES,
            "-q",
        ),
        ("git", "diff", "--check", f"{upstream_ref}...HEAD"),
    )
    checks: list[dict[str, Any]] = []
    for command in commands:
        result = run_command(command, cwd=worktree, check=False, timeout=1200)
        check_name = verification_check_name(command)
        checks.append(
            {
                "name": check_name,
                "returncode": result.returncode,
                "passed": result.returncode == 0,
            }
        )
        if result.returncode != 0:
            raise SyncBlocked(
                "verification_failed",
                details={"failed_check": check_name, "returncode": result.returncode},
            )
    changed = run_command(
        ("git", "diff", "--name-only", f"{upstream_ref}...HEAD"),
        cwd=worktree,
    ).stdout.splitlines()
    marker_files: list[str] = []
    for relative in changed:
        path = worktree / relative
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        if any(line.startswith(("<<<<<<<", ">>>>>>>")) for line in lines):
            marker_files.append(relative)
    checks.append(
        {
            "name": "conflict_markers",
            "returncode": 1 if marker_files else 0,
            "passed": not marker_files,
        }
    )
    if marker_files:
        raise SyncBlocked(
            "verification_failed",
            details={
                "failed_check": "conflict_markers",
                "conflict_marker_files": sorted(marker_files),
            },
        )
    return checks


def open_candidate_prs(
    config: SyncConfig,
    fork_repo: str,
    *,
    runner: Runner = run_command,
) -> list[dict[str, Any]]:
    gh = shutil.which("gh")
    if not gh:
        raise SyncBlocked("gh_cli_not_available")
    result = runner(
        (
            gh,
            "pr",
            "list",
            "--repo",
            fork_repo,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,url,headRefName,baseRefName",
        ),
        cwd=config.repo,
        check=True,
        timeout=120,
    )
    data = json.loads(result.stdout or "[]")
    return [
        item
        for item in data
        if item.get("headRefName") == config.candidate_branch
        and item.get("baseRefName") == config.source_branch
    ]


def push_candidate_and_ensure_pr(
    config: SyncConfig,
    candidate_head: str,
    upstream_sha: str,
    *,
    runner: Runner = run_command,
) -> str:
    fork_url = _git_output(
        config,
        "remote",
        "get-url",
        config.fork_remote,
        runner=runner,
    )
    fork_repo = parse_github_repo(fork_url)
    branch = _safe_automation_branch(config.candidate_branch)
    git(
        config,
        "push",
        config.fork_remote,
        f"{candidate_head}:refs/heads/{branch}",
        runner=runner,
    )
    existing = open_candidate_prs(config, fork_repo, runner=runner)
    if existing:
        return str(existing[0]["url"])

    gh = shutil.which("gh")
    if not gh:
        raise SyncBlocked("gh_cli_not_available")
    body = (
        "Automated SkyAI fork-only upstream-sync candidate.\n\n"
        f"- upstream: `{upstream_sha}`\n"
        "- boundary: SkyAI plugin/skills/docs/scripts/tests only\n"
        "- verification: fixed SkyAI, voice, architecture, schema, and sync tests\n\n"
        "This PR is not authorized to auto-merge or deploy. PROD, DEV runtime, "
        "frontend, PBX, and public upstream remain unchanged."
    )
    result = runner(
        (
            gh,
            "pr",
            "create",
            "--repo",
            fork_repo,
            "--base",
            config.source_branch,
            "--head",
            branch,
            "--title",
            f"chore(skyai): sync upstream {upstream_sha[:12]}",
            "--body",
            body,
        ),
        cwd=config.repo,
        check=True,
        timeout=120,
    )
    return result.stdout.strip().splitlines()[-1]


def report_fingerprint(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": report.get("status"),
        "outcome": report.get("outcome"),
        "source_sha": report.get("source_sha"),
        "upstream_sha": report.get("upstream_sha"),
        "candidate_sha": report.get("candidate_sha"),
        "blocker": report.get("blocker"),
        "failed_check": report.get("failed_check"),
    }


def write_report(config: SyncConfig, report: dict[str, Any]) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config.state_dir, 0o700)
    latest = config.state_dir / "latest.json"
    previous: dict[str, Any] = {}
    if latest.exists():
        try:
            previous = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    report["duplicate_of_previous"] = bool(
        previous and report_fingerprint(previous) == report_fingerprint(report)
    )
    stamp = str(report["run_at"]).replace(":", "").replace("-", "")
    target = config.state_dir / f"report-{stamp}.json"
    payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    temp = config.state_dir / f".{target.name}.tmp"
    temp.write_text(payload, encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, target)
    latest_temp = config.state_dir / ".latest.json.tmp"
    latest_temp.write_text(payload, encoding="utf-8")
    os.chmod(latest_temp, 0o600)
    os.replace(latest_temp, latest)


@contextmanager
def run_lock(config: SyncConfig) -> Iterator[None]:
    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = config.state_dir / "routine.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncBlocked("already_running") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def run_sync(
    config: SyncConfig,
    *,
    runner: Runner = run_command,
    check_runner: CheckRunner | None = None,
) -> dict[str, Any]:
    run_at = utc_now().isoformat()
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "run_at": run_at,
        "mode": "execute" if config.execute else "dry_run",
        "status": "BLOCKED",
        "outcome": "not_started",
        "auto_merge": False,
        "deploy": False,
        "force_push": False,
        "runtime_mutation": False,
    }
    try:
        with run_lock(config):
            ensure_clean_source(config, runner=runner)
            if config.fetch:
                fetch_refs(config, runner=runner)

            source_ref = f"{config.fork_remote}/{_safe_ref(config.source_branch)}"
            upstream_ref = (
                f"{config.upstream_remote}/{_safe_ref(config.upstream_branch)}"
            )
            if not ref_exists(
                config, f"refs/remotes/{source_ref}", runner=runner
            ):
                raise SyncBlocked("canonical_source_remote_ref_missing")
            if not ref_exists(
                config, f"refs/remotes/{upstream_ref}", runner=runner
            ):
                raise SyncBlocked("upstream_remote_ref_missing")

            source_sha = _git_output(config, "rev-parse", source_ref, runner=runner)
            upstream_sha = _git_output(
                config, "rev-parse", upstream_ref, runner=runner
            )
            ahead, behind = upstream_counts(
                config, source_ref, upstream_ref, runner=runner
            )
            report.update(
                {
                    "source_branch": config.source_branch,
                    "source_sha": source_sha,
                    "upstream_ref": upstream_ref,
                    "upstream_sha": upstream_sha,
                    "head_ahead": ahead,
                    "head_behind": behind,
                    "candidate_branch": config.candidate_branch,
                }
            )
            if behind == 0:
                report.update({"status": "PASS", "outcome": "up_to_date"})
                write_report(config, report)
                return report
            if not config.execute:
                report.update(
                    {"status": "PARTIAL", "outcome": "candidate_required"}
                )
                write_report(config, report)
                return report

            candidate_ref = f"{config.fork_remote}/{config.candidate_branch}"
            remote_candidate_ref = f"refs/remotes/{candidate_ref}"
            start_ref = (
                candidate_ref
                if ref_exists(config, remote_candidate_ref, runner=runner)
                else source_ref
            )
            if start_ref == candidate_ref and is_ancestor(
                config, source_ref, candidate_ref, runner=runner
            ) and is_ancestor(
                config, upstream_ref, candidate_ref, runner=runner
            ):
                candidate_sha = _git_output(
                    config, "rev-parse", candidate_ref, runner=runner
                )
                report.update(
                    {
                        "status": "PASS",
                        "outcome": "candidate_already_current",
                        "candidate_sha": candidate_sha,
                    }
                )
                if config.push_pr:
                    report["pr_url"] = push_candidate_and_ensure_pr(
                        config,
                        candidate_sha,
                        upstream_sha,
                        runner=runner,
                    )
                    report["outcome"] = "candidate_pr_ready"
                write_report(config, report)
                return report

            config.worktree_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            candidate_head, checks = build_candidate(
                config,
                source_ref=source_ref,
                upstream_ref=upstream_ref,
                start_ref=start_ref,
                runner=runner,
                check_runner=check_runner,
            )
            report.update(
                {
                    "status": "PASS",
                    "outcome": "candidate_verified",
                    "candidate_sha": candidate_head,
                    "checks": checks,
                }
            )
            if config.push_pr:
                report["pr_url"] = push_candidate_and_ensure_pr(
                    config,
                    candidate_head,
                    upstream_sha,
                    runner=runner,
                )
                report["outcome"] = "candidate_pr_ready"
            write_report(config, report)
            return report
    except SyncBlocked as exc:
        report.update(
            {
                "status": "BLOCKED",
                "outcome": "fail_closed",
                "blocker": exc.reason,
                **exc.details,
            }
        )
        write_report(config, report)
        return report
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        report.update(
            {
                "status": "BLOCKED",
                "outcome": "fail_closed",
                "blocker": "unexpected_operational_error",
            }
        )
        write_report(config, report)
        return report


def build_parser() -> argparse.ArgumentParser:
    repo_default = Path(__file__).resolve().parents[1]
    state_default = (
        Path.home() / ".hermes" / "state" / "skyai-v2-upstream-sync"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=repo_default)
    parser.add_argument("--state-dir", type=Path, default=state_default)
    parser.add_argument("--worktree-root", type=Path)
    parser.add_argument("--source-branch", default=SOURCE_BRANCH)
    parser.add_argument("--upstream-remote", default=UPSTREAM_REMOTE)
    parser.add_argument("--upstream-branch", default=UPSTREAM_BRANCH)
    parser.add_argument("--fork-remote", default=FORK_REMOTE)
    parser.add_argument("--candidate-branch", default=CANDIDATE_BRANCH)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Build and verify an isolated candidate. Never merges or deploys.",
    )
    parser.add_argument(
        "--push-pr",
        action="store_true",
        help="Push the verified rolling candidate and create/reuse a fork PR.",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Use existing refs. Intended for tests and bounded diagnostics.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.push_pr and not args.execute:
        print(
            json.dumps(
                {
                    "schema": REPORT_SCHEMA,
                    "status": "BLOCKED",
                    "blocker": "push_pr_requires_execute",
                },
                indent=2,
            )
        )
        return 2
    repo = args.repo.expanduser().resolve()
    state_dir = args.state_dir.expanduser().resolve()
    worktree_root = (
        args.worktree_root.expanduser().resolve()
        if args.worktree_root
        else state_dir / "worktrees"
    )
    config = SyncConfig(
        repo=repo,
        state_dir=state_dir,
        worktree_root=worktree_root,
        source_branch=args.source_branch,
        upstream_remote=args.upstream_remote,
        upstream_branch=args.upstream_branch,
        fork_remote=args.fork_remote,
        candidate_branch=args.candidate_branch,
        execute=args.execute,
        push_pr=args.push_pr,
        fetch=not args.no_fetch,
    )
    report = run_sync(config)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] in {"PASS", "PARTIAL"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
