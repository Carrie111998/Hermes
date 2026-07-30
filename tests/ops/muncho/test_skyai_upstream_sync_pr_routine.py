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
TEST_CANDIDATE_ID = "a" * 64
TEST_CANDIDATE_BRANCH = routine.candidate_branch(TEST_CANDIDATE_ID)
TEST_REPOSITORY_IDENTITY = {
    "isCrossRepository": False,
    "headRepository": {"nameWithOwner": routine.FORK_REPO},
    "headRepositoryOwner": {"login": routine.FORK_OWNER},
}


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


def test_open_candidate_head_is_stable_and_new_commits_are_tail_drift(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repo, "base")

    _git(repo, "switch", "-c", "candidate", base)
    (repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    candidate = _commit(repo, "candidate")

    _git(repo, "switch", "-c", "source", candidate)
    (repo / "source-tail.txt").write_text("source\n", encoding="utf-8")
    _commit(repo, "source tail")

    _git(repo, "switch", "-c", "upstream", candidate)
    (repo / "upstream-tail.txt").write_text("upstream\n", encoding="utf-8")
    _commit(repo, "upstream tail")

    state = routine.inspect_open_candidate(
        repo,
        candidate_ref="candidate",
        source_ref="source",
        upstream_ref="upstream",
        candidate_exists=True,
        candidates=[{"headRefOid": candidate}],
    )

    assert state == {
        "head": candidate,
        "source_tail_ahead": 0,
        "source_tail_behind": 1,
        "upstream_tail_ahead": 0,
        "upstream_tail_behind": 1,
    }
    assert _git(repo, "rev-parse", "candidate") == candidate


def test_open_candidate_requires_exact_pr_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    head = _commit(repo, "base")

    with pytest.raises(routine.SkyAISyncBlocked) as raised:
        routine.inspect_open_candidate(
            repo,
            candidate_ref="main",
            source_ref="main",
            upstream_ref="main",
            candidate_exists=True,
            candidates=[{"headRefOid": "f" * 40}],
        )

    assert raised.value.code == "candidate_pr_head_mismatch"
    assert _git(repo, "rev-parse", "main") == head


def test_push_and_pr_targets_are_fork_only_without_merge_or_deploy() -> None:
    source = MODULE.read_text(encoding="utf-8")
    assert 'FORK_GIT_URL = "https://github.com/lomliev/hermes-agent.git"' in source
    assert 'UPSTREAM_GIT_URL = "https://github.com/NousResearch/hermes-agent.git"' in source
    assert '"push",\n        FORK_GIT_URL' in source
    assert '"push",\n        UPSTREAM_GIT_URL' not in source
    assert '"pr",\n                "create"' in source
    assert (
        '"--head",\n                candidate_branch_name,\n'
        '                "--draft",'
        in source
    )
    assert '"pr",\n                "merge"' not in source
    assert "muncho-auto-deploy-release" not in source
    assert '"auto_merge": False' in source
    assert '"deploy": False' in source
    assert '"runtime_mutation": False' in source


def test_candidate_branch_is_unique_and_exact_query_uses_plain_branch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = routine.candidate_branch("1" * 64)
    second = routine.candidate_branch("2" * 64)
    assert first != second
    observed: list[tuple[str, ...]] = []

    def fake_gh_json(args, *, cwd):
        del cwd
        observed.append(tuple(args))
        return []

    monkeypatch.setattr(routine, "gh_json", fake_gh_json)

    assert (
        routine.exact_branch_candidate_prs(
            tmp_path,
            expected_head="3" * 40,
            expected_branch=first,
        )
        == []
    )
    command = observed[0]
    assert command[command.index("--head") + 1] == first
    assert f"{routine.FORK_OWNER}:{first}" not in command


def test_cross_repository_pr_cannot_match_exact_candidate() -> None:
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id=TEST_CANDIDATE_ID,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.SOURCE_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch=TEST_CANDIDATE_BRANCH,
        head_sha="3" * 40,
        base_sha="1" * 40,
        upstream_sha="2" * 40,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    published = routine.publish_candidate_manifest(prepared, pr_number=178)
    lookalike = {
        "number": 178,
        "headRefName": TEST_CANDIDATE_BRANCH,
        "headRefOid": "3" * 40,
        "baseRefName": routine.SOURCE_BRANCH,
        "isCrossRepository": True,
        "headRepository": {"nameWithOwner": "attacker/hermes-agent"},
        "headRepositoryOwner": {"login": "attacker"},
    }

    mismatches = routine.candidate_manifest_pr_mismatches(
        published,
        lookalike,
    )

    assert "candidate_pr_cross_repository_mismatch" in mismatches
    assert "candidate_pr_head_repository_mismatch" in mismatches
    assert "candidate_pr_head_owner_mismatch" in mismatches


def test_open_candidate_execute_performs_no_external_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_sha = "1" * 40
    upstream_sha = "2" * 40
    candidate_sha = "3" * 40
    candidate = {
        **TEST_REPOSITORY_IDENTITY,
        "number": 178,
        "url": "https://github.com/lomliev/hermes-agent/pull/178",
        "state": "OPEN",
        "headRefName": TEST_CANDIDATE_BRANCH,
        "headRefOid": candidate_sha,
        "baseRefName": routine.SOURCE_BRANCH,
    }
    monkeypatch.setenv(routine.EXECUTE_ENV, "1")
    monkeypatch.setattr(routine, "WORKTREE_ROOT", tmp_path / "worktrees")
    monkeypatch.setattr(
        routine,
        "AUTO_STATE",
        tmp_path / "state" / "candidate.json",
    )
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id=TEST_CANDIDATE_ID,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.SOURCE_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch=TEST_CANDIDATE_BRANCH,
        head_sha=candidate_sha,
        base_sha=source_sha,
        upstream_sha=upstream_sha,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    routine.append_candidate_manifest(
        routine.AUTO_STATE,
        routine.publish_candidate_manifest(prepared, pr_number=178),
    )
    monkeypatch.setattr(
        routine.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": routine.MIN_FREE_BYTES + 1})(),
    )
    monkeypatch.setattr(routine, "safe_rmtree", lambda _path: None)
    monkeypatch.setattr(routine, "clone_refs", lambda _repo, _branch=None: None)
    monkeypatch.setattr(
        routine,
        "rev_parse",
        lambda _repo, ref: {
            f"origin/{routine.SOURCE_BRANCH}": source_sha,
            f"upstream/{routine.UPSTREAM_BRANCH}": upstream_sha,
            f"origin/{TEST_CANDIDATE_BRANCH}": candidate_sha,
        }[ref],
    )
    monkeypatch.setattr(routine, "ahead_behind", lambda *_args: (0, 1))
    monkeypatch.setattr(routine, "ref_exists", lambda *_args: True)
    monkeypatch.setattr(
        routine,
        "candidate_pr_view",
        lambda *_args: candidate,
    )
    monkeypatch.setattr(
        routine,
        "candidate_ci_status",
        lambda *_args: {
            "status": "PASS",
            "outcome": "candidate_pr_ci_green",
            "check": {"name": "github_ci", "passed": True},
        },
    )
    external_commands: list[tuple[str, ...]] = []

    def forbidden_run(args, **_kwargs):
        external_commands.append(tuple(args))
        raise AssertionError("open candidate attempted an external command")

    reports: list[dict] = []
    monkeypatch.setattr(routine, "run", forbidden_run)
    monkeypatch.setattr(
        routine,
        "write_report",
        lambda report: reports.append(dict(report)),
    )

    report = routine.execute()

    assert external_commands == []
    assert report["status"] == "PARTIAL"
    assert report["outcome"] == "candidate_pr_ci_green_tail_pending"
    assert report["candidate_sha"] == candidate_sha
    assert reports[-1] == report


def test_candidate_create_remote_mutation_trace_is_fork_draft_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_sha = "1" * 40
    upstream_sha = "2" * 40
    candidate_sha = "3" * 40
    monkeypatch.setenv(routine.EXECUTE_ENV, "1")
    monkeypatch.setattr(routine, "WORKTREE_ROOT", tmp_path / "worktrees")
    monkeypatch.setattr(
        routine,
        "AUTO_STATE",
        tmp_path / "state" / "candidate.json",
    )
    monkeypatch.setattr(
        routine.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": routine.MIN_FREE_BYTES + 1})(),
    )
    monkeypatch.setattr(routine, "safe_rmtree", lambda _path: None)
    monkeypatch.setattr(routine, "clone_refs", lambda _repo, _branch=None: None)
    monkeypatch.setattr(
        routine,
        "rev_parse",
        lambda _repo, ref: {
            f"origin/{routine.SOURCE_BRANCH}": source_sha,
            f"upstream/{routine.UPSTREAM_BRANCH}": upstream_sha,
            "HEAD": candidate_sha,
        }[ref],
    )
    monkeypatch.setattr(routine, "ahead_behind", lambda *_args: (0, 1))
    monkeypatch.setattr(
        routine,
        "reserved_candidate_prs",
        lambda _repo, _branch: [],
    )
    monkeypatch.setattr(
        routine,
        "exact_branch_candidate_prs",
        lambda _repo, *, expected_head, expected_branch: [
            {
                **TEST_REPOSITORY_IDENTITY,
                "number": 178,
                "url": "https://github.com/lomliev/hermes-agent/pull/178",
                "state": "OPEN",
                "headRefName": expected_branch,
                "headRefOid": expected_head,
                "baseRefName": routine.SOURCE_BRANCH,
            }
        ],
    )
    monkeypatch.setattr(routine, "ref_exists", lambda *_args: False)
    monkeypatch.setattr(routine, "is_ancestor", lambda *_args: False)
    monkeypatch.setattr(routine, "merge_exact", lambda *_args: None)
    monkeypatch.setattr(routine, "run_static_checks", lambda *_args: [])
    monkeypatch.setattr(
        routine,
        "candidate_ci_status",
        lambda *_args: {
            "status": "PASS",
            "outcome": "candidate_pr_ci_green",
            "check": {"name": "github_ci", "passed": True},
        },
    )
    monkeypatch.setattr(routine, "write_report", lambda _report: None)
    commands: list[tuple[str, ...]] = []

    def fake_run(
        args,
        *,
        cwd=None,
        check=True,
        timeout=1200,
        environment=None,
    ):
        del cwd, check, timeout, environment
        command = tuple(str(item) for item in args)
        commands.append(command)
        stdout = (
            "https://github.com/lomliev/hermes-agent/pull/178\n"
            if command[:3] == (str(routine.GH), "pr", "create")
            else ""
        )
        return routine.CmdResult(0, stdout, "")

    monkeypatch.setattr(routine, "run", fake_run)

    report = routine.execute()

    pushes = [
        command
        for command in commands
        if command[0] == "git" and "push" in command
    ]
    creates = [
        command
        for command in commands
        if command[:3] == (str(routine.GH), "pr", "create")
    ]
    assert report["status"] == "PASS"
    assert len(pushes) == 1
    assert routine.FORK_GIT_URL in pushes[0]
    assert routine.UPSTREAM_GIT_URL not in pushes[0]
    assert len(creates) == 1
    assert creates[0][creates[0].index("--repo") + 1] == routine.FORK_REPO
    assert "--draft" in creates[0]
    assert not any(
        command[:3]
        in {
            (str(routine.GH), "pr", "merge"),
            (str(routine.GH), "pr", "close"),
        }
        for command in commands
    )
    assert not any(
        token in {"deploy", "restart", "systemctl"}
        for command in commands
        for token in command
    )


def test_prepared_ledger_failure_preserves_only_local_candidate_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_sha = "1" * 40
    upstream_sha = "2" * 40
    candidate_sha = "3" * 40
    worktree_root = tmp_path / "worktrees"
    candidate_repo = worktree_root / "skyai-upstream-sync"
    removals: list[Path] = []
    monkeypatch.setenv(routine.EXECUTE_ENV, "1")
    monkeypatch.setattr(routine, "WORKTREE_ROOT", worktree_root)
    monkeypatch.setattr(routine, "AUTO_STATE", tmp_path / "state/candidate.json")
    monkeypatch.setattr(
        routine.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": routine.MIN_FREE_BYTES + 1})(),
    )

    def safe_remove(path: Path) -> None:
        removals.append(path)

    def clone(_repo: Path, _branch=None) -> None:
        candidate_repo.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(routine, "safe_rmtree", safe_remove)
    monkeypatch.setattr(routine, "clone_refs", clone)
    monkeypatch.setattr(
        routine,
        "rev_parse",
        lambda _repo, ref: {
            f"origin/{routine.SOURCE_BRANCH}": source_sha,
            f"upstream/{routine.UPSTREAM_BRANCH}": upstream_sha,
            "HEAD": candidate_sha,
        }[ref],
    )
    monkeypatch.setattr(routine, "ahead_behind", lambda *_args: (0, 1))
    monkeypatch.setattr(routine, "git", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routine, "ref_exists", lambda *_args: False)
    monkeypatch.setattr(
        routine,
        "reserved_candidate_prs",
        lambda *_args: [],
    )
    monkeypatch.setattr(routine, "is_ancestor", lambda *_args: False)
    monkeypatch.setattr(routine, "merge_exact", lambda *_args: None)
    monkeypatch.setattr(routine, "run_static_checks", lambda *_args: [])
    monkeypatch.setattr(
        routine,
        "append_candidate_manifest",
        lambda *_args: (_ for _ in ()).throw(
            routine.CandidateManifestError("injected_pointer_failure")
        ),
    )
    monkeypatch.setattr(routine, "write_report", lambda _report: None)

    report = routine.execute()

    assert report["blocker"] == "unexpected_operational_error"
    assert removals == [candidate_repo]
    assert candidate_repo.is_dir()


def test_prepared_candidate_recovers_existing_exact_pr_without_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state" / "candidate.json"
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id=TEST_CANDIDATE_ID,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.SOURCE_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch=TEST_CANDIDATE_BRANCH,
        head_sha="3" * 40,
        base_sha="1" * 40,
        upstream_sha="2" * 40,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    monkeypatch.setattr(routine, "AUTO_STATE", state)
    routine.append_candidate_manifest(state, prepared)
    candidate = {
        **TEST_REPOSITORY_IDENTITY,
        "number": 178,
        "url": "https://github.com/lomliev/hermes-agent/pull/178",
        "state": "OPEN",
        "headRefName": TEST_CANDIDATE_BRANCH,
        "headRefOid": prepared["head_sha"],
        "baseRefName": routine.SOURCE_BRANCH,
    }
    monkeypatch.setattr(routine, "_validate_prepared_repo", lambda *_args: None)
    monkeypatch.setattr(
        routine,
        "exact_branch_candidate_prs",
        lambda *_args, **_kwargs: [candidate],
    )
    monkeypatch.setattr(
        routine,
        "push_candidate",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("existing exact PR was pushed")
        ),
    )

    published, observed = routine._recover_prepared_candidate(
        tmp_path,
        prepared,
    )

    assert observed == candidate
    assert published["phase"] == "published"
    assert routine.recover_candidate_manifest(state) == published


