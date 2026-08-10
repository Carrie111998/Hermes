import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli.runner import WorkspaceRunner
from hermes_cli.runner_protocol import RunnerCommand
from hermes_cli.runner_spool import RunnerSpool


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def git_ref_exists(cwd: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", ref], cwd=cwd, check=False
    ).returncode == 0


def command(binding_id, lease, method, params, command_id):
    return RunnerCommand.create(
        attempt_id="attempt-git",
        binding_id=binding_id,
        command_id=command_id,
        fencing_token=lease.fencing_token,
        lease_id=lease.lease_id,
        method=method,
        params=params,
        run_id="run-git",
    )


def setup_git_runner(tmp_path: Path):
    root = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    root.mkdir()
    remote.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "file.txt").write_text("initial\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "initial")
    git(root, "branch", "-M", "main")
    git(remote, "init", "--bare", "-q")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-qu", "origin", "main")

    spool = RunnerSpool(tmp_path / "runner.db")
    runner = WorkspaceRunner(spool, trusted_executables={sys.executable})
    parent = runner.register_binding(project_id="project-1", root_path=root, label="Repo")
    parent_lease = runner.acquire_lease(
        binding_id=parent.binding_id,
        owner="run-git",
        ttl_seconds=300,
        expected_head=git(root, "rev-parse", "HEAD"),
    )
    return root, remote, runner, spool, parent, parent_lease


def test_runner_worktree_commit_and_digest_approved_push(tmp_path, monkeypatch):
    root, remote, runner, spool, parent, parent_lease = setup_git_runner(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "must-not-reach-runner-git")
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\nprintf '%s' \"$GH_TOKEN\" > \"$(git rev-parse --show-toplevel)/hook-leak\"\n"
    )
    hook.chmod(0o755)
    worktree_result = runner.execute(
        command(
            parent.binding_id,
            parent_lease,
            "git.worktree.add",
            {"branch": "hermes/task-one", "name": "task-one"},
            "worktree-add",
        )
    )["result"]

    child = worktree_result["binding"]
    assert "root_path" not in child
    worktree = spool.resolve_binding(child["binding_id"])
    child_lease = runner.acquire_lease(
        binding_id=child["binding_id"],
        owner="run-git-child",
        ttl_seconds=300,
        expected_head=git(worktree, "rev-parse", "HEAD"),
    )
    (worktree / "file.txt").write_text("changed\n")

    commit_result = runner.execute(
        command(
            child["binding_id"],
            child_lease,
            "git.commit",
            {
                "checks": [{"argv": [sys.executable, "-c", "print('checks passed')"], "cwd": "."}],
                "message": "feat: runner commit",
            },
            "commit-1",
        )
    )["result"]
    assert commit_result["commit_sha"] == git(worktree, "rev-parse", "HEAD")
    assert commit_result["checks"][0]["exit_code"] == 0
    assert not (worktree / "hook-leak").exists()

    request = runner.execute(
        command(
            child["binding_id"],
            child_lease,
            "git.push.request",
            {},
            "push-request",
        )
    )["result"]
    assert request["commitSha"] == commit_result["commit_sha"]
    assert len(request["changeSetDigest"]) == 64

    assert not git_ref_exists(remote, "refs/heads/hermes/task-one")
    with pytest.raises(ValueError, match="runner method"):
        command(
            child["binding_id"],
            child_lease,
            "git.push.approved",
            {
                "decision": {
                    **request,
                    "approved": True,
                    "approvedBy": "user-1",
                }
            },
            "push-approved",
        )

    with pytest.raises(ValueError, match="active lease"):
        runner.execute(
            command(
                parent.binding_id,
                parent_lease,
                "git.worktree.remove",
                {"binding_id": child["binding_id"]},
                "remove-active",
            )
        )
    assert spool.release_lease(
        binding_id=child["binding_id"],
        lease_id=child_lease.lease_id,
        fencing_token=child_lease.fencing_token,
    ) is True
    removed = runner.execute(
        command(
            parent.binding_id,
            parent_lease,
            "git.worktree.remove",
            {"binding_id": child["binding_id"]},
            "remove-clean",
        )
    )["result"]
    assert removed == {"binding_id": child["binding_id"], "removed": True}
    assert not worktree.exists()
    with pytest.raises(ValueError, match="revoked"):
        spool.resolve_binding(child["binding_id"])


