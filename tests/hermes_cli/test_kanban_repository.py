from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

from hermes_cli.kanban_repository import (
    RepositoryConfigurationError,
    load_repository_contract,
    resolve_commit,
)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "tests@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Repository Tests"],
        check=True,
    )
    (repo / "dashboard").mkdir()
    (repo / "dashboard" / "index.html").write_text("index\n", encoding="utf-8")
    (repo / "dashboard" / "data.json").write_text("{}\n", encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "run_tests.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", sha],
        check=True,
    )
    return repo


def board_metadata() -> dict[str, object]:
    return {
        "repository": {
            "base_ref": "refs/remotes/origin/main",
            "target_branch": "main",
            "verification_profiles": {
                "story_integration": [
                    {
                        "argv": ["bash", "scripts/run_tests.sh"],
                        "workdir": ".",
                        "timeout_seconds": 1800,
                    }
                ],
                "epic_release": [
                    {
                        "argv": ["bash", "scripts/run_tests.sh"],
                        "workdir": ".",
                        "timeout_seconds": 1800,
                    }
                ],
            },
            "ci_observation": {
                "provider": "github_actions",
                "required_workflows": ["CI", "Deploy Test"],
            },
            "boundary_evidence": {
                "test_globs": ["tests/**"],
                "fixture_globs": ["tests/fixtures/**"],
                "generated_paths": ["dashboard/index.html", "dashboard/data.json"],
            },
        }
    }


def test_contract_normalizes_commands_and_generated_paths(repository: Path):
    contract = load_repository_contract(board_metadata(), repo_root=repository)

    assert contract.repo_root == repository.resolve()
    assert contract.base_ref == "refs/remotes/origin/main"
    assert contract.target_branch == "main"
    assert contract.generated_paths == (
        PurePosixPath("dashboard/index.html"),
        PurePosixPath("dashboard/data.json"),
    )
    assert contract.verification["story_integration"].commands[0].argv == (
        "bash",
        "scripts/run_tests.sh",
    )
    assert contract.verification["story_integration"].commands[0].workdir == PurePosixPath(".")
    assert contract.ci_workflows == ("CI", "Deploy Test")
    assert len(contract.digest) == 64


def test_contract_digest_is_order_independent_for_mapping_keys(repository: Path):
    first = board_metadata()
    second = board_metadata()
    repository_policy = second["repository"]
    assert isinstance(repository_policy, dict)
    second["repository"] = {
        "boundary_evidence": repository_policy["boundary_evidence"],
        "ci_observation": repository_policy["ci_observation"],
        "verification_profiles": repository_policy["verification_profiles"],
        "target_branch": repository_policy["target_branch"],
        "base_ref": repository_policy["base_ref"],
    }

    assert load_repository_contract(first, repo_root=repository).digest == load_repository_contract(
        second, repo_root=repository
    ).digest


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda policy: policy.pop("base_ref"), "missing_base_ref"),
        (lambda policy: policy.update(base_ref=""), "malformed_base_ref"),
        (lambda policy: policy.update(target_branch=""), "malformed_target_branch"),
        (lambda policy: policy.update(unknown=True), "unknown_key"),
    ],
)
def test_contract_rejects_malformed_top_level_policy(repository: Path, mutator, code: str):
    metadata = board_metadata()
    policy = metadata["repository"]
    assert isinstance(policy, dict)
    mutator(policy)

    with pytest.raises(RepositoryConfigurationError) as exc_info:
        load_repository_contract(metadata, repo_root=repository)

    assert exc_info.value.code == code


def test_contract_rejects_unknown_nested_keys(repository: Path):
    metadata = board_metadata()
    policy = metadata["repository"]
    assert isinstance(policy, dict)
    profiles = policy["verification_profiles"]
    assert isinstance(profiles, dict)
    profiles["story_integration"][0]["shell"] = True

    with pytest.raises(RepositoryConfigurationError) as exc_info:
        load_repository_contract(metadata, repo_root=repository)

    assert exc_info.value.code == "unknown_key"


@pytest.mark.parametrize("generated_path", ["/tmp/output.txt", "../outside.txt", "dashboard/missing.txt"])
def test_contract_rejects_invalid_generated_paths(repository: Path, generated_path: str):
    metadata = board_metadata()
    policy = metadata["repository"]
    assert isinstance(policy, dict)
    boundary = policy["boundary_evidence"]
    assert isinstance(boundary, dict)
    boundary["generated_paths"] = [generated_path]

    with pytest.raises(RepositoryConfigurationError) as exc_info:
        load_repository_contract(metadata, repo_root=repository)

    assert exc_info.value.code in {"invalid_path", "untracked_path"}


def test_contract_rejects_workdir_escape(repository: Path):
    metadata = board_metadata()
    policy = metadata["repository"]
    assert isinstance(policy, dict)
    profiles = policy["verification_profiles"]
    assert isinstance(profiles, dict)
    profiles["story_integration"][0]["workdir"] = "../outside"

    with pytest.raises(RepositoryConfigurationError) as exc_info:
        load_repository_contract(metadata, repo_root=repository)

    assert exc_info.value.code == "invalid_workdir"


def test_resolve_commit_uses_configured_ref_not_checked_out_head(repository: Path):
    (repository / "later.txt").write_text("later\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "later.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "later"],
        check=True,
        capture_output=True,
    )

    configured = resolve_commit(repository, "refs/remotes/origin/main")
    head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert configured != head
    assert len(configured) == 40


def test_resolve_commit_rejects_missing_ref(repository: Path):
    with pytest.raises(RepositoryConfigurationError) as exc_info:
        resolve_commit(repository, "refs/remotes/origin/missing")

    assert exc_info.value.code == "missing_ref"


def test_resolve_commit_rejects_ambiguous_ref(repository: Path):
    subprocess.run(["git", "-C", str(repository), "branch", "shared"], check=True)
    subprocess.run(["git", "-C", str(repository), "tag", "shared"], check=True)

    with pytest.raises(RepositoryConfigurationError) as exc_info:
        resolve_commit(repository, "shared")

    assert exc_info.value.code == "missing_ref"