def test_prepared_candidate_crash_recovery_creates_exact_pr_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state" / "candidate.json"
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id=TEST_CANDIDATE_ID,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.SOURCE_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch=TEST_CANDIDATE_BRANCH,
        head_sha="3" * 40,
        base_sha="1" * 40,
        upstream_sha="2" * 40,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    monkeypatch.setattr(routine, "AUTO_STATE", state)
    routine.append_candidate_manifest(state, prepared)
    candidate = {
        **TEST_REPOSITORY_IDENTITY,
        "number": 178,
        "url": "https://github.com/lomliev/hermes-agent/pull/178",
        "state": "OPEN",
        "headRefName": TEST_CANDIDATE_BRANCH,
        "headRefOid": prepared["head_sha"],
        "baseRefName": routine.SOURCE_BRANCH,
    }
    observed_lists = iter(([], [candidate]))
    counters = {"push": 0, "create": 0}
    monkeypatch.setattr(routine, "_validate_prepared_repo", lambda *_args: None)
    monkeypatch.setattr(
        routine,
        "exact_branch_candidate_prs",
        lambda *_args, **_kwargs: next(observed_lists),
    )
    monkeypatch.setattr(
        routine,
        "push_candidate",
        lambda *_args: counters.__setitem__("push", counters["push"] + 1),
    )
    monkeypatch.setattr(
        routine,
        "ensure_pr",
        lambda *_args, **_kwargs: (
            counters.__setitem__("create", counters["create"] + 1)
            or candidate["url"]
        ),
    )

    published, _observed = routine._recover_prepared_candidate(
        tmp_path,
        prepared,
    )

    assert counters == {"push": 1, "create": 1}
    assert routine.recover_candidate_manifest(state) == published


