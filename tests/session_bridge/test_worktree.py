from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shutil
import stat
import subprocess
from types import SimpleNamespace

import pytest

from session_bridge.worktree import (
    WorktreeSnapshotError,
    capture_worktree_snapshot,
    validate_worktree_snapshot,
)


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Session Bridge Tests",
            "-c",
            "user.email=session-bridge@example.invalid",
            "-C",
            str(cwd),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def _repo(path: Path, *, content: str = "first") -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    (path / "tracked.txt").write_text(content, encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-m", "initial")
    return path


def _directory_alias(alias: Path, target: Path) -> str:
    try:
        alias.symlink_to(target, target_is_directory=True)
        return "symlink"
    except OSError as symlink_error:
        if os.name != "nt":
            pytest.skip(f"directory symlink unavailable: {symlink_error}")
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(target)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if completed.returncode != 0:
            pytest.skip(
                "directory identity retarget test requires a symlink or Windows "
                "junction; both are unavailable"
            )
        return "junction"


def _remove_tree(path: Path) -> None:
    def _make_writable(function, value, _error) -> None:
        os.chmod(value, stat.S_IWRITE)
        function(value)

    shutil.rmtree(path, onerror=_make_writable)


def test_worktree_snapshot_captures_exact_spelling_and_linked_identity(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "main")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-b", "feature/exact", str(linked))

    snapshot = capture_worktree_snapshot(str(linked / "."))
    main_snapshot = capture_worktree_snapshot(str(repo))

    assert snapshot.cwd == os.path.abspath(str(linked / "."))
    assert snapshot.git_root == str(linked.resolve(strict=True))
    assert snapshot.branch == "feature/exact"
    assert snapshot.head == _git(linked, "rev-parse", "HEAD")
    assert snapshot.worktree_id != main_snapshot.worktree_id
    assert validate_worktree_snapshot(snapshot) == (snapshot, ())


def test_worktree_capture_ignores_inherited_git_repository_redirection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _repo(tmp_path / "source")
    redirected = _repo(tmp_path / "redirected", content="private")
    expected_head = _git(source, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", str(redirected / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(redirected))

    snapshot = capture_worktree_snapshot(str(source))

    assert snapshot.cwd == os.path.abspath(str(source))
    assert snapshot.git_root == str(source.resolve(strict=True))
    assert snapshot.head == expected_head


def test_worktree_snapshot_supports_non_git_directory_identity(tmp_path: Path) -> None:
    source = tmp_path / "ordinary"
    source.mkdir()

    snapshot = capture_worktree_snapshot(str(source))

    assert snapshot.git_root is None
    assert snapshot.branch is None
    assert snapshot.head is None
    assert validate_worktree_snapshot(snapshot) == (snapshot, ())


def test_worktree_capture_rejects_git_probe_failure_when_git_metadata_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path / "repo")
    nested = repo / "nested" / "source"
    nested.mkdir(parents=True)
    original_run = subprocess.run

    def fail_discovery(command: list[str], **kwargs: object):
        if command[:3] == ["git", "-C", str(nested.resolve(strict=True))]:
            return SimpleNamespace(returncode=128, stdout=b"")
        return original_run(command, **kwargs)

    monkeypatch.setattr("session_bridge.worktree.subprocess.run", fail_discovery)

    with pytest.raises(WorktreeSnapshotError) as raised:
        capture_worktree_snapshot(str(nested))

    assert raised.value.code == "source_identity_mismatch"


def test_worktree_validation_rejects_replaced_git_metadata(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    recorded = capture_worktree_snapshot(str(repo))
    _remove_tree(repo / ".git")
    (repo / ".git").mkdir()

    with pytest.raises(WorktreeSnapshotError) as raised:
        validate_worktree_snapshot(recorded)

    assert raised.value.code == "source_identity_mismatch"


def test_worktree_capture_rejects_probe_failure_for_worktree_git_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path / "main")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-b", "feature/linked", str(linked))
    assert (linked / ".git").is_file()
    monkeypatch.setattr(
        "session_bridge.worktree.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=128, stdout=b""),
    )

    with pytest.raises(WorktreeSnapshotError) as raised:
        capture_worktree_snapshot(str(linked))

    assert raised.value.code == "source_identity_mismatch"


def test_worktree_snapshot_supports_unborn_repository_identity(tmp_path: Path) -> None:
    source = tmp_path / "unborn"
    source.mkdir()
    _git(source, "init", "-b", "main")

    snapshot = capture_worktree_snapshot(str(source))

    assert snapshot.git_root == str(source.resolve(strict=True))
    assert snapshot.branch == "main"
    assert snapshot.head is None
    assert validate_worktree_snapshot(snapshot) == (snapshot, ())


def test_worktree_git_probes_share_one_total_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _repo(tmp_path / "source")
    git_dir = source / ".git"
    clock = [100.0]
    timeouts: list[float] = []

    def monotonic() -> float:
        return clock[0]

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        timeout = float(kwargs["timeout"])
        timeouts.append(timeout)
        clock[0] += 6.0
        args = command[3:]
        if args == ["rev-parse", "--show-toplevel"]:
            output = str(source)
        elif args == ["rev-parse", "--absolute-git-dir"]:
            output = str(git_dir)
        elif args == ["rev-parse", "--git-common-dir"]:
            output = str(git_dir)
        else:
            output = "0" * 40
        return SimpleNamespace(returncode=0, stdout=f"{output}\n".encode())

    monkeypatch.setattr("session_bridge.worktree.time.monotonic", monotonic)
    monkeypatch.setattr("session_bridge.worktree.subprocess.run", run)

    with pytest.raises(WorktreeSnapshotError) as raised:
        capture_worktree_snapshot(str(source))

    assert raised.value.code == "source_identity_mismatch"
    assert timeouts == [15.0, 9.0, 3.0]


def test_worktree_validation_allows_branch_and_head_drift_with_truthful_warnings(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    recorded = capture_worktree_snapshot(str(repo))
    _git(repo, "checkout", "-b", "feature/drift")
    (repo / "tracked.txt").write_text("second", encoding="utf-8")
    _git(repo, "commit", "-am", "drift")

    current, warnings = validate_worktree_snapshot(recorded)

    assert current.cwd == recorded.cwd
    assert current.worktree_id == recorded.worktree_id
    assert current.branch == "feature/drift"
    assert current.head != recorded.head
    assert warnings == (
        "worktree_branch_drift: recorded=main current=feature/drift",
        f"worktree_head_drift: recorded={recorded.head} current={current.head}",
    )


def test_worktree_validation_fails_closed_when_cwd_disappears(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    recorded = capture_worktree_snapshot(str(repo))
    _remove_tree(repo)

    with pytest.raises(WorktreeSnapshotError) as raised:
        validate_worktree_snapshot(recorded)

    assert raised.value.code == "source_cwd_missing"
    assert str(raised.value) == "source_cwd_missing"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_worktree_capture_missing_path_is_excludable(tmp_path: Path) -> None:
    missing = tmp_path / "deleted-worktree"

    with pytest.raises(WorktreeSnapshotError) as raised:
        capture_worktree_snapshot(str(missing))

    assert raised.value.code == "source_cwd_missing"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize("error", [PermissionError(), OSError("io")])
def test_worktree_capture_inaccessible_path_is_not_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
) -> None:
    def _raise_error(_path: Path) -> os.stat_result:
        raise error

    monkeypatch.setattr(Path, "lstat", _raise_error)

    with pytest.raises(WorktreeSnapshotError) as raised:
        capture_worktree_snapshot(str(tmp_path))

    assert raised.value.code == "permission_preflight_failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_worktree_capture_does_not_expose_raw_git_error_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _repo(tmp_path / "private-repository-name")
    monkeypatch.setattr(
        "session_bridge.worktree.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("private-git-command", 5)
        ),
    )

    with pytest.raises(WorktreeSnapshotError) as raised:
        capture_worktree_snapshot(str(source))

    assert raised.value.code == "source_identity_mismatch"
    assert str(raised.value) == "source_identity_mismatch"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_worktree_validation_fails_closed_after_repository_replacement(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    recorded = capture_worktree_snapshot(str(repo))
    mirror = tmp_path / "repo-backup.git"
    subprocess.run(
        ["git", "clone", "--mirror", str(repo), str(mirror)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    _remove_tree(repo)
    subprocess.run(
        ["git", "clone", str(mirror), str(repo)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    replacement = capture_worktree_snapshot(str(repo))

    assert replacement.branch == recorded.branch
    assert replacement.head == recorded.head
    assert replacement.worktree_id != recorded.worktree_id

    with pytest.raises(WorktreeSnapshotError) as raised:
        validate_worktree_snapshot(recorded)

    assert raised.value.code == "source_identity_mismatch"


def test_worktree_validation_fails_closed_after_alias_retarget(tmp_path: Path) -> None:
    first = _repo(tmp_path / "first")
    second = _repo(tmp_path / "second")
    alias = tmp_path / "source-alias"
    alias_kind = _directory_alias(alias, first)
    recorded = capture_worktree_snapshot(str(alias))

    if alias_kind == "junction":
        os.rmdir(alias)
    else:
        alias.unlink()
    _directory_alias(alias, second)

    with pytest.raises(WorktreeSnapshotError) as raised:
        validate_worktree_snapshot(recorded)

    assert raised.value.code == "source_identity_mismatch"


def test_worktree_validation_fails_closed_after_linked_worktree_substitution(
    tmp_path: Path,
) -> None:
    first = _repo(tmp_path / "first")
    source = tmp_path / "source-worktree"
    _git(first, "worktree", "add", "-b", "feature/first", str(source))
    recorded = capture_worktree_snapshot(str(source))
    _git(first, "worktree", "remove", "--force", str(source))

    second = _repo(tmp_path / "second")
    _git(second, "worktree", "add", "-b", "feature/second", str(source))

    with pytest.raises(WorktreeSnapshotError) as raised:
        validate_worktree_snapshot(recorded)

    assert raised.value.code == "source_identity_mismatch"


@pytest.mark.parametrize("field", ["cwd", "git_root", "worktree_id"])
def test_worktree_validation_rejects_tampered_recorded_identity(
    tmp_path: Path,
    field: str,
) -> None:
    repo = _repo(tmp_path / "repo")
    recorded = capture_worktree_snapshot(str(repo))
    tampered = replace(recorded, **{field: getattr(recorded, field) + "-tampered"})

    with pytest.raises(WorktreeSnapshotError) as raised:
        validate_worktree_snapshot(tampered)

    assert raised.value.code in {"source_cwd_missing", "source_identity_mismatch"}
