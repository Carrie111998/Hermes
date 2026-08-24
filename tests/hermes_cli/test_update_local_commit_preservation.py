"""Real-repository regression coverage for the pinned updater policy."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_cli.update_cmd import (
    _LOCAL_NONE,
    _REMOTE_ADVANCED,
    _REMOTE_REWRITTEN,
    _REMOTE_UNKNOWN,
    _REF_ABSENT,
    _REF_PRESENT,
    _apply_pinned_update,
    _capture_apply_generations,
    _classify_local_history,
    _classify_remote_history,
    _classify_remote_without_tracking_baseline,
    _cleanup_owned_fetch_ref,
    _protect_detached_checkout,
    _resolve_commit,
    _resolve_optional_commit,
    _rollback_ref_update,
    _restore_stashed_changes,
    _stash_local_changes_if_needed,
    _validate_update_branch,
    StashRestoreSafetyError,
)

GIT = ["git"]


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        GIT + list(args),
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


def _snapshot(repo: Path) -> tuple[str, bytes, bytes, bytes, dict[str, bytes], str]:
    files = {
        str(path.relative_to(repo)): path.read_bytes()
        for path in sorted(repo.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }
    return (
        _git(repo, "rev-parse", "HEAD").stdout.strip(),
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
    old_tip = _git(origin, "rev-parse", "HEAD").stdout.strip()

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    return Repositories(origin, clone, old_tip)


def _fetch_policy(repos: Repositories) -> tuple[str, str, str]:
    local_head = _resolve_commit(GIT, repos.clone, "HEAD")
    old_origin = _resolve_commit(GIT, repos.clone, "refs/remotes/origin/main")
    assert local_head is not None
    assert old_origin is not None
    _git(repos.clone, "fetch", "-q", "origin", "main")
    new_origin = _resolve_commit(GIT, repos.clone, "refs/remotes/origin/main")
    assert new_origin is not None
    return local_head, old_origin, new_origin


def _apply(
    repos: Repositories,
    local_head: str,
    old_origin: str,
    new_origin: str,
) -> tuple[bool, str]:
    remote_history = _classify_remote_history(
        GIT, repos.clone, old_origin, new_origin
    )
    return _apply_pinned_update(
        GIT,
        repos.clone,
        branch="main",
        local_head_sha=local_head,
        old_origin_sha=old_origin,
        new_origin_sha=new_origin,
        remote_history=remote_history,
    )


def test_no_local_commit_normal_advance_fast_forwards_pinned_sha(
    repos: Repositories,
) -> None:
    expected = _commit(repos.origin, "remote.txt", "remote\n", "remote advance")
    local_head, old_origin, new_origin = _fetch_policy(repos)

    assert new_origin == expected
    assert _classify_remote_history(GIT, repos.clone, old_origin, new_origin) == _REMOTE_ADVANCED
    assert _classify_local_history(GIT, repos.clone, old_origin, local_head) == _LOCAL_NONE
    assert _apply(repos, local_head, old_origin, new_origin) == (True, "fast_forwarded")
    assert _resolve_commit(GIT, repos.clone, "HEAD") == expected


def test_normal_advance_merges_local_commits_and_ignores_mutable_ref_race(
    repos: Repositories,
) -> None:
    local_commit = _commit(repos.clone, "local.txt", "local\n", "local commit")
    pinned_remote = _commit(
        repos.origin, "remote.txt", "pinned\n", "pinned remote advance"
    )
    local_head, old_origin, new_origin = _fetch_policy(repos)
    assert local_head == local_commit
    assert new_origin == pinned_remote

    # Move the mutable tracking ref again after policy admission. The update
    # must integrate the already-pinned object, never this later ref value.
    raced_remote = _commit(repos.origin, "race.txt", "race\n", "later remote")
    _git(repos.clone, "fetch", "-q", "origin", "main")
    assert _resolve_commit(GIT, repos.clone, "refs/remotes/origin/main") == raced_remote

    updated, outcome = _apply(repos, local_head, old_origin, new_origin)
    assert updated is True
    assert outcome.startswith("merged:refs/hermes-update-backups/")
    head = _resolve_commit(GIT, repos.clone, "HEAD")
    assert head is not None
    assert _git(repos.clone, "merge-base", "--is-ancestor", local_commit, head).returncode == 0
    assert _git(repos.clone, "merge-base", "--is-ancestor", pinned_remote, head).returncode == 0
    assert _git(
        repos.clone, "merge-base", "--is-ancestor", raced_remote, head, check=False
    ).returncode == 1
    recovery_ref = outcome.split(":", 1)[1]
    assert _resolve_commit(GIT, repos.clone, recovery_ref) == local_commit
    assert _git(repos.clone, "rev-list", "--parents", "-n", "1", head).stdout.split()[1:] == [
        local_commit,
        pinned_remote,
    ]


def test_local_ahead_checkout_already_contains_pinned_target(
    repos: Repositories,
) -> None:
    local_commit = _commit(repos.clone, "local.txt", "local\n", "local commit")
    local_head, old_origin, new_origin = _fetch_policy(repos)

    assert new_origin == old_origin
    assert _apply(repos, local_head, old_origin, new_origin) == (
        True,
        "already_integrated",
    )
    assert _resolve_commit(GIT, repos.clone, "HEAD") == local_commit


def test_same_sha_branch_switch_fails_mutation_boundary(repos: Repositories) -> None:
    expected = _commit(repos.origin, "remote.txt", "remote\n", "remote advance")
    local_head, old_origin, new_origin = _fetch_policy(repos)
    _git(repos.clone, "checkout", "-qb", "concurrent-branch")
    before = _snapshot(repos.clone)

    assert new_origin == expected
    assert _apply_pinned_update(
        GIT,
        repos.clone,
        branch="main",
        local_head_sha=local_head,
        old_origin_sha=old_origin,
        new_origin_sha=new_origin,
        remote_history=_REMOTE_ADVANCED,
    ) == (False, "wrong_branch")
    assert _snapshot(repos.clone) == before


@pytest.mark.parametrize("remote_advances", [False, True])
def test_detached_local_commit_gets_durable_ref_before_managed_checkout(
    repos: Repositories, remote_advances: bool
) -> None:
    """Normal and no-op updates must not leave detached work reflog-only."""

    _git(repos.clone, "checkout", "-q", "--detach", "HEAD")
    detached_commit = _commit(
        repos.clone,
        "detached-local.txt",
        "detached local work\n",
        "detached local commit",
    )
    if remote_advances:
        remote_tip = _commit(
            repos.origin,
            "remote.txt",
            "remote advance\n",
            "remote advance",
        )
    else:
        remote_tip = repos.old_tip

    recovery_ref = _protect_detached_checkout(
        GIT, repos.clone, detached_commit
    )
    assert recovery_ref is not None

    _git(repos.clone, "checkout", "-q", "main")
    _git(repos.clone, "fetch", "-q", "origin", "main")
    _git(repos.clone, "merge", "-q", "--ff-only", remote_tip)
    _git(repos.clone, "reflog", "expire", "--expire=now", "--all")
    _git(repos.clone, "gc", "--prune=now")

    assert _resolve_commit(GIT, repos.clone, recovery_ref) == detached_commit
    assert _resolve_commit(GIT, repos.clone, "HEAD") == remote_tip


def test_confirmed_rewrite_without_local_commits_moves_to_pinned_sha(
    repos: Repositories,
) -> None:
    (repos.origin / "base.txt").write_text("rewritten\n", encoding="utf-8")
    _git(repos.origin, "add", "base.txt")
    _git(repos.origin, "commit", "--amend", "-qm", "rewritten base")
    rewritten = _resolve_commit(GIT, repos.origin, "HEAD")
    assert rewritten is not None
    local_head, old_origin, new_origin = _fetch_policy(repos)

    assert _classify_remote_history(GIT, repos.clone, old_origin, new_origin) == _REMOTE_REWRITTEN
    assert _apply(repos, local_head, old_origin, new_origin) == (
        True,
        "rewritten_remote_adopted",
    )
    assert _resolve_commit(GIT, repos.clone, "HEAD") == rewritten


def test_rewrite_cas_preserves_concurrent_branch_commit(
    repos: Repositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    concurrent = _commit(
        repos.clone, "concurrent.txt", "concurrent\n", "concurrent candidate"
    )
    _git(repos.clone, "reset", "--hard", repos.old_tip)
    (repos.origin / "base.txt").write_text("rewritten\n", encoding="utf-8")
    _git(repos.origin, "add", "base.txt")
    _git(repos.origin, "commit", "--amend", "-qm", "rewritten base")
    local_head, old_origin, new_origin = _fetch_policy(repos)

    real_run = subprocess.run
    injected = False

    def raced_run(command, *args, **kwargs):
        nonlocal injected
        result = real_run(command, *args, **kwargs)
        if (
            not injected
            and command[1:3] == ["checkout", "--detach"]
            and result.returncode == 0
        ):
            injected = True
            moved = real_run(
                ["git", "update-ref", "refs/heads/main", concurrent, local_head],
                cwd=repos.clone,
                capture_output=True,
                text=True,
                check=False,
            )
            assert moved.returncode == 0
        return result

    monkeypatch.setattr("hermes_cli.update_cmd.subprocess.run", raced_run)
    updated, outcome = _apply(repos, local_head, old_origin, new_origin)

    assert updated is False
    assert outcome == "rewrite_branch_changed"
    assert _resolve_commit(GIT, repos.clone, "refs/heads/main") == concurrent
    recovery_refs = _git(
        repos.clone,
        "for-each-ref",
        "--format=%(objectname)",
        "refs/hermes-update-backups/",
    ).stdout.splitlines()
    assert local_head in recovery_refs


def test_confirmed_remote_rewind_to_ancestor_is_not_mistaken_for_no_update(
    repos: Repositories,
) -> None:
    old_base = repos.old_tip
    advanced = _commit(repos.origin, "remote.txt", "remote\n", "remote advance")
    _git(repos.clone, "fetch", "-q", "origin", "main")
    _git(repos.clone, "merge", "--ff-only", advanced)
    _git(repos.origin, "reset", "--hard", old_base)
    local_head, old_origin, new_origin = _fetch_policy(repos)

    assert local_head == advanced
    assert old_origin == advanced
    assert new_origin == old_base
    assert _classify_remote_history(GIT, repos.clone, old_origin, new_origin) == _REMOTE_REWRITTEN
    assert _apply(repos, local_head, old_origin, new_origin) == (
        True,
        "rewritten_remote_adopted",
    )
    assert _resolve_commit(GIT, repos.clone, "HEAD") == old_base


def test_confirmed_rewrite_with_local_commits_fails_without_state_change(
    repos: Repositories,
) -> None:
    _commit(repos.clone, "local.txt", "local\n", "local commit")
    (repos.origin / "base.txt").write_text("rewritten\n", encoding="utf-8")
    _git(repos.origin, "add", "base.txt")
    _git(repos.origin, "commit", "--amend", "-qm", "rewritten base")
    local_head, old_origin, new_origin = _fetch_policy(repos)
    before = _snapshot(repos.clone)

    assert _apply(repos, local_head, old_origin, new_origin) == (
        False,
        "rewrite_with_local_commits",
    )
    assert _snapshot(repos.clone) == before


def test_missing_tip_is_unknown_and_fails_closed(repos: Repositories) -> None:
    local_head = _resolve_commit(GIT, repos.clone, "HEAD")
    old_origin = _resolve_commit(GIT, repos.clone, "refs/remotes/origin/main")
    assert local_head is not None
    assert old_origin is not None
    before = _snapshot(repos.clone)

    assert _classify_remote_history(GIT, repos.clone, old_origin, None) == _REMOTE_UNKNOWN
    assert _apply_pinned_update(
        GIT,
        repos.clone,
        branch="main",
        local_head_sha=local_head,
        old_origin_sha=old_origin,
        new_origin_sha=None,
        remote_history=_REMOTE_UNKNOWN,
    ) == (False, "history_unknown")
    assert _snapshot(repos.clone) == before


def test_ancestry_operational_error_is_unknown_and_fails_closed(
    repos: Repositories, tmp_path: Path
) -> None:
    _commit(repos.origin, "remote.txt", "remote\n", "remote advance")
    local_head, old_origin, new_origin = _fetch_policy(repos)
    wrapper = tmp_path / "git-with-broken-merge-base"
    wrapper.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "merge-base" ]; then exit 2; fi\n'
        'exec git "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    broken_git = [str(wrapper)]
    before = _snapshot(repos.clone)

    assert (
        _classify_remote_history(broken_git, repos.clone, old_origin, new_origin)
        == _REMOTE_UNKNOWN
    )
    assert _apply_pinned_update(
        broken_git,
        repos.clone,
        branch="main",
        local_head_sha=local_head,
        old_origin_sha=old_origin,
        new_origin_sha=new_origin,
        remote_history=_REMOTE_UNKNOWN,
    ) == (False, "history_unknown")
    assert _snapshot(repos.clone) == before


def test_merge_conflict_aborts_and_restores_staged_unstaged_and_untracked_state(
    repos: Repositories,
) -> None:
    local_head = _commit(
        repos.clone, "conflict.txt", "local\n", "local conflicting commit"
    )
    _commit(repos.origin, "conflict.txt", "remote\n", "remote conflicting commit")
    _, old_origin, new_origin = _fetch_policy(repos)

    # The updater owns only its UUID-marked stash. An older unrelated entry
    # must survive restore/drop unchanged.
    (repos.clone / "preexisting.txt").write_text("older stash\n", encoding="utf-8")
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

    stash_ref = _stash_local_changes_if_needed(GIT, repos.clone)
    assert stash_ref is not None
    updated, outcome = _apply(repos, local_head, old_origin, new_origin)
    assert updated is False
    assert outcome.startswith("merge_conflict:refs/hermes-update-backups/")
    assert _resolve_commit(GIT, repos.clone, "HEAD") == local_head
    assert _git(repos.clone, "status", "--porcelain").stdout == ""
    assert _resolve_commit(GIT, repos.clone, "MERGE_HEAD") is None

    assert _restore_stashed_changes(
        GIT, repos.clone, stash_ref, restore_index=True
    ) is True
    after = _snapshot(repos.clone)
    assert after[:5] == before[:5]
    assert "unrelated-preexisting-stash" in after[5]
    assert stash_ref in after[5]


def test_stash_conflict_is_left_intact_and_can_stop_the_pipeline(
    repos: Repositories,
) -> None:
    (repos.clone / "conflict.txt").write_text("local edit\n", encoding="utf-8")
    stash_ref = _stash_local_changes_if_needed(GIT, repos.clone)
    assert stash_ref is not None
    (repos.clone / "conflict.txt").write_text("new generation\n", encoding="utf-8")
    _git(repos.clone, "add", "conflict.txt")
    _git(repos.clone, "commit", "-qm", "new generation")

    with pytest.raises(StashRestoreSafetyError, match="produced conflicts"):
        _restore_stashed_changes(
            GIT,
            repos.clone,
            stash_ref,
            restore_index=True,
            raise_on_unsafe=True,
        )

    assert _git(repos.clone, "ls-files", "--unmerged").stdout.strip()
    assert stash_ref in _git(
        repos.clone, "stash", "list", "--format=%H"
    ).stdout.splitlines()
    assert stash_ref in _git(
        repos.clone,
        "for-each-ref",
        "--format=%(objectname)",
        "refs/hermes-update-stashes/",
    ).stdout.splitlines()


def test_tracking_rollback_keeps_rewrite_classification_on_second_attempt(
    repos: Repositories,
) -> None:
    local_commit = _commit(repos.clone, "local.txt", "local\n", "local commit")
    (repos.origin / "base.txt").write_text("rewritten\n", encoding="utf-8")
    _git(repos.origin, "add", "base.txt")
    _git(repos.origin, "commit", "--amend", "-qm", "rewritten base")
    old_tracking = _resolve_commit(GIT, repos.clone, "refs/remotes/origin/main")
    assert old_tracking is not None

    for suffix in ("first", "second"):
        fetch_ref = f"refs/hermes-update-fetches/{suffix}"
        _git(
            repos.clone,
            "fetch",
            "--no-tags",
            "--refmap=",
            "origin",
            f"+refs/heads/main:{fetch_ref}",
        )
        fetched = _resolve_commit(GIT, repos.clone, fetch_ref)
        assert fetched is not None
        _git(
            repos.clone,
            "update-ref",
            "refs/remotes/origin/main",
            fetched,
            old_tracking,
        )
        assert _classify_remote_history(GIT, repos.clone, old_tracking, fetched) == _REMOTE_REWRITTEN
        assert _classify_local_history(GIT, repos.clone, old_tracking, local_commit) != _LOCAL_NONE
        assert _rollback_ref_update(
            GIT,
            repos.clone,
            ref="refs/remotes/origin/main",
            old_state="present",
            old_sha=old_tracking,
            new_sha=fetched,
        )
        assert _cleanup_owned_fetch_ref(GIT, repos.clone, fetch_ref, fetched)
        assert _resolve_commit(GIT, repos.clone, "refs/remotes/origin/main") == old_tracking


@pytest.mark.parametrize(
    "branch",
    ["-main", "bad..name", "bad name", "", "@{-1}", "refs/heads/main"],
)
def test_update_branch_validation_rejects_ambiguous_names(
    repos: Repositories, branch: str
) -> None:
    if branch == "@{-1}":
        # Make Git's checkout shorthand valid: rc-only validation would now
        # accept it and silently normalize it to ``previous-checkout``.
        _git(repos.clone, "branch", "previous-checkout")
        _git(repos.clone, "checkout", "-q", "previous-checkout")
        _git(repos.clone, "checkout", "-q", "main")
        expanded = _git(
            repos.clone,
            "check-ref-format",
            "--branch",
            branch,
            check=False,
        )
        assert expanded.returncode == 0
        assert expanded.stdout.strip() == "previous-checkout"
    assert _validate_update_branch(GIT, repos.clone, branch) is False


def test_update_branch_validation_accepts_normal_name(repos: Repositories) -> None:
    assert _validate_update_branch(GIT, repos.clone, "release/safe-name") is True


def test_successful_stash_restore_releases_only_hidden_pin(
    repos: Repositories,
) -> None:
    (repos.clone / "staged.txt").write_text("local\n", encoding="utf-8")
    stash_ref = _stash_local_changes_if_needed(GIT, repos.clone)
    assert stash_ref is not None
    assert stash_ref in _git(
        repos.clone,
        "for-each-ref",
        "--format=%(objectname)",
        "refs/hermes-update-stashes/",
    ).stdout.splitlines()

    assert _restore_stashed_changes(GIT, repos.clone, stash_ref)

    assert _git(
        repos.clone,
        "for-each-ref",
        "--format=%(objectname)",
        "refs/hermes-update-stashes/",
    ).stdout.splitlines() == []
    assert stash_ref in _git(
        repos.clone, "stash", "list", "--format=%H"
    ).stdout.splitlines()


def test_absent_tracking_ref_is_exactly_deleted_after_refusal(
    repos: Repositories,
) -> None:
    tracking_ref = "refs/remotes/origin/main"
    fetched = _resolve_commit(GIT, repos.clone, tracking_ref)
    assert fetched is not None
    _git(repos.clone, "update-ref", "-d", tracking_ref, fetched)
    state, _ = _resolve_optional_commit(GIT, repos.clone, tracking_ref)
    assert state == _REF_ABSENT
    zero = "0" * len(fetched)
    _git(repos.clone, "update-ref", tracking_ref, fetched, zero)

    assert _rollback_ref_update(
        GIT,
        repos.clone,
        ref=tracking_ref,
        old_state=_REF_ABSENT,
        old_sha=None,
        new_sha=fetched,
    )
    state, _ = _resolve_optional_commit(GIT, repos.clone, tracking_ref)
    assert state == _REF_ABSENT


def test_absent_tracking_existing_behind_branch_fast_forwards(
    repos: Repositories,
) -> None:
    tracking_ref = "refs/remotes/origin/main"
    fetch_ref = "refs/hermes-update-fetches/absent-behind"
    local_tip = _resolve_commit(GIT, repos.clone, "refs/heads/main")
    old_tracking = _resolve_commit(GIT, repos.clone, tracking_ref)
    assert local_tip == old_tracking
    assert old_tracking is not None
    expected = _commit(repos.origin, "remote.txt", "remote\n", "remote advance")
    _git(repos.clone, "update-ref", "-d", tracking_ref, old_tracking)
    _git(
        repos.clone,
        "fetch",
        "--no-tags",
        "--refmap=",
        "origin",
        f"+refs/heads/main:{fetch_ref}",
    )
    fetched = _resolve_commit(GIT, repos.clone, fetch_ref)
    assert fetched == expected
    _git(repos.clone, "update-ref", tracking_ref, fetched, "0" * len(fetched))

    remote_history, effective_baseline = (
        _classify_remote_without_tracking_baseline(
            GIT,
            repos.clone,
            _REF_PRESENT,
            local_tip,
            fetched,
        )
    )
    assert (remote_history, effective_baseline) == (_REMOTE_ADVANCED, local_tip)
    assert _apply_pinned_update(
        GIT,
        repos.clone,
        branch="main",
        local_head_sha=local_tip,
        old_origin_sha=effective_baseline,
        new_origin_sha=fetched,
        remote_history=remote_history,
    ) == (True, "fast_forwarded")
    assert _resolve_commit(GIT, repos.clone, "HEAD") == expected
    assert _cleanup_owned_fetch_ref(GIT, repos.clone, fetch_ref, fetched)
    assert _resolve_optional_commit(GIT, repos.clone, fetch_ref)[0] == _REF_ABSENT
    assert _resolve_commit(GIT, repos.clone, tracking_ref) == expected


def test_absent_tracking_existing_ahead_branch_is_already_integrated(
    repos: Repositories,
) -> None:
    tracking_ref = "refs/remotes/origin/main"
    fetch_ref = "refs/hermes-update-fetches/absent-ahead"
    old_tracking = _resolve_commit(GIT, repos.clone, tracking_ref)
    assert old_tracking is not None
    local_tip = _commit(repos.clone, "local.txt", "local\n", "local advance")
    _git(repos.clone, "update-ref", "-d", tracking_ref, old_tracking)
    _git(
        repos.clone,
        "fetch",
        "--no-tags",
        "--refmap=",
        "origin",
        f"+refs/heads/main:{fetch_ref}",
    )
    fetched = _resolve_commit(GIT, repos.clone, fetch_ref)
    assert fetched == old_tracking
    _git(repos.clone, "update-ref", tracking_ref, fetched, "0" * len(fetched))

    remote_history, effective_baseline = (
        _classify_remote_without_tracking_baseline(
            GIT,
            repos.clone,
            _REF_PRESENT,
            local_tip,
            fetched,
        )
    )
    assert (remote_history, effective_baseline) == (_REMOTE_ADVANCED, local_tip)
    assert _apply_pinned_update(
        GIT,
        repos.clone,
        branch="main",
        local_head_sha=local_tip,
        old_origin_sha=effective_baseline,
        new_origin_sha=fetched,
        remote_history=remote_history,
    ) == (True, "already_integrated")
    assert _resolve_commit(GIT, repos.clone, "HEAD") == local_tip
    assert _cleanup_owned_fetch_ref(GIT, repos.clone, fetch_ref, fetched)
    assert _resolve_optional_commit(GIT, repos.clone, fetch_ref)[0] == _REF_ABSENT
    assert _resolve_commit(GIT, repos.clone, tracking_ref) == fetched


def test_absent_tracking_rollback_preserves_later_generation(
    repos: Repositories,
) -> None:
    tracking_ref = "refs/remotes/origin/main"
    fetch_ref = "refs/hermes-update-fetches/absent-concurrent"
    installed = _resolve_commit(GIT, repos.clone, tracking_ref)
    assert installed is not None
    _git(repos.clone, "update-ref", "-d", tracking_ref, installed)
    _git(
        repos.clone,
        "fetch",
        "--no-tags",
        "--refmap=",
        "origin",
        f"+refs/heads/main:{fetch_ref}",
    )
    assert _resolve_commit(GIT, repos.clone, fetch_ref) == installed
    _git(repos.clone, "update-ref", tracking_ref, installed, "0" * len(installed))
    later = _commit(repos.origin, "later.txt", "later\n", "later")
    _git(repos.clone, "fetch", "-q", "origin", "main")
    assert _resolve_commit(GIT, repos.clone, tracking_ref) == later

    assert not _rollback_ref_update(
        GIT,
        repos.clone,
        ref=tracking_ref,
        old_state=_REF_ABSENT,
        old_sha=None,
        new_sha=installed,
    )
    assert _resolve_commit(GIT, repos.clone, tracking_ref) == later
    assert _cleanup_owned_fetch_ref(GIT, repos.clone, fetch_ref, installed)
    assert _resolve_optional_commit(GIT, repos.clone, fetch_ref)[0] == _REF_ABSENT


def test_concurrent_clean_commit_is_its_own_rollback_baseline(
    repos: Repositories,
) -> None:
    concurrent = _commit(
        repos.clone, "concurrent-admission.txt", "kept\n", "concurrent admission"
    )

    assert _capture_apply_generations(
        GIT,
        repos.clone,
        branch="main",
    ) == (concurrent, concurrent)


def test_fork_sync_generation_must_still_match_at_apply_boundary(
    repos: Repositories,
) -> None:
    pre_sync = repos.old_tip
    synced = _commit(repos.clone, "synced.txt", "synced\n", "fork sync")
    assert _capture_apply_generations(
        GIT,
        repos.clone,
        branch="main",
        fork_sync_rollback_sha=pre_sync,
        fork_sync_applied_sha=synced,
    ) == (pre_sync, synced)
    concurrent = _commit(
        repos.clone, "after-sync.txt", "concurrent\n", "after sync"
    )
    assert concurrent != synced
    assert _capture_apply_generations(
        GIT,
        repos.clone,
        branch="main",
        fork_sync_rollback_sha=pre_sync,
        fork_sync_applied_sha=synced,
    ) is None