@pytest.mark.parametrize(
    ("candidate_state", "candidate_head", "expected_blocker"),
    (
        ("OPEN", "4" * 40, "candidate_pr_manifest_mismatch"),
        (
            "CLOSED",
            "3" * 40,
            "candidate_closed_requires_operator_reconciliation",
        ),
    ),
)
def test_published_candidate_head_move_or_close_blocks_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_state: str,
    candidate_head: str,
    expected_blocker: str,
) -> None:
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id=TEST_CANDIDATE_ID,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.SOURCE_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch=TEST_CANDIDATE_BRANCH,
        head_sha="3" * 40,
        base_sha="1" * 40,
        upstream_sha="2" * 40,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    published = routine.publish_candidate_manifest(prepared, pr_number=178)
    monkeypatch.setenv(routine.EXECUTE_ENV, "1")
    monkeypatch.setattr(routine, "WORKTREE_ROOT", tmp_path / "worktrees")
    monkeypatch.setattr(
        routine,
        "recover_candidate_manifest",
        lambda _path: published,
    )
    monkeypatch.setattr(
        routine.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": routine.MIN_FREE_BYTES + 1})(),
    )
    monkeypatch.setattr(routine, "safe_rmtree", lambda _path: None)
    monkeypatch.setattr(routine, "clone_refs", lambda _repo, _branch=None: None)
    monkeypatch.setattr(
        routine,
        "rev_parse",
        lambda _repo, ref: {
            f"origin/{routine.SOURCE_BRANCH}": "1" * 40,
            f"upstream/{routine.UPSTREAM_BRANCH}": "2" * 40,
            f"origin/{TEST_CANDIDATE_BRANCH}": candidate_head,
        }[ref],
    )
    monkeypatch.setattr(routine, "ahead_behind", lambda *_args: (0, 1))
    monkeypatch.setattr(routine, "ref_exists", lambda *_args: True)
    monkeypatch.setattr(
        routine,
        "candidate_pr_view",
        lambda *_args: {
            **TEST_REPOSITORY_IDENTITY,
            "number": 178,
            "url": "https://github.com/lomliev/hermes-agent/pull/178",
            "state": candidate_state,
            "headRefName": TEST_CANDIDATE_BRANCH,
            "headRefOid": candidate_head,
            "baseRefName": routine.SOURCE_BRANCH,
        },
    )
    reports: list[dict] = []
    monkeypatch.setattr(
        routine,
        "write_report",
        lambda report: reports.append(dict(report)),
    )
    monkeypatch.setattr(
        routine,
        "push_candidate",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("blocked candidate attempted a push")
        ),
    )

    report = routine._execute_locked()

    assert report["status"] == "BLOCKED"
    assert report["blocker"] == expected_blocker
    assert reports[-1] == report


