"""End-to-end Git policy tests for managed-checkout installer updates."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
REAL_GIT = shutil.which("git")


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    assert REAL_GIT is not None
    return subprocess.run(
        [REAL_GIT, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _commit(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _snapshot(repo: Path) -> tuple[str, bytes, bytes, bytes, dict[str, bytes], str]:
    files = {
        str(path.relative_to(repo)): path.read_bytes()
        for path in sorted(repo.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }
    return (
        _head(repo),
        _git(repo, "status", "--porcelain=v1", "-z").stdout.encode(),
        _git(repo, "diff", "--binary").stdout.encode(),
        _git(repo, "diff", "--cached", "--binary").stdout.encode(),
        files,
        _git(repo, "stash", "list", "--format=%H%x09%gs").stdout,
    )


@dataclass
class Repositories:
    origin: Path
    clone: Path
    old_tip: str


@pytest.fixture()
def repos(tmp_path: Path) -> Repositories:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "test@example.com")
    _git(origin, "config", "user.name", "Test")
    for filename, content in {
        "base.txt": "base\n",
        "conflict.txt": "base\n",
        "staged.txt": "base\n",
        "unstaged.txt": "base\n",
    }.items():
        (origin / filename).write_text(content, encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "commit", "-qm", "base")
    old_tip = _head(origin)

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    return Repositories(origin, clone, old_tip)


def _run_bash_update(
    repo: Path,
    *,
    branch: str = "main",
    path_prefix: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            "bash",
            "-c",
            'installer=$1; repo=$2; branch=$3; set --; source "$installer"; '
            'update_managed_checkout "$repo" "$branch"',
            "hermes-install-test",
            str(INSTALL_SH),
            str(repo),
            branch,
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _rewrite_origin(repos: Repositories) -> str:
    (repos.origin / "base.txt").write_text("rewritten\n", encoding="utf-8")
    _git(repos.origin, "add", "base.txt")
    _git(repos.origin, "commit", "--amend", "-qm", "rewritten base")
    return _head(repos.origin)


def test_install_sh_fast_forwards_no_local_commit(repos: Repositories) -> None:
    expected = _commit(repos.origin, "remote.txt", "remote\n", "remote advance")

    result = _run_bash_update(repos.clone)

    assert result.returncode == 0, result.stderr + result.stdout
    assert _head(repos.clone) == expected


def test_install_sh_merges_normal_advance_with_local_commits(
    repos: Repositories,
) -> None:
    local = _commit(repos.clone, "local.txt", "local\n", "local commit")
    remote = _commit(repos.origin, "remote.txt", "remote\n", "remote advance")

    result = _run_bash_update(repos.clone)

    assert result.returncode == 0, result.stderr + result.stdout
    head = _head(repos.clone)
    assert _git(repos.clone, "merge-base", "--is-ancestor", local, head).returncode == 0
    assert _git(repos.clone, "merge-base", "--is-ancestor", remote, head).returncode == 0
    recovery_refs = _git(
        repos.clone,
        "for-each-ref",
        "--format=%(refname)",
        "refs/hermes-update-backups/",
    ).stdout.splitlines()
    assert len(recovery_refs) == 1
    assert _git(repos.clone, "rev-parse", recovery_refs[0]).stdout.strip() == local


def test_install_sh_adopts_confirmed_rewrite_without_local_commits(
    repos: Repositories,
) -> None:
    rewritten = _rewrite_origin(repos)

    result = _run_bash_update(repos.clone)

    assert result.returncode == 0, result.stderr + result.stdout
    assert _head(repos.clone) == rewritten


def test_install_sh_adopts_confirmed_remote_rewind_to_ancestor(
    repos: Repositories,
) -> None:
    previous_remote = _commit(
        repos.origin, "later.txt", "later\n", "temporary remote advance"
    )
    _git(repos.clone, "fetch", "-q", "origin", "main")
    _git(repos.clone, "reset", "--hard", previous_remote)
    _git(repos.origin, "reset", "--hard", repos.old_tip)

    result = _run_bash_update(repos.clone)

    assert result.returncode == 0, result.stderr + result.stdout
    assert _head(repos.clone) == repos.old_tip


def test_install_sh_rewrite_with_local_commits_fails_closed(
    repos: Repositories,
) -> None:
    _commit(repos.clone, "local.txt", "local\n", "local commit")
    _rewrite_origin(repos)
    before = _snapshot(repos.clone)
    old_tracking = _git(
        repos.clone, "rev-parse", "refs/remotes/origin/main"
    ).stdout.strip()

    first = _run_bash_update(repos.clone)
    second = _run_bash_update(repos.clone)

    assert first.returncode != 0
    assert second.returncode != 0, "a failed first run must not weaken the rewrite guard"
    assert _snapshot(repos.clone) == before
    assert (
        _git(repos.clone, "rev-parse", "refs/remotes/origin/main").stdout.strip()
        == old_tracking
    )
    assert not _git(
        repos.clone,
        "for-each-ref",
        "--format=%(refname)",
        "refs/hermes-update-fetches/",
    ).stdout.strip()


def test_install_sh_missing_old_tip_fails_closed(repos: Repositories) -> None:
    _git(repos.clone, "update-ref", "-d", "refs/remotes/origin/main")
    before = _snapshot(repos.clone)

    result = _run_bash_update(repos.clone)

    assert result.returncode != 0
    assert _snapshot(repos.clone) == before


def test_install_sh_ancestry_error_fails_closed(
    repos: Repositories, tmp_path: Path
) -> None:
    _commit(repos.origin, "remote.txt", "remote\n", "remote advance")
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "merge-base" ]; then exit 2; fi\n'
        "done\n"
        f'exec "{REAL_GIT}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    before = _snapshot(repos.clone)

    result = _run_bash_update(repos.clone, path_prefix=wrapper_dir)

    assert result.returncode != 0
    assert _snapshot(repos.clone) == before


def test_install_sh_merge_conflict_restores_exact_dirty_state_and_other_stash(
    repos: Repositories,
) -> None:
    _commit(repos.clone, "conflict.txt", "local\n", "local conflict")
    _commit(repos.origin, "conflict.txt", "remote\n", "remote conflict")
    (repos.clone / "older-stash.txt").write_text("older\n", encoding="utf-8")
    _git(
        repos.clone,
        "stash",
        "push",
        "--include-untracked",
        "-m",
        "unrelated-preexisting-stash",
    )
    (repos.clone / "staged.txt").write_text("staged edit\n", encoding="utf-8")
    _git(repos.clone, "add", "staged.txt")
    (repos.clone / "unstaged.txt").write_text("unstaged edit\n", encoding="utf-8")
    (repos.clone / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    before = _snapshot(repos.clone)

    result = _run_bash_update(repos.clone)

    assert result.returncode != 0
    after = _snapshot(repos.clone)
    assert after[:5] == before[:5]
    assert "unrelated-preexisting-stash" in after[5]
    assert "hermes-install-autostash-" in after[5]
    assert _git(repos.clone, "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode != 0


def test_install_sh_uses_pinned_sha_when_remote_tracking_ref_moves(
    repos: Repositories, tmp_path: Path
) -> None:
    pinned = _commit(repos.origin, "pinned.txt", "pinned\n", "pinned advance")
    raced = _commit(repos.origin, "raced.txt", "raced\n", "later advance")
    _git(repos.clone, "fetch", "-q", "origin", "main")
    _git(repos.clone, "update-ref", "refs/remotes/origin/main", repos.old_tip)
    _git(repos.origin, "reset", "--hard", pinned)

    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'case " $* " in\n'
        '  *" switch --detach "*)\n'
        f'    "{REAL_GIT}" -C "$HERMES_RACE_REPO" update-ref '
        'refs/remotes/origin/main "$HERMES_RACE_SHA" || exit $?\n'
        "    ;;\n"
        "esac\n"
        f'exec "{REAL_GIT}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = _run_bash_update(
        repos.clone,
        path_prefix=wrapper_dir,
        extra_env={"HERMES_RACE_REPO": str(repos.clone), "HERMES_RACE_SHA": raced},
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert _head(repos.clone) == pinned
    assert _git(
        repos.clone, "rev-parse", "refs/remotes/origin/main"
    ).stdout.strip() == raced


def test_install_sh_refuses_absent_branch_created_during_preflight(
    repos: Repositories, tmp_path: Path
) -> None:
    _git(repos.origin, "branch", "feature", repos.old_tip)
    _git(repos.clone, "fetch", "-q", "origin", "feature:refs/remotes/origin/feature")
    _git(repos.origin, "checkout", "-q", "feature")
    _commit(repos.origin, "feature.txt", "remote feature\n", "feature advance")
    _git(repos.origin, "checkout", "-q", "main")
    old_tracking = _git(
        repos.clone, "rev-parse", "refs/remotes/origin/feature"
    ).stdout.strip()
    concurrent_tip = repos.old_tip

    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    raced_flag = tmp_path / "raced"
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'case " $* " in\n'
        '  *" status --porcelain --untracked-files=all "*)\n'
        '    if [ ! -e "$HERMES_RACE_FLAG" ]; then\n'
        '      : > "$HERMES_RACE_FLAG"\n'
        f'      "{REAL_GIT}" -C "$HERMES_RACE_REPO" update-ref '
        'refs/heads/feature "$HERMES_RACE_SHA" || exit $?\n'
        "    fi\n"
        "    ;;\n"
        "esac\n"
        f'exec "{REAL_GIT}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = _run_bash_update(
        repos.clone,
        branch="feature",
        path_prefix=wrapper_dir,
        extra_env={
            "HERMES_RACE_REPO": str(repos.clone),
            "HERMES_RACE_SHA": concurrent_tip,
            "HERMES_RACE_FLAG": str(raced_flag),
        },
    )

    assert result.returncode != 0
    assert _head(repos.clone) == repos.old_tip
    assert _git(repos.clone, "rev-parse", "refs/heads/feature").stdout.strip() == concurrent_tip
    assert (
        _git(repos.clone, "rev-parse", "refs/remotes/origin/feature").stdout.strip()
        == old_tracking
    )
    assert not _git(
        repos.clone,
        "for-each-ref",
        "--format=%(refname)",
        "refs/hermes-update-fetches/",
    ).stdout.strip()


def test_install_sh_preserves_detached_commit_under_durable_ref(
    repos: Repositories,
) -> None:
    _git(repos.clone, "switch", "--detach", repos.old_tip)
    detached = _commit(repos.clone, "detached.txt", "detached\n", "detached work")
    expected = _commit(repos.origin, "remote.txt", "remote\n", "remote advance")

    result = _run_bash_update(repos.clone)

    assert result.returncode == 0, result.stderr + result.stdout
    assert _head(repos.clone) == expected
    recovery_refs = _git(
        repos.clone,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/hermes-update-backups/",
    ).stdout.splitlines()
    assert any(line.endswith(f" {detached}") for line in recovery_refs)
    _git(repos.clone, "reflog", "expire", "--expire=now", "--all")
    _git(repos.clone, "gc", "--prune=now")
    assert _git(repos.clone, "cat-file", "-e", f"{detached}^{{commit}}").returncode == 0


def test_install_sh_restores_option_like_original_branch_on_failure(
    repos: Repositories, tmp_path: Path
) -> None:
    _git(repos.clone, "update-ref", "refs/heads/-parked", repos.old_tip)
    _git(repos.clone, "switch", "--", "-parked")
    new_tip = _commit(repos.origin, "remote.txt", "remote\n", "remote advance")
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'case " $* " in\n'
        f'  *" switch --detach {new_tip} "*) exit 9 ;;\n'
        "esac\n"
        f'exec "{REAL_GIT}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = _run_bash_update(repos.clone, path_prefix=wrapper_dir)

    assert result.returncode != 0
    assert _git(repos.clone, "branch", "--show-current").stdout.strip() == "-parked"
    assert _head(repos.clone) == repos.old_tip


def test_install_sh_rolls_back_failed_post_validation_with_exact_cas(
    repos: Repositories, tmp_path: Path
) -> None:
    new_tip = _commit(repos.origin, "remote.txt", "remote\n", "remote advance")
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'case " $* " in\n'
        f'  *" merge-base --is-ancestor {new_tip} {new_tip} "*) exit 2 ;;\n'
        "esac\n"
        f'exec "{REAL_GIT}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = _run_bash_update(repos.clone, path_prefix=wrapper_dir)

    assert result.returncode != 0
    assert "rolled back safely" in result.stdout
    assert _head(repos.clone) == repos.old_tip
    assert (
        _git(repos.clone, "rev-parse", "refs/remotes/origin/main").stdout.strip()
        == repos.old_tip
    )
    recovery_objects = _git(
        repos.clone,
        "for-each-ref",
        "--format=%(objectname)",
        "refs/hermes-update-backups/",
    ).stdout.splitlines()
    assert new_tip in recovery_objects


def test_install_sh_never_rolls_back_concurrent_commit_as_installer_owned(
    repos: Repositories, tmp_path: Path
) -> None:
    new_tip = _commit(repos.origin, "remote.txt", "remote\n", "remote advance")
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    raced_flag = tmp_path / "concurrent-created"
    concurrent_sha_file = tmp_path / "concurrent-sha"
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'case " $* " in\n'
        '  *" status --porcelain --untracked-files=no "*)\n'
        '    current=$("$REAL_GIT_BIN" -C "$HERMES_RACE_REPO" rev-parse HEAD) || exit $?\n'
        '    if [ "$current" = "$HERMES_INSTALLED_SHA" ] && [ ! -e "$HERMES_RACE_FLAG" ]; then\n'
        '      output=$("$REAL_GIT_BIN" "$@")\n'
        "      rc=$?\n"
        '      printf "concurrent\\n" > "$HERMES_RACE_REPO/concurrent.txt"\n'
        '      "$REAL_GIT_BIN" -C "$HERMES_RACE_REPO" add concurrent.txt || exit $?\n'
        '      "$REAL_GIT_BIN" -C "$HERMES_RACE_REPO" commit -qm "concurrent commit" || exit $?\n'
        '      "$REAL_GIT_BIN" -C "$HERMES_RACE_REPO" rev-parse HEAD > "$HERMES_RACE_SHA_FILE" || exit $?\n'
        '      : > "$HERMES_RACE_FLAG"\n'
        '      printf "%s" "$output"\n'
        '      exit "$rc"\n'
        "    fi\n"
        "    ;;\n"
        "esac\n"
        'exec "$REAL_GIT_BIN" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = _run_bash_update(
        repos.clone,
        path_prefix=wrapper_dir,
        extra_env={
            "REAL_GIT_BIN": str(REAL_GIT),
            "HERMES_RACE_REPO": str(repos.clone),
            "HERMES_INSTALLED_SHA": new_tip,
            "HERMES_RACE_FLAG": str(raced_flag),
            "HERMES_RACE_SHA_FILE": str(concurrent_sha_file),
        },
    )

    assert result.returncode != 0
    assert "automatic rollback was refused" in result.stdout
    concurrent_sha = concurrent_sha_file.read_text(encoding="utf-8").strip()
    assert _head(repos.clone) == concurrent_sha
    assert (repos.clone / "concurrent.txt").read_text(encoding="utf-8") == "concurrent\n"
    recovery_objects = _git(
        repos.clone,
        "for-each-ref",
        "--format=%(objectname)",
        "refs/hermes-update-backups/",
    ).stdout.splitlines()
    assert concurrent_sha not in recovery_objects
    assert (
        _git(repos.clone, "rev-parse", "refs/remotes/origin/main").stdout.strip()
        == new_tip
    )


def test_install_sh_pins_stash_even_when_git_reports_nonzero(
    repos: Repositories, tmp_path: Path
) -> None:
    (repos.clone / "unstaged.txt").write_text("local edit\n", encoding="utf-8")
    _commit(repos.origin, "remote.txt", "remote\n", "remote advance")
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'case " $* " in\n'
        '  *" stash push "*)\n'
        f'    "{REAL_GIT}" "$@"\n'
        "    rc=$?\n"
        '    [ "$rc" -eq 0 ] || exit "$rc"\n'
        "    exit 7\n"
        "    ;;\n"
        "esac\n"
        f'exec "{REAL_GIT}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = _run_bash_update(repos.clone, path_prefix=wrapper_dir)

    assert result.returncode != 0
    assert "partial operation is pinned" in result.stdout
    stash_sha = _git(repos.clone, "rev-parse", "refs/stash").stdout.strip()
    pins = _git(
        repos.clone,
        "for-each-ref",
        "--format=%(objectname)",
        "refs/hermes-update-stashes/",
    ).stdout.splitlines()
    assert stash_sha in pins
    assert _head(repos.clone) == repos.old_tip
    assert not _git(
        repos.clone,
        "for-each-ref",
        "--format=%(refname)",
        "refs/hermes-update-fetches/",
    ).stdout.strip()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell not installed")
def test_install_ps1_helper_executes_real_fast_forward(repos: Repositories) -> None:
    expected = _commit(repos.origin, "remote.txt", "remote\n", "remote advance")
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f". '{INSTALL_PS1}'; "
        f"Update-ManagedCheckout -Repo '{repos.clone}' -Branch main"
    )

    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert _head(repos.clone) == expected


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell not installed")
def test_install_ps1_rewrite_with_local_commits_fails_closed(
    repos: Repositories,
) -> None:
    _commit(repos.clone, "local.txt", "local\n", "local commit")
    _rewrite_origin(repos)
    before = _snapshot(repos.clone)
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f". '{INSTALL_PS1}'; "
        f"Update-ManagedCheckout -Repo '{repos.clone}' -Branch main"
    )

    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert _snapshot(repos.clone) == before
