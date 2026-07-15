from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


_GIT_TIMEOUT_SECONDS = 5.0
_GIT_OUTPUT_LIMIT = 4096
_HEAD_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


@dataclass(frozen=True)
class WorktreeSnapshot:
    """Exact source spelling plus the Git worktree identity it resolved to."""

    cwd: str
    git_root: str
    branch: str
    head: str
    worktree_id: str


class WorktreeSnapshotError(ValueError):
    """A fixed, non-sensitive failure from worktree capture or validation."""

    def __init__(self, code: str) -> None:
        if code not in {"source_cwd_missing", "source_identity_mismatch"}:
            raise ValueError("invalid worktree snapshot error code")
        self.code = code
        super().__init__(code)


def capture_worktree_snapshot(cwd: str) -> WorktreeSnapshot:
    """Capture the exact cwd spelling and the immutable worktree identity."""

    source = _source_path(cwd)
    source_lstat: os.stat_result | None = None
    resolved: Path | None = None
    resolved_stat: os.stat_result | None = None
    try:
        source_lstat = source.lstat()
        resolved = source.resolve(strict=True)
        resolved_stat = resolved.stat()
    except (FileNotFoundError, NotADirectoryError, OSError):
        pass
    if source_lstat is None or resolved is None or resolved_stat is None:
        raise WorktreeSnapshotError("source_cwd_missing")
    if not resolved.is_dir():
        raise WorktreeSnapshotError("source_cwd_missing")

    git_root: Path | None = None
    git_dir: Path | None = None
    common_dir: Path | None = None
    head: str | None = None
    branch_result: str | None = None
    try:
        git_root = Path(_git(resolved, "rev-parse", "--show-toplevel")).resolve(
            strict=True
        )
        git_dir = Path(_git(resolved, "rev-parse", "--absolute-git-dir")).resolve(
            strict=True
        )
        common_dir = Path(_git(resolved, "rev-parse", "--git-common-dir"))
        if not common_dir.is_absolute():
            common_dir = resolved / common_dir
        common_dir = common_dir.resolve(strict=True)
        head = _git(resolved, "rev-parse", "--verify", "HEAD")
        branch_result = _git_result(
            resolved,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            allowed_returncodes=(0, 1),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    if any(
        value is None
        for value in (git_root, git_dir, common_dir, head, branch_result)
    ):
        raise WorktreeSnapshotError("source_identity_mismatch")
    assert git_root is not None
    assert git_dir is not None
    assert common_dir is not None
    assert head is not None
    assert branch_result is not None

    if not _HEAD_RE.fullmatch(head):
        raise WorktreeSnapshotError("source_identity_mismatch")
    branch = branch_result if branch_result else "(detached)"
    if any(character in branch for character in "\x00\r\n"):
        raise WorktreeSnapshotError("source_identity_mismatch")

    identity = {
        "version": 1,
        "source_path": _normalized_path(source),
        "source_entry": _stat_identity(source_lstat),
        "resolved_cwd": _normalized_path(resolved),
        "resolved_cwd_entry": _stat_identity(resolved_stat),
        "git_root": _normalized_path(git_root),
        "git_root_entry": _stat_identity(git_root.stat()),
        "git_dir": _normalized_path(git_dir),
        "git_dir_entry": _stat_identity(git_dir.stat()),
        "common_dir": _normalized_path(common_dir),
        "common_dir_entry": _stat_identity(common_dir.stat()),
    }
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return WorktreeSnapshot(
        cwd=str(source),
        git_root=str(git_root),
        branch=branch,
        head=head.lower(),
        worktree_id=f"worktree:v1:{hashlib.sha256(encoded).hexdigest()}",
    )


def validate_worktree_snapshot(
    snapshot: WorktreeSnapshot,
) -> tuple[WorktreeSnapshot, tuple[str, ...]]:
    """Fail closed on identity changes while permitting truthful branch/HEAD drift."""

    _validate_recorded_snapshot(snapshot)
    current = capture_worktree_snapshot(snapshot.cwd)
    if (
        _normalized_path(Path(current.cwd)) != _normalized_path(Path(snapshot.cwd))
        or _normalized_path(Path(current.git_root))
        != _normalized_path(Path(snapshot.git_root))
        or current.worktree_id != snapshot.worktree_id
    ):
        raise WorktreeSnapshotError("source_identity_mismatch")

    warnings: list[str] = []
    if current.branch != snapshot.branch:
        warnings.append(
            "worktree_branch_drift: "
            f"recorded={snapshot.branch} current={current.branch}"
        )
    if current.head != snapshot.head:
        warnings.append(
            f"worktree_head_drift: recorded={snapshot.head} current={current.head}"
        )
    return current, tuple(warnings)


def _source_path(cwd: str) -> Path:
    if (
        not isinstance(cwd, str)
        or not cwd.strip()
        or "\x00" in cwd
        or len(cwd) > 32_768
    ):
        raise WorktreeSnapshotError("source_cwd_missing")
    return Path(os.path.abspath(os.path.normpath(cwd)))


def _validate_recorded_snapshot(snapshot: WorktreeSnapshot) -> None:
    if not isinstance(snapshot, WorktreeSnapshot):
        raise WorktreeSnapshotError("source_identity_mismatch")
    for value in (
        snapshot.cwd,
        snapshot.git_root,
        snapshot.branch,
        snapshot.head,
        snapshot.worktree_id,
    ):
        if not isinstance(value, str) or not value or any(
            character in value for character in "\x00\r\n"
        ):
            raise WorktreeSnapshotError("source_identity_mismatch")
    if not os.path.isabs(snapshot.cwd) or not os.path.isabs(snapshot.git_root):
        raise WorktreeSnapshotError("source_identity_mismatch")
    if not _HEAD_RE.fullmatch(snapshot.head) or not re.fullmatch(
        r"worktree:v1:[0-9a-f]{64}", snapshot.worktree_id
    ):
        raise WorktreeSnapshotError("source_identity_mismatch")


def _git(cwd: Path, *args: str) -> str:
    return _git_result(cwd, *args, allowed_returncodes=(0,))


def _git_result(
    cwd: Path,
    *args: str,
    allowed_returncodes: tuple[int, ...],
) -> str:
    env = os.environ.copy()
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C.UTF-8",
    })
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        shell=False,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode not in allowed_returncodes:
        raise ValueError("git_identity_unavailable")
    output = completed.stdout
    if len(output) > _GIT_OUTPUT_LIMIT:
        raise ValueError("git_identity_unavailable")
    try:
        value = output.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("git_identity_unavailable") from exc
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("git_identity_unavailable")
    return value


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _stat_identity(value: os.stat_result) -> tuple[int, int]:
    return int(value.st_dev), int(value.st_ino)


__all__ = [
    "WorktreeSnapshot",
    "WorktreeSnapshotError",
    "capture_worktree_snapshot",
    "validate_worktree_snapshot",
]
