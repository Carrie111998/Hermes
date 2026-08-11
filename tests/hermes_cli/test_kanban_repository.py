from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path, PurePosixPath

import pytest

import hermes_cli.kanban_repository as repository_module
from hermes_cli.kanban_repository import (
    RepositoryConfigurationError,
    RefreshRequest,
    VerificationCommand,
    VerificationProfile,
    load_repository_contract,
    refresh_story_branch,
    resolve_commit,
    run_verification,
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


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _refresh_fixture(repository: Path) -> tuple[Path, Path]:
    _git(repository, "branch", "story")
    _git(repository, "branch", "epic")
    return repository, repository


def _refresh_request(
    story: Path,
    epic: Path,
    *,
    story_sha: str | None = None,
    epic_tip_sha: str | None = None,
) -> RefreshRequest:
    return RefreshRequest(
        repo_root=story,
        story_id="story-fixture",
        story_worktree=story,
        story_branch="story",
        story_sha=story_sha or _git(story, "rev-parse", "story"),
        epic_branch="epic",
        epic_tip_sha=epic_tip_sha or _git(epic, "rev-parse", "epic"),
    )


def test_refresh_story_branch_advances_clean_story_by_isolated_cas(repository: Path, tmp_path: Path):
    story, epic = _refresh_fixture(repository)
    _git(repository, "checkout", "epic")
    _commit(epic, "epic.txt", "epic\n", "epic change")
    _git(repository, "checkout", "story")
    before = _git(story, "rev-parse", "story")

    result = refresh_story_branch(_refresh_request(story, epic, story_sha=before))

    assert result.kind == "refreshed"
    assert result.before_sha == before
    assert result.after_sha == _git(story, "rev-parse", "story")
    assert result.after_sha != before
    assert (story / "epic.txt").read_text(encoding="utf-8") == "epic\n"
    assert _git(story, "status", "--porcelain", "--untracked-files=all") == ""


def test_refresh_story_branch_returns_dirty_evidence_without_touching_story(repository: Path, tmp_path: Path):
    story, epic = _refresh_fixture(repository)
    _git(repository, "checkout", "epic")
    _commit(epic, "epic.txt", "epic\n", "epic change")
    _git(repository, "checkout", "story")
    dirty = story / "operator-note.txt"
    dirty.write_text("keep me\n", encoding="utf-8")
    before = _git(story, "rev-parse", "story")

    result = refresh_story_branch(_refresh_request(story, epic, story_sha=before))

    assert result.kind == "dirty"
    assert result.before_sha == before
    assert result.after_sha is None
    assert result.dirty_paths == ("operator-note.txt",)
    assert _git(story, "rev-parse", "story") == before
    assert dirty.read_text(encoding="utf-8") == "keep me\n"


def test_refresh_story_branch_returns_conflict_and_retains_isolated_evidence(
    repository: Path, tmp_path: Path
):
    story, epic = _refresh_fixture(repository)
    _git(repository, "checkout", "story")
    _commit(story, "shared.txt", "story\n", "story change")
    _git(repository, "checkout", "epic")
    _commit(epic, "shared.txt", "epic\n", "epic change")
    _git(repository, "checkout", "story")
    before = _git(story, "rev-parse", "story")

    result = refresh_story_branch(_refresh_request(story, epic, story_sha=before))

    assert result.kind == "conflict"
    assert result.before_sha == before
    assert result.after_sha is None
    assert result.conflict_worktree is not None
    assert result.conflict_worktree.is_dir()
    assert result.conflict_paths == ("shared.txt",)
    assert _git(story, "rev-parse", "story") == before
    assert _git(story, "status", "--porcelain", "--untracked-files=all") == ""


def test_refresh_story_branch_returns_source_moved_evidence(repository: Path, tmp_path: Path):
    story, epic = _refresh_fixture(repository)
    _git(repository, "checkout", "epic")
    _commit(epic, "epic.txt", "epic\n", "epic change")
    _git(repository, "checkout", "story")
    pinned_story_sha = _git(story, "rev-parse", "story")
    _commit(story, "story.txt", "moved\n", "move story source")
    moved_story_sha = _git(story, "rev-parse", "story")

    result = refresh_story_branch(
        _refresh_request(story, epic, story_sha=pinned_story_sha)
    )

    assert result.kind == "source_moved"
    assert result.before_sha == pinned_story_sha
    assert result.current_sha == moved_story_sha
    assert _git(story, "rev-parse", "story") == moved_story_sha


def test_refresh_story_branch_detects_source_move_between_merge_and_cas(
    repository: Path, monkeypatch: pytest.MonkeyPatch
):
    story, epic = _refresh_fixture(repository)
    _git(repository, "checkout", "epic")
    _commit(epic, "epic.txt", "epic\n", "epic change")
    _git(repository, "checkout", "story")
    before = _git(story, "rev-parse", "story")
    original_refresh_git = repository_module._refresh_git
    moved = False

    def move_story_before_cas(path: Path, *args: str, **kwargs):
        nonlocal moved
        if args[:1] == ("update-ref",) and not moved:
            moved = True
            _commit(story, "story-late.txt", "late\n", "story moved during refresh")
        return original_refresh_git(path, *args, **kwargs)

    monkeypatch.setattr(repository_module, "_refresh_git", move_story_before_cas)
    result = refresh_story_branch(_refresh_request(story, epic, story_sha=before))

    moved_story_sha = _git(story, "rev-parse", "story")
    assert result.kind == "source_moved"
    assert result.before_sha == before
    assert result.current_sha == moved_story_sha
    assert moved_story_sha != before
    assert _git(story, "status", "--porcelain", "--untracked-files=all") == ""


def test_refresh_story_branch_rechecks_dirty_worktree_before_cas(
    repository: Path, monkeypatch: pytest.MonkeyPatch
):
    story, epic = _refresh_fixture(repository)
    _git(repository, "checkout", "epic")
    _commit(epic, "epic.txt", "epic\n", "epic change")
    _git(repository, "checkout", "story")
    before = _git(story, "rev-parse", "story")
    original_refresh_git = repository_module._refresh_git
    status_checks = 0
    dirtied = False

    def dirty_story_before_cas(path: Path, *args: str, **kwargs):
        nonlocal dirtied, status_checks
        if args[:1] == ("status",):
            status_checks += 1
        if args[:1] == ("status",) and status_checks == 2 and not dirtied:
            dirtied = True
            (story / "README.md").write_text("operator edit\n", encoding="utf-8")
        return original_refresh_git(path, *args, **kwargs)

    monkeypatch.setattr(repository_module, "_refresh_git", dirty_story_before_cas)
    result = refresh_story_branch(_refresh_request(story, epic, story_sha=before))

    assert result.kind == "dirty"
    assert result.before_sha == before
    assert result.after_sha is None
    assert result.dirty_paths == ("README.md",)
    assert _git(story, "rev-parse", "story") == before
    assert (story / "README.md").read_text(encoding="utf-8") == "operator edit\n"


def _write_verification_script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env python3\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _verification_profile(*commands: VerificationCommand) -> VerificationProfile:
    return VerificationProfile(tuple(commands))


def _verification_command(
    candidate: Path,
    executable: Path,
    *args: str,
    workdir: str = ".",
    timeout_seconds: int = 5,
) -> VerificationCommand:
    workdir_path = candidate / Path(workdir)
    return VerificationCommand(
        argv=(executable.relative_to(workdir_path).as_posix(), *args),
        workdir=PurePosixPath(workdir),
        timeout_seconds=timeout_seconds,
    )


def test_run_verification_uses_configured_argv_workdir_and_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    candidate = tmp_path / "candidate"
    script = _write_verification_script(
        candidate / "nested" / "record.py",
        "import json, os; print('api_key=should-not-survive'); print(json.dumps({'argv': __import__('sys').argv[1:], 'cwd': os.getcwd(), 'secret': os.environ.get('R03_SECRET')}))",
    )
    monkeypatch.setenv("R03_SECRET", "must-not-cross-boundary")
    profile = _verification_profile(
        _verification_command(
            candidate,
            script,
            "--first",
            "value",
            workdir="nested",
            timeout_seconds=7,
        )
    )

    result = run_verification(
        profile,
        candidate,
        source_sha="source-sha",
        candidate_sha="candidate-sha",
        contract_digest="contract-digest",
        scope="story_integration",
        subject_id="story-1",
    )

    assert result.status == "passed"
    assert result.source_sha == "source-sha"
    assert result.candidate_sha == "candidate-sha"
    assert result.contract_digest == "contract-digest"
    assert result.profile == "story_integration"
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.status == "passed"
    assert step.argv[1:] == ("--first", "value")
    assert step.workdir == PurePosixPath("nested")
    payload = json.loads(step.stdout_tail.splitlines()[-1])
    assert payload["argv"] == ["--first", "value"]
    assert Path(payload["cwd"]) == candidate / "nested"
    assert payload["secret"] is None
    assert "should-not-survive" not in step.stdout_tail
    assert "[REDACTED]" in step.stdout_tail


def test_run_verification_stops_on_nonzero_and_caps_output(tmp_path: Path):
    candidate = tmp_path / "candidate"
    failing = _write_verification_script(
        candidate / "fail.py",
        "print('x' * 10000); raise SystemExit(3)",
    )
    marker = candidate / "should-not-run"
    following = _write_verification_script(
        candidate / "following.py",
        f"__import__('pathlib').Path({str(marker)!r}).touch()",
    )
    profile = _verification_profile(
        _verification_command(candidate, failing),
        _verification_command(candidate, following),
    )

    result = run_verification(
        profile,
        candidate,
        source_sha="source",
        candidate_sha="candidate",
        contract_digest="digest",
        scope="epic_release",
        subject_id="epic-1",
    )

    assert result.status == "failed"
    assert len(result.steps) == 1
    assert result.steps[0].returncode == 3
    assert len(result.steps[0].stdout_tail) <= 4096
    assert not marker.exists()


def test_run_verification_classifies_timeout_as_infrastructure_error(tmp_path: Path):
    candidate = tmp_path / "candidate"
    sleeper = _write_verification_script(
        candidate / "sleep.py",
        "__import__('time').sleep(2)",
    )
    profile = _verification_profile(
        _verification_command(candidate, sleeper, timeout_seconds=1),
    )

    result = run_verification(
        profile,
        candidate,
        source_sha="source",
        candidate_sha="candidate",
        contract_digest="digest",
        scope="story_integration",
        subject_id="story-1",
    )

    assert result.status == "infrastructure_error"
    assert len(result.steps) == 1
    assert result.steps[0].status == "infrastructure_error"
    assert result.steps[0].error == "timeout"


def test_run_verification_missing_profile_is_configuration_error(tmp_path: Path):
    result = run_verification(
        None,
        tmp_path,
        source_sha="source",
        candidate_sha="candidate",
        contract_digest="digest",
        scope="story_integration",
        subject_id="story-1",
    )

    assert result.status == "configuration_error"
    assert result.steps == ()


def test_run_verification_missing_executable_is_configuration_error(tmp_path: Path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    profile = _verification_profile(
        VerificationCommand(
            argv=("does-not-exist-r03",),
            workdir=PurePosixPath("."),
            timeout_seconds=5,
        )
    )

    result = run_verification(
        profile,
        candidate,
        source_sha="source",
        candidate_sha="candidate",
        contract_digest="digest",
        scope="story_integration",
        subject_id="story-1",
    )

    assert result.status == "configuration_error"
    assert result.steps == ()


def test_run_verification_process_error_is_infrastructure_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    candidate = tmp_path / "candidate"
    script = _write_verification_script(candidate / "runner.py", "print('ok')")
    profile = _verification_profile(_verification_command(candidate, script))

    def fail_process(*args, **kwargs):
        raise OSError("process unavailable")

    monkeypatch.setattr(repository_module.subprocess, "run", fail_process)
    result = run_verification(
        profile,
        candidate,
        source_sha="source",
        candidate_sha="candidate",
        contract_digest="digest",
        scope="story_integration",
        subject_id="story-1",
    )

    assert result.status == "infrastructure_error"
    assert len(result.steps) == 1
    assert result.steps[0].status == "infrastructure_error"
    assert result.steps[0].error == "process_error"