@pytest.mark.parametrize("merged_into_source", (False, True))
def test_merged_candidate_requires_exact_fetched_source_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    merged_into_source: bool,
) -> None:
    state = tmp_path / "state" / "candidate.json"
    prepared = routine.build_prepared_candidate_manifest(
        candidate_id=TEST_CANDIDATE_ID,
        fork_repository=routine.FORK_REPO,
        upstream_repository=routine.UPSTREAM_REPO,
        base_ref=routine.SOURCE_BRANCH,
        upstream_ref=routine.UPSTREAM_BRANCH,
        branch=TEST_CANDIDATE_BRANCH,
        head_sha="3" * 40,
        base_sha="1" * 40,
        upstream_sha="2" * 40,
        created_at_utc="2026-07-30T09:00:00Z",
    )
    published = routine.publish_candidate_manifest(prepared, pr_number=178)
    routine.append_candidate_manifest(state, published)
    monkeypatch.setenv(routine.EXECUTE_ENV, "1")
    monkeypatch.setattr(routine, "AUTO_STATE", state)
    monkeypatch.setattr(routine, "WORKTREE_ROOT", tmp_path / "worktrees")
    monkeypatch.setattr(
        routine.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": routine.MIN_FREE_BYTES + 1})(),
    )
    monkeypatch.setattr(routine, "safe_rmtree", lambda _path: None)
    monkeypatch.setattr(routine, "clone_refs", lambda _repo, _branch=None: None)
    monkeypatch.setattr(
        routine,
        "rev_parse",
        lambda _repo, ref: {
            f"origin/{routine.SOURCE_BRANCH}": "5" * 40,
            f"upstream/{routine.UPSTREAM_BRANCH}": "2" * 40,
            f"origin/{TEST_CANDIDATE_BRANCH}": "3" * 40,
        }[ref],
    )
    monkeypatch.setattr(routine, "ahead_behind", lambda *_args: (0, 0))
    # GitHub may delete the head branch immediately after merge. The exact PR
    # head plus fetched source ancestry remain the terminal proof.
    monkeypatch.setattr(routine, "ref_exists", lambda *_args: False)
    monkeypatch.setattr(
        routine,
        "candidate_pr_view",
        lambda *_args: {
            **TEST_REPOSITORY_IDENTITY,
            "number": 178,
            "url": "https://github.com/lomliev/hermes-agent/pull/178",
            "state": "MERGED",
            "headRefName": TEST_CANDIDATE_BRANCH,
            "headRefOid": "3" * 40,
            "baseRefName": routine.SOURCE_BRANCH,
        },
    )
    monkeypatch.setattr(
        routine,
        "is_ancestor",
        lambda *_args: merged_into_source,
    )
    monkeypatch.setattr(routine, "write_report", lambda _report: None)

    report = routine.execute()

    if merged_into_source:
        assert report["outcome"] == "candidate_merged_reconciled"
        assert routine.recover_candidate_manifest(state) is None
    else:
        assert report["blocker"] == "candidate_merged_without_source_proof"
        assert routine.recover_candidate_manifest(state) == published


