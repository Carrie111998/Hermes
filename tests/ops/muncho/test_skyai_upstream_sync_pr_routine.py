from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[3]
MODULE = ROOT / "ops/muncho/runtime/skyai_upstream_sync_pr_routine.py"
SPEC = importlib.util.spec_from_file_location(
    "skyai_upstream_sync_pr_routine_test",
    MODULE,
)
assert SPEC and SPEC.loader
routine = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = routine
SPEC.loader.exec_module(routine)


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


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_fixed_verification_set_covers_architecture_voice_and_sync() -> None:
    assert "tests/plugins/test_skyai_customer_architecture.py" in routine.TEST_FILES
    assert "tests/plugins/test_skyai_customer_voice_contract.py" in routine.TEST_FILES
    assert "tests/scripts/test_skyai_v2_upstream_sync_check.py" in routine.TEST_FILES
    assert "tests/scripts/test_skyai_v2_upstream_sync_routine.py" in routine.TEST_FILES
    assert not any("scheduler" in path for path in routine.TEST_FILES)
    source = MODULE.read_text(encoding="utf-8")
    assert '"scripts/run_tests.sh"' not in source
    assert '"py_compile"' not in source


def test_unknown_merge_conflict_fails_closed_without_resolver(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repo, "base")

    _git(repo, "switch", "-c", "source")
    (repo / "shared.txt").write_text("source\n", encoding="utf-8")
    _commit(repo, "source")

    _git(repo, "switch", "-c", "upstream", base)
    (repo / "shared.txt").write_text("upstream\n", encoding="utf-8")
    _commit(repo, "upstream")

    _git(repo, "switch", "source")
    with pytest.raises(routine.SkyAISyncBlocked) as raised:
        routine.merge_exact(repo, "upstream", "merge upstream")

    assert raised.value.code == "merge_conflicts"
    assert raised.value.details["conflicted_files"] == ["shared.txt"]
    assert _git(repo, "status", "--porcelain") == ""
    assert (repo / "shared.txt").read_text(encoding="utf-8") == "source\n"


def test_push_and_pr_targets_are_fork_only_without_merge_or_deploy() -> None:
    source = MODULE.read_text(encoding="utf-8")
    assert 'FORK_GIT_URL = "https://github.com/lomliev/hermes-agent.git"' in source
    assert 'UPSTREAM_GIT_URL = "https://github.com/NousResearch/hermes-agent.git"' in source
    assert '"push",\n        FORK_GIT_URL' in source
    assert '"push",\n        UPSTREAM_GIT_URL' not in source
    assert '"pr",\n                "create"' in source
    assert '"pr",\n                "merge"' not in source
    assert "muncho-auto-deploy-release" not in source
    assert '"auto_merge": False' in source
    assert '"deploy": False' in source
    assert '"runtime_mutation": False' in source


def test_github_ci_failure_is_a_stable_blocker(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        routine,
        "gh_json",
        lambda _args, cwd: {
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "baseRefName": routine.SOURCE_BRANCH,
            "headRefName": routine.CANDIDATE_BRANCH,
            "statusCheckRollup": [
                {
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                },
                {
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                },
            ],
        },
    )

    result = routine.candidate_ci_status(
        tmp_path,
        "https://github.com/lomliev/hermes-agent/pull/178",
        "a" * 40,
    )

    assert result["status"] == "BLOCKED"
    assert result["outcome"] == "candidate_ci_failed"
    assert result["blocker"] == "github_ci_failed"
    assert result["check"]["passed"] is False


def test_pending_github_ci_is_partial_not_pass(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        routine,
        "gh_json",
        lambda _args, cwd: {
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "baseRefName": routine.SOURCE_BRANCH,
            "headRefName": routine.CANDIDATE_BRANCH,
            "statusCheckRollup": [
                {
                    "status": "IN_PROGRESS",
                    "conclusion": "",
                }
            ],
        },
    )

    result = routine.candidate_ci_status(
        tmp_path,
        "https://github.com/lomliev/hermes-agent/pull/178",
        "a" * 40,
    )

    assert result["status"] == "PARTIAL"
    assert result["outcome"] == "candidate_ci_pending"
    assert result["check"]["passed"] is None


def test_module_compiles_in_isolated_stdlib() -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(MODULE), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
