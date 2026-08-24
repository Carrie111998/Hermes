"""Focused Windows-installer transaction safety tests."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
PWSH = shutil.which("pwsh") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(
    PWSH is None or shutil.which("git") is None,
    reason="needs git and PowerShell",
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _pwsh(command: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    assert PWSH is not None
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        env=(os.environ | env) if env else None,
        capture_output=True,
        text=True,
        check=False,
    )


def _seed_repo(tmp_path: Path) -> tuple[Path, list[str]]:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    commits: list[str] = []
    for revision in range(3):
        (origin / "tracked.txt").write_text(f"revision {revision}\n", encoding="utf-8")
        _git(origin, "add", "tracked.txt")
        _git(origin, "commit", "-qm", f"revision {revision}")
        commits.append(_git(origin, "rev-parse", "HEAD"))
    return origin, commits


def _run_managed_update(
    repo: Path,
    home: Path,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return _pwsh(
        f". '{INSTALL_PS1}' -HermesHome '{home}' -InstallDir '{repo}'; "
        f"Update-ManagedCheckout -Repo '{repo}' -Branch main",
        env=env,
    )


def _git_wrapper(tmp_path: Path, body: str) -> tuple[Path, str]:
    """Install a POSIX git wrapper and return its bin dir and real git path."""
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\nset -u\nREAL_GIT=$HERMES_REAL_GIT\n" + body,
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return bin_dir, real_git


def _wrapper_env(bin_dir: Path, real_git: str, **values: Path) -> dict[str, str]:
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HERMES_REAL_GIT": real_git,
        **{name: str(value) for name, value in values.items()},
    }


def test_shared_marker_claim_is_complete_and_owner_checked(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    command = (
        f". '{INSTALL_PS1}' -HermesHome '{home}' -InstallDir '{repo}'; "
        "$lock = Enter-InstallerUpdateLock; "
        "$body = [IO.File]::ReadAllText($lock.MarkerPath); "
        "if ($body -notmatch ('^' + $PID + '\\n[0-9]+\\n$')) { exit 7 }; "
        "Exit-InstallerUpdateLock -Lock $lock; "
        "if (Test-Path -LiteralPath $lock.MarkerPath) { exit 8 }"
    )

    result = _pwsh(command)

    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.live_system_guard_bypass
def test_live_foreign_marker_is_refused_without_mutation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    marker = home / ".hermes-update-in-progress"
    holder = subprocess.Popen(["sleep", "30"])
    try:
        body = f"{holder.pid}\n1700000000\n"
        marker.write_text(body, encoding="ascii")
        repo = tmp_path / "repo"
        command = (
            f". '{INSTALL_PS1}' -HermesHome '{home}' -InstallDir '{repo}'; "
            "try { $null = Enter-InstallerUpdateLock; exit 9 } "
            "catch { if (-not $script:InstallerLockContended) { exit 10 }; exit 2 }"
        )

        result = _pwsh(command)

        assert result.returncode == 2, result.stderr + result.stdout
        assert marker.read_text(encoding="ascii") == body
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_commit_pin_uses_exact_private_ref_and_blocks_shallow_downgrade(
    tmp_path: Path,
) -> None:
    origin, commits = _seed_repo(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{origin}", str(clone))
    head_before = _git(clone, "rev-parse", "HEAD")
    home = tmp_path / "home"
    command = (
        f". '{INSTALL_PS1}' -HermesHome '{home}' -InstallDir '{clone}'; "
        f"Set-ManagedPinnedCheckout -Repo '{clone}' -Branch main -Commit '{commits[0]}'"
    )

    result = _pwsh(command)

    assert result.returncode == 0, result.stderr + result.stdout
    assert _git(clone, "rev-parse", "HEAD") == head_before
    assert "already newer" in result.stdout
    assert _git(clone, "for-each-ref", "--format=%(refname)", "refs/hermes-update-fetches/") == ""


def test_forced_commit_pin_detaches_exact_requested_sha(tmp_path: Path) -> None:
    origin, commits = _seed_repo(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    home = tmp_path / "home"
    command = (
        f". '{INSTALL_PS1}' -HermesHome '{home}' -InstallDir '{clone}'; "
        f"Set-ManagedPinnedCheckout -Repo '{clone}' -Branch main "
        f"-Commit '{commits[0]}' -ForceCommit"
    )

    result = _pwsh(command)

    assert result.returncode == 0, result.stderr + result.stdout
    assert _git(clone, "rev-parse", "HEAD") == commits[0]
    assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


def test_annotated_tag_is_peeled_then_checked_out_by_exact_commit(tmp_path: Path) -> None:
    origin, commits = _seed_repo(tmp_path)
    _git(origin, "tag", "-a", "release/test", commits[1], "-m", "release")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    home = tmp_path / "home"
    command = (
        f". '{INSTALL_PS1}' -HermesHome '{home}' -InstallDir '{clone}'; "
        f"Set-ManagedPinnedCheckout -Repo '{clone}' -Branch main -Tag 'release/test'"
    )

    result = _pwsh(command)

    assert result.returncode == 0, result.stderr + result.stdout
    assert _git(clone, "rev-parse", "HEAD") == commits[1]
    assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


def test_diverged_branch_merges_from_detached_inputs_then_cas_attaches(
    tmp_path: Path,
) -> None:
    origin, _ = _seed_repo(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.name", "Test")
    _git(clone, "config", "user.email", "t@example.com")
    (clone / "local.txt").write_text("local\n", encoding="utf-8")
    _git(clone, "add", "local.txt")
    _git(clone, "commit", "-qm", "local")
    local = _git(clone, "rev-parse", "HEAD")
    (origin / "remote.txt").write_text("remote\n", encoding="utf-8")
    _git(origin, "add", "remote.txt")
    _git(origin, "commit", "-qm", "remote")
    remote = _git(origin, "rev-parse", "HEAD")

    result = _run_managed_update(clone, tmp_path / "home")

    assert result.returncode == 0, result.stderr + result.stdout
    merged = _git(clone, "rev-parse", "HEAD")
    _git(clone, "merge-base", "--is-ancestor", local, merged)
    _git(clone, "merge-base", "--is-ancestor", remote, merged)
    assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX git race wrapper")
def test_first_checkout_boundary_refuses_and_pins_concurrent_detached_head(
    tmp_path: Path,
) -> None:
    origin, _ = _seed_repo(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.name", "Test")
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "switch", "-qc", "parked")
    original = _git(clone, "rev-parse", "HEAD")
    main_before = _git(clone, "rev-parse", "main")
    (origin / "remote.txt").write_text("remote\n", encoding="utf-8")
    _git(origin, "add", "remote.txt")
    _git(origin, "commit", "-qm", "remote")

    raced_sha = tmp_path / "raced-sha"
    bin_dir, real_git = _git_wrapper(
        tmp_path,
        r'''
"$REAL_GIT" "$@"
rc=$?
case " $* " in
  *" show-ref --verify --hash refs/heads/main "*)
    if [ ! -e "$HERMES_RACE_SHA" ]; then
      tree=$("$REAL_GIT" -C "$HERMES_RACE_REPO" rev-parse 'HEAD^{tree}') || exit 91
      sha=$(printf '%s\n' concurrent-detached | "$REAL_GIT" -C "$HERMES_RACE_REPO" commit-tree "$tree" -p HEAD) || exit 92
      "$REAL_GIT" -C "$HERMES_RACE_REPO" checkout -q --detach "$sha" || exit 93
      printf '%s\n' "$sha" > "$HERMES_RACE_SHA"
    fi
    ;;
esac
exit "$rc"
''',
    )
    env = _wrapper_env(
        bin_dir,
        real_git,
        HERMES_RACE_REPO=clone,
        HERMES_RACE_SHA=raced_sha,
    )

    result = _run_managed_update(clone, tmp_path / "home", env=env)

    raced = raced_sha.read_text(encoding="ascii").strip()
    assert result.returncode != 0, result.stderr + result.stdout
    assert "first mutation boundary" in (result.stderr + result.stdout).lower()
    # Restoration may safely reattach the original checkout only after the
    # concurrent detached commit has acquired a durable recovery ref.
    assert _git(clone, "rev-parse", "HEAD") == original
    assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "parked"
    assert _git(clone, "rev-parse", "main") == main_before == original
    protected = _git(
        clone,
        "for-each-ref",
        "--format=%(objectname)",
        "refs/hermes-update-backups/",
    ).splitlines()
    assert raced in protected
    assert _git(clone, "for-each-ref", "--format=%(refname)", "refs/hermes-update-fetches/") == ""


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX git race wrapper")
def test_merge_rejects_hook_added_descendant_and_pins_exact_result(
    tmp_path: Path,
) -> None:
    origin, _ = _seed_repo(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.name", "Test")
    _git(clone, "config", "user.email", "t@example.com")
    (clone / "local.txt").write_text("local\n", encoding="utf-8")
    _git(clone, "add", "local.txt")
    _git(clone, "commit", "-qm", "local")
    local = _git(clone, "rev-parse", "HEAD")
    (origin / "remote.txt").write_text("remote\n", encoding="utf-8")
    _git(origin, "add", "remote.txt")
    _git(origin, "commit", "-qm", "remote")

    injected_sha = tmp_path / "injected-sha"
    bin_dir, real_git = _git_wrapper(
        tmp_path,
        r'''
case " $* " in
  *" merge --no-edit "*)
    "$REAL_GIT" "$@"
    rc=$?
    if [ "$rc" -eq 0 ]; then
      "$REAL_GIT" -C "$HERMES_RACE_REPO" commit -q --allow-empty -m injected-after-merge || exit 94
      "$REAL_GIT" -C "$HERMES_RACE_REPO" rev-parse HEAD > "$HERMES_RACE_SHA" || exit 95
    fi
    exit "$rc"
    ;;
esac
exec "$REAL_GIT" "$@"
''',
    )
    env = _wrapper_env(
        bin_dir,
        real_git,
        HERMES_RACE_REPO=clone,
        HERMES_RACE_SHA=injected_sha,
    )

    result = _run_managed_update(clone, tmp_path / "home", env=env)

    injected = injected_sha.read_text(encoding="ascii").strip()
    assert result.returncode != 0, result.stderr + result.stdout
    normalized_output = " ".join((result.stderr + result.stdout).split())
    assert "retained at" in normalized_output
    assert _git(clone, "rev-parse", "main") == local
    protected = _git(
        clone,
        "for-each-ref",
        "--format=%(objectname)",
        "refs/hermes-update-backups/",
    ).splitlines()
    assert injected in protected
    assert _git(clone, "for-each-ref", "--format=%(refname)", "refs/hermes-update-fetches/") == ""


def test_successful_dirty_update_restores_index_and_removes_hidden_stash_ref(
    tmp_path: Path,
) -> None:
    origin, _ = _seed_repo(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    (clone / "tracked.txt").write_text("staged local\n", encoding="utf-8")
    _git(clone, "add", "tracked.txt")
    with (clone / "tracked.txt").open("a", encoding="utf-8") as stream:
        stream.write("unstaged local\n")
    (clone / "untracked.txt").write_text("untracked local\n", encoding="utf-8")
    status_before = _git(clone, "status", "--porcelain=v1")
    cached_before = _git(clone, "diff", "--cached", "--", "tracked.txt")
    (origin / "remote.txt").write_text("remote\n", encoding="utf-8")
    _git(origin, "add", "remote.txt")
    _git(origin, "commit", "-qm", "remote")

    result = _run_managed_update(clone, tmp_path / "home")

    assert result.returncode == 0, result.stderr + result.stdout
    assert _git(clone, "status", "--porcelain=v1") == status_before
    assert _git(clone, "diff", "--cached", "--", "tracked.txt") == cached_before
    assert (clone / "tracked.txt").read_text(encoding="utf-8") == "staged local\nunstaged local\n"
    assert (clone / "untracked.txt").read_text(encoding="utf-8") == "untracked local\n"
    assert "hermes-install-autostash-" in _git(clone, "stash", "list")
    assert _git(clone, "for-each-ref", "--format=%(refname)", "refs/hermes-update-stashes/") == ""


def test_conflicted_stash_restore_retains_partial_state_and_exact_recovery_ref(
    tmp_path: Path,
) -> None:
    origin, _ = _seed_repo(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    (clone / "tracked.txt").write_text("local dirty\n", encoding="utf-8")
    (origin / "tracked.txt").write_text("remote update\n", encoding="utf-8")
    _git(origin, "add", "tracked.txt")
    _git(origin, "commit", "-qm", "conflicting remote")

    result = _run_managed_update(clone, tmp_path / "home")

    output = result.stderr + result.stdout
    assert result.returncode != 0, output
    assert "Exact ref cleanup after recovery: git update-ref -d" in output
    assert _git(clone, "diff", "--name-only", "--diff-filter=U") == "tracked.txt"
    stash_sha = _git(clone, "stash", "list", "--format=%H").splitlines()[0]
    hidden = _git(
        clone,
        "for-each-ref",
        "--format=%(objectname)",
        "refs/hermes-update-stashes/",
    ).splitlines()
    assert hidden == [stash_sha]


def test_absent_tracking_ref_succeeds_twice_and_leaves_no_private_fetch_ref(
    tmp_path: Path,
) -> None:
    origin, _ = _seed_repo(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "update-ref", "-d", "refs/remotes/origin/main")
    (origin / "remote-1.txt").write_text("one\n", encoding="utf-8")
    _git(origin, "add", "remote-1.txt")
    _git(origin, "commit", "-qm", "remote one")
    first_tip = _git(origin, "rev-parse", "HEAD")

    first = _run_managed_update(clone, tmp_path / "home")

    assert first.returncode == 0, first.stderr + first.stdout
    assert _git(clone, "rev-parse", "HEAD") == first_tip
    assert _git(clone, "rev-parse", "refs/remotes/origin/main") == first_tip
    assert _git(clone, "for-each-ref", "--format=%(refname)", "refs/hermes-update-fetches/") == ""

    (origin / "remote-2.txt").write_text("two\n", encoding="utf-8")
    _git(origin, "add", "remote-2.txt")
    _git(origin, "commit", "-qm", "remote two")
    second_tip = _git(origin, "rev-parse", "HEAD")
    second = _run_managed_update(clone, tmp_path / "home")

    assert second.returncode == 0, second.stderr + second.stdout
    assert _git(clone, "rev-parse", "HEAD") == second_tip
    assert _git(clone, "rev-parse", "refs/remotes/origin/main") == second_tip
    assert _git(clone, "for-each-ref", "--format=%(refname)", "refs/hermes-update-fetches/") == ""


def test_absent_tracking_and_local_target_create_safely(tmp_path: Path) -> None:
    origin, _ = _seed_repo(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "switch", "-qc", "parked")
    parked = _git(clone, "rev-parse", "HEAD")
    _git(clone, "branch", "-D", "main")
    _git(clone, "update-ref", "-d", "refs/remotes/origin/main")
    (origin / "remote.txt").write_text("remote\n", encoding="utf-8")
    _git(origin, "add", "remote.txt")
    _git(origin, "commit", "-qm", "remote")
    remote = _git(origin, "rev-parse", "HEAD")

    result = _run_managed_update(clone, tmp_path / "home")

    assert result.returncode == 0, result.stderr + result.stdout
    assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(clone, "rev-parse", "HEAD") == remote
    assert _git(clone, "rev-parse", "parked") == parked
    assert _git(clone, "rev-parse", "refs/remotes/origin/main") == remote
    assert _git(clone, "for-each-ref", "--format=%(refname)", "refs/hermes-update-fetches/") == ""


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX git failure wrapper")
def test_failed_update_exactly_removes_transaction_created_tracking_ref(
    tmp_path: Path,
) -> None:
    origin, _ = _seed_repo(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "update-ref", "-d", "refs/remotes/origin/main")
    (origin / "remote.txt").write_text("remote\n", encoding="utf-8")
    _git(origin, "add", "remote.txt")
    _git(origin, "commit", "-qm", "remote")
    bin_dir, real_git = _git_wrapper(
        tmp_path,
        r'''
case " $* " in
  *" status --porcelain --untracked-files=all "*) exit 2 ;;
esac
exec "$REAL_GIT" "$@"
''',
    )
    env = _wrapper_env(bin_dir, real_git)

    result = _run_managed_update(clone, tmp_path / "home", env=env)

    assert result.returncode != 0, result.stderr + result.stdout
    verify_tracking = subprocess.run(
        ["git", "-C", str(clone), "show-ref", "--verify", "--quiet", "refs/remotes/origin/main"],
        check=False,
    )
    assert verify_tracking.returncode == 1
    assert _git(clone, "for-each-ref", "--format=%(refname)", "refs/hermes-update-fetches/") == ""
