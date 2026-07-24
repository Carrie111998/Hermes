from __future__ import annotations

import json
from pathlib import Path
import subprocess

from scripts import skyai_v2_upstream_sync_routine as routine


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _make_diverged_repo(tmp_path: Path, *, conflict: bool = False) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "SkyAI Sync Test")
    _git(repo, "config", "user.email", "skyai-sync-test@example.invalid")
    _write(repo / "shared.txt", "base\n")
    base = _commit(repo, "base")

    _git(repo, "switch", "-c", routine.SOURCE_BRANCH)
    if conflict:
        _write(repo / "shared.txt", "source\n")
    else:
        _write(repo / "skyai.txt", "plugin edge\n")
    source_sha = _commit(repo, "skyai source")

    _git(repo, "switch", "-c", "upstream-main", base)
    if conflict:
        _write(repo / "shared.txt", "upstream\n")
    else:
        _write(repo / "upstream.txt", "upstream core\n")
    upstream_sha = _commit(repo, "upstream")

    _git(repo, "switch", routine.SOURCE_BRANCH)
    _git(
        repo,
        "update-ref",
        f"refs/remotes/{routine.FORK_REMOTE}/{routine.SOURCE_BRANCH}",
        source_sha,
    )
    _git(
        repo,
        "update-ref",
        f"refs/remotes/{routine.UPSTREAM_REMOTE}/{routine.UPSTREAM_BRANCH}",
        upstream_sha,
    )
    return repo, source_sha, upstream_sha


def _config(
    repo: Path,
    tmp_path: Path,
    *,
    execute: bool,
) -> routine.SyncConfig:
    state = tmp_path / "state"
    return routine.SyncConfig(
        repo=repo,
        state_dir=state,
        worktree_root=state / "worktrees",
        execute=execute,
        push_pr=False,
        fetch=False,
    )


def _passing_checks(worktree: Path, upstream_ref: str) -> list[dict[str, object]]:
    assert (worktree / "skyai.txt").read_text(encoding="utf-8") == "plugin edge\n"
    assert (worktree / "upstream.txt").read_text(encoding="utf-8") == "upstream core\n"
    assert upstream_ref == f"{routine.UPSTREAM_REMOTE}/{routine.UPSTREAM_BRANCH}"
    return [{"name": "synthetic", "returncode": 0, "passed": True}]


def _failing_checks(worktree: Path, upstream_ref: str) -> list[dict[str, object]]:
    assert worktree.is_dir()
    assert upstream_ref == f"{routine.UPSTREAM_REMOTE}/{routine.UPSTREAM_BRANCH}"
    raise routine.SyncBlocked(
        "verification_failed",
        details={"failed_check": "synthetic", "returncode": 1},
    )


def test_parse_github_repo_accepts_https_and_ssh() -> None:
    assert (
        routine.parse_github_repo("https://github.com/lomliev/hermes-agent.git")
        == "lomliev/hermes-agent"
    )
    assert (
        routine.parse_github_repo("git@github.com:lomliev/hermes-agent.git")
        == "lomliev/hermes-agent"
    )


def test_parse_github_repo_rejects_non_github_remote() -> None:
    try:
        routine.parse_github_repo("https://example.invalid/repo.git")
    except routine.SyncBlocked as exc:
        assert exc.reason == "fork_remote_is_not_a_supported_github_repo"
    else:
        raise AssertionError("unsupported remote must fail closed")


def test_verification_check_names_are_stable() -> None:
    assert routine.verification_check_name(
        ("python3", "scripts/skyai_v2_upstream_sync_check.py", "origin/main")
    ) == "boundary"
    assert routine.verification_check_name(
        ("scripts/run_tests.sh", "tests/example.py", "-q")
    ) == "tests"
    assert routine.verification_check_name(
        ("git", "diff", "--check", "origin/main...HEAD")
    ) == "diff_check"


def test_dry_run_reports_candidate_required_without_creating_worktree(
    tmp_path: Path,
) -> None:
    repo, source_sha, upstream_sha = _make_diverged_repo(tmp_path)
    config = _config(repo, tmp_path, execute=False)

    report = routine.run_sync(config)

    assert report["status"] == "PARTIAL"
    assert report["outcome"] == "candidate_required"
    assert report["source_sha"] == source_sha
    assert report["upstream_sha"] == upstream_sha
    assert report["head_behind"] == 1
    assert not config.worktree_root.exists()