def test_github_ci_failure_is_a_stable_blocker(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        routine,
        "gh_json",
        lambda _args, cwd: {
            **TEST_REPOSITORY_IDENTITY,
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "baseRefName": routine.SOURCE_BRANCH,
            "headRefName": TEST_CANDIDATE_BRANCH,
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
        TEST_CANDIDATE_BRANCH,
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
            **TEST_REPOSITORY_IDENTITY,
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "baseRefName": routine.SOURCE_BRANCH,
            "headRefName": TEST_CANDIDATE_BRANCH,
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
        TEST_CANDIDATE_BRANCH,
    )

    assert result["status"] == "PARTIAL"
    assert result["outcome"] == "candidate_ci_pending"
    assert result["check"]["passed"] is None


@pytest.mark.parametrize(
    "rollup",
    [
        [{"status": "completed", "conclusion": "SUCCESS"}],
        [{"status": "COMPLETED", "conclusion": "success"}],
        [{"status": 1, "conclusion": "SUCCESS"}],
        [{"status": [], "conclusion": "SUCCESS"}],
        [{"status": "COMPLETED", "conclusion": []}],
        [{"status": "COMPLETED", "conclusion": "UNKNOWN"}],
        [{"state": "success"}],
        [{"status": "COMPLETED", "state": "SUCCESS", "conclusion": "SUCCESS"}],
        [{}],
        ["COMPLETED"],
    ],
)
def test_github_ci_protocol_lookalikes_fail_closed(
    monkeypatch,
    tmp_path: Path,
    rollup,
) -> None:
    monkeypatch.setattr(
        routine,
        "gh_json",
        lambda _args, cwd: {
            **TEST_REPOSITORY_IDENTITY,
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "baseRefName": routine.SOURCE_BRANCH,
            "headRefName": TEST_CANDIDATE_BRANCH,
            "statusCheckRollup": rollup,
        },
    )

    result = routine.candidate_ci_status(
        tmp_path,
        "https://github.com/lomliev/hermes-agent/pull/178",
        "a" * 40,
        TEST_CANDIDATE_BRANCH,
    )

    assert result["status"] == "BLOCKED"
    assert result["outcome"] == "candidate_ci_protocol_invalid"
    assert result["blocker"] == "github_ci_protocol_invalid"
    assert result["check"]["invalid"] == 1


def test_status_context_uses_exact_structured_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        routine,
        "gh_json",
        lambda _args, cwd: {
            **TEST_REPOSITORY_IDENTITY,
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "baseRefName": routine.SOURCE_BRANCH,
            "headRefName": TEST_CANDIDATE_BRANCH,
            "statusCheckRollup": [{"state": "SUCCESS"}],
        },
    )

    result = routine.candidate_ci_status(
        tmp_path,
        "https://github.com/lomliev/hermes-agent/pull/178",
        "a" * 40,
        TEST_CANDIDATE_BRANCH,
    )

    assert result["status"] == "PASS"
    assert result["check"]["invalid"] == 0