def test_runner_push_url_identity_rejects_ambiguous_or_credentialed_urls(tmp_path):
    _effective, display, _digest = WorkspaceRunner._push_url_identity(
        tmp_path,
        "git@github.com:owner/repo.git",
    )
    assert display == "ssh://github.com/owner/repo.git"
    _effective, display, _digest = WorkspaceRunner._push_url_identity(
        tmp_path,
        "ssh://git@github.com/owner/repo.git",
    )
    assert display == "ssh://github.com/owner/repo.git"
    with pytest.raises(ValueError, match="scheme"):
        WorkspaceRunner._push_url_identity(tmp_path, "ftp://example.com/repo.git")
    with pytest.raises(ValueError, match="credentials"):
        WorkspaceRunner._push_url_identity(
            tmp_path,
            "https://token@example.com/owner/repo.git",
        )


def test_failed_check_prevents_host_owned_commit(tmp_path):
    _root, _remote, runner, spool, parent, parent_lease = setup_git_runner(tmp_path)
    child = runner.execute(
        command(
            parent.binding_id,
            parent_lease,
            "git.worktree.add",
            {"branch": "hermes/failing", "name": "failing"},
            "worktree-failing",
        )
    )["result"]["binding"]
    worktree = spool.resolve_binding(child["binding_id"])
    original_head = git(worktree, "rev-parse", "HEAD")
    lease = runner.acquire_lease(
        binding_id=child["binding_id"],
        owner="run-failing",
        ttl_seconds=300,
        expected_head=original_head,
    )
    (worktree / "file.txt").write_text("bad\n")

    with pytest.raises(ValueError, match="check"):
        runner.execute(
            command(
                child["binding_id"],
                lease,
                "git.commit",
                {
                    "checks": [{"argv": [sys.executable, "-c", "raise SystemExit(2)"], "cwd": "."}],
                    "message": "should not commit",
                },
                "commit-failing",
            )
        )

    assert git(worktree, "rev-parse", "HEAD") == original_head


def test_runner_never_executes_approved_push_even_after_remote_substitution(tmp_path):
    _root, approved_remote, runner, spool, parent, parent_lease = setup_git_runner(tmp_path)
    child = runner.execute(
        command(
            parent.binding_id,
            parent_lease,
            "git.worktree.add",
            {"branch": "hermes/substitute", "name": "substitute"},
            "worktree-substitute",
        )
    )["result"]["binding"]
    worktree = spool.resolve_binding(child["binding_id"])
    lease = runner.acquire_lease(
        binding_id=child["binding_id"],
        owner="run-substitute",
        ttl_seconds=300,
        expected_head=git(worktree, "rev-parse", "HEAD"),
    )
    (worktree / "file.txt").write_text("changed\n")
    runner.execute(
        command(
            child["binding_id"],
            lease,
            "git.commit",
            {
                "checks": [{"argv": [sys.executable, "-c", "print('ok')"], "cwd": "."}],
                "message": "feat: destination binding",
            },
            "commit-substitute",
        )
    )
    request = runner.execute(
        command(child["binding_id"], lease, "git.push.request", {}, "request-substitute")
    )["result"]
    substituted_remote = tmp_path / "substituted.git"
    substituted_remote.mkdir()
    git(substituted_remote, "init", "--bare", "-q")
    git(worktree, "remote", "set-url", "--push", "origin", str(substituted_remote))

    with pytest.raises(ValueError, match="runner method"):
        runner.execute(
            command(
                child["binding_id"],
                lease,
                "git.push.approved",
                {
                    "decision": {
                        **request,
                        "approved": True,
                        "approvedBy": "user-1",
                        "decidedAt": "2026-08-06T00:00:00.000Z",
                    }
                },
                "approve-substitute",
            )
        )

    assert not git_ref_exists(approved_remote, "refs/heads/hermes/substitute")
    assert not git_ref_exists(substituted_remote, "refs/heads/hermes/substitute")