def test_execute_builds_verified_merge_candidate_in_isolated_worktree(
    tmp_path: Path,
) -> None:
    repo, source_sha, upstream_sha = _make_diverged_repo(tmp_path)
    config = _config(repo, tmp_path, execute=True)

    report = routine.run_sync(config, check_runner=_passing_checks)

    assert report["status"] == "PASS"
    assert report["outcome"] == "candidate_verified"
    assert report["checks"] == [
        {"name": "synthetic", "returncode": 0, "passed": True}
    ]
    candidate_sha = str(report["candidate_sha"])
    assert _git(repo, "merge-base", "--is-ancestor", source_sha, candidate_sha) == ""
    assert _git(repo, "merge-base", "--is-ancestor", upstream_sha, candidate_sha) == ""
    assert list(config.worktree_root.iterdir()) == []
    assert _git(repo, "status", "--porcelain") == ""


def test_execute_blocks_unknown_merge_conflict_and_cleans_worktree(
    tmp_path: Path,
) -> None:
    repo, _, _ = _make_diverged_repo(tmp_path, conflict=True)
    config = _config(repo, tmp_path, execute=True)

    report = routine.run_sync(config, check_runner=_passing_checks)

    assert report["status"] == "BLOCKED"
    assert report["outcome"] == "fail_closed"
    assert report["blocker"] == "merge_conflicts"
    assert report["conflicted_files"] == ["shared.txt"]
    assert list(config.worktree_root.iterdir()) == []
    assert _git(repo, "status", "--porcelain") == ""


def test_execute_blocks_failed_verification_and_cleans_worktree(
    tmp_path: Path,
) -> None:
    repo, _, _ = _make_diverged_repo(tmp_path)
    config = _config(repo, tmp_path, execute=True)

    report = routine.run_sync(config, check_runner=_failing_checks)

    assert report["status"] == "BLOCKED"
    assert report["blocker"] == "verification_failed"
    assert report["failed_check"] == "synthetic"
    assert report["returncode"] == 1
    assert list(config.worktree_root.iterdir()) == []
    assert _git(repo, "status", "--porcelain") == ""


def test_dirty_canonical_source_blocks_before_candidate_creation(
    tmp_path: Path,
) -> None:
    repo, _, _ = _make_diverged_repo(tmp_path)
    config = _config(repo, tmp_path, execute=True)
    _write(repo / "uncommitted.txt", "owner work\n")

    report = routine.run_sync(config, check_runner=_passing_checks)

    assert report["status"] == "BLOCKED"
    assert report["blocker"] == "canonical_source_worktree_is_dirty"
    assert not config.worktree_root.exists()


def test_up_to_date_source_is_a_noop(tmp_path: Path) -> None:
    repo, source_sha, _ = _make_diverged_repo(tmp_path)
    _git(
        repo,
        "update-ref",
        f"refs/remotes/{routine.UPSTREAM_REMOTE}/{routine.UPSTREAM_BRANCH}",
        source_sha,
    )
    config = _config(repo, tmp_path, execute=True)

    report = routine.run_sync(config, check_runner=_passing_checks)

    assert report["status"] == "PASS"
    assert report["outcome"] == "up_to_date"
    assert report["head_behind"] == 0
    assert not config.worktree_root.exists()


def test_nonblocking_lock_returns_stable_blocker(tmp_path: Path) -> None:
    repo, _, _ = _make_diverged_repo(tmp_path)
    config = _config(repo, tmp_path, execute=False)

    with routine.run_lock(config):
        report = routine.run_sync(config)

    assert report["status"] == "BLOCKED"
    assert report["blocker"] == "already_running"


def test_identical_reports_are_deduplicated(tmp_path: Path) -> None:
    repo, _, _ = _make_diverged_repo(tmp_path)
    config = _config(repo, tmp_path, execute=False)
    first = {
        "run_at": "2026-07-24T20:00:00+00:00",
        "status": "BLOCKED",
        "outcome": "fail_closed",
        "source_sha": "a" * 40,
        "upstream_sha": "b" * 40,
        "blocker": "verification_failed",
    }
    second = {
        **first,
        "run_at": "2026-07-24T23:00:00+00:00",
    }

    routine.write_report(config, first)
    routine.write_report(config, second)

    latest = json.loads(
        (config.state_dir / "latest.json").read_text(encoding="utf-8")
    )
    assert latest["duplicate_of_previous"] is True