def test_candidate_pr_url_is_exact_without_trailing_slash(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(routine, "gh_json", lambda _args, cwd: {})

    with pytest.raises(routine.SkyAISyncBlocked) as raised:
        routine.candidate_ci_status(
            tmp_path,
            "https://github.com/lomliev/hermes-agent/pull/178/",
            "a" * 40,
            TEST_CANDIDATE_BRANCH,
        )

    assert raised.value.code == "candidate_pr_url_invalid"


def test_redact_uses_only_exact_registered_secret_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "github_pat_" + "a" * 40
    unregistered_lookalike = "ghp_" + "b" * 40
    monkeypatch.setenv("GH_TOKEN", secret)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    value = f"first={secret} second={unregistered_lookalike}"

    assert (
        routine.redact(value)
        == f"first=[REDACTED] second={unregistered_lookalike}"
    )


def test_redact_preserves_unregistered_secret_lookalikes_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    value = f"github_pat_{'a' * 40}\nghp_{'b' * 40}\nGhO_{'c' * 40}"

    assert routine.redact(value) == value


def test_run_redacts_exact_secret_from_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "github_pat_" + "x" * 40
    unregistered_lookalike = "ghp_" + "y" * 40

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout=f"{secret} {unregistered_lookalike}",
            stderr="",
        )

    monkeypatch.setattr(routine.subprocess, "run", fake_run)

    result = routine.run(
        ("git", "status"),
        environment={"GH_TOKEN": secret},
    )

    assert result.stdout == f"[REDACTED] {unregistered_lookalike}"


def test_module_compiles_in_isolated_stdlib() -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(MODULE), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
