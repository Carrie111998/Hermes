from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


_GIT_CAPTURE_TIMEOUT_SECONDS = 15.0
_GIT_OUTPUT_LIMIT = 4096
_HEAD_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


@dataclass(frozen=True)
class WorktreeSnapshot:
    """Exact source spelling plus the Git worktree identity it resolved to."""

    cwd: str
    git_root: str | None
    branch: str | None
    head: str | None
    worktree_id: str


class WorktreeSnapshotError(ValueError):
    """A fixed, non-sensitive failure from worktree capture or validation."""

    def __init__(self, code: str) -> None:
        if code not in {
            "source_cwd_missing",
            "source_identity_mismatch",
            "permission_preflight_failed",
        }:
            raise ValueError("invalid worktree snapshot error code")
        self.code = code
        super().__init__(code)


def capture_worktree_snapshot(cwd: str) -> WorktreeSnapshot:
    """Capture the exact cwd spelling and the immutable worktree identity."""

    source = _source_path(cwd)
    source_lstat: os.stat_result | None = None
    resolved: Path | None = None
    resolved_stat: os.stat_result | None = None
    git_failed = False
    capture_error: str | None = None
    try:
        source_lstat = source.lstat()
        resolved = source.resolve(strict=True)
        resolved_stat = resolved.stat()
    except (FileNotFoundError, NotADirectoryError):
        capture_error = "source_cwd_missing"
    except OSError:
        capture_error = "permission_preflight_failed"
    if capture_error is not None:
        raise WorktreeSnapshotError(capture_error)
    if source_lstat is None or resolved is None or resolved_stat is None:
        raise WorktreeSnapshotError("source_cwd_missing")
    if not resolved.is_dir():
        raise WorktreeSnapshotError("source_cwd_missing")

    deadline = time.monotonic() + _GIT_CAPTURE_TIMEOUT_SECONDS
    try:
        git_root_result = _git_result(
            resolved,
            "rev-parse",
            "--show-toplevel",
            allowed_returncodes=(0, 128),
            deadline=deadline,
        )
        if not git_root_result:
            if _has_git_metadata_in_ancestry(resolved):
                raise WorktreeSnapshotError("source_identity_mismatch")
            return _filesystem_snapshot(
                source=source,
                source_lstat=source_lstat,
                resolved=resolved,
                resolved_stat=resolved_stat,
            )
        git_root = Path(git_root_result).resolve(strict=True)
        git_dir = Path(
            _git(
                resolved,
                "rev-parse",
                "--absolute-git-dir",
                deadline=deadline,
            )
        ).resolve(strict=True)
        common_dir = Path(
            _git(
                resolved,
                "rev-parse",
                "--git-common-dir",
                deadline=deadline,
            )
        )
        if not common_dir.is_absolute():
            common_dir = resolved / common_dir
        common_dir = common_dir.resolve(strict=True)
        head_result = _git_result(
            resolved,
            "rev-parse",
            "--verify",
            "HEAD",
            allowed_returncodes=(0, 128),
            deadline=deadline,
        )
        head = head_result or None
        branch_result = _git_result(
            resolved,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            allowed_returncodes=(0, 1),
            deadline=deadline,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        git_failed = True
    if git_failed:
        raise WorktreeSnapshotError("source_identity_mismatch")

    if head is not None and not _HEAD_RE.fullmatch(head):
        raise WorktreeSnapshotError("source_identity_mismatch")
    branch = branch_result if branch_result else "(detached)"
    if any(character in branch for character in "\x00\r\n"):
        raise WorktreeSnapshotError("source_identity_mismatch")

    identity = {
        "version": 1,
        "kind": "git",
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
        head=(head.lower() if head is not None else None),
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
        or _normalized_optional_path(current.git_root)
        != _normalized_optional_path(snapshot.git_root)
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
    for value in (snapshot.cwd, snapshot.worktree_id):
        if not isinstance(value, str) or not value or any(
            character in value for character in "\x00\r\n"
        ):
            raise WorktreeSnapshotError("source_identity_mismatch")
    if not os.path.isabs(snapshot.cwd):
        raise WorktreeSnapshotError("source_identity_mismatch")
    if snapshot.git_root is None:
        if snapshot.branch is not None or snapshot.head is not None:
            raise WorktreeSnapshotError("source_identity_mismatch")
    else:
        if not isinstance(snapshot.git_root, str) or not os.path.isabs(
            snapshot.git_root
        ):
            raise WorktreeSnapshotError("source_identity_mismatch")
        if (
            not isinstance(snapshot.branch, str)
            or not snapshot.branch
            or any(character in snapshot.branch for character in "\x00\r\n")
            or (
                snapshot.head is not None
                and (
                    not isinstance(snapshot.head, str)
                    or _HEAD_RE.fullmatch(snapshot.head) is None
                )
            )
        ):
            raise WorktreeSnapshotError("source_identity_mismatch")
    if not re.fullmatch(r"worktree:v1:[0-9a-f]{64}", snapshot.worktree_id):
        raise WorktreeSnapshotError("source_identity_mismatch")


def _filesystem_snapshot(
    *,
    source: Path,
    source_lstat: os.stat_result,
    resolved: Path,
    resolved_stat: os.stat_result,
) -> WorktreeSnapshot:
    identity = {
        "version": 1,
        "kind": "filesystem",
        "source_path": _normalized_path(source),
        "source_entry": _stat_identity(source_lstat),
        "resolved_cwd": _normalized_path(resolved),
        "resolved_cwd_entry": _stat_identity(resolved_stat),
    }
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return WorktreeSnapshot(
        cwd=str(source),
        git_root=None,
        branch=None,
        head=None,
        worktree_id=f"worktree:v1:{hashlib.sha256(encoded).hexdigest()}",
    )


def _has_git_metadata_in_ancestry(cwd: Path) -> bool:
    """Return true only when a lexical ancestor visibly owns Git metadata."""

    for directory in (cwd, *cwd.parents):
        marker = directory / ".git"
        try:
            marker.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise WorktreeSnapshotError("source_identity_mismatch") from None
        return True
    return False


def _git(cwd: Path, *args: str, deadline: float) -> str:
    return _git_result(
        cwd,
        *args,
        allowed_returncodes=(0,),
        deadline=deadline,
    )


def _git_result(
    cwd: Path,
    *args: str,
    allowed_returncodes: tuple[int, ...],
    deadline: float,
) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(["git"], 0)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
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
        timeout=remaining,
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


def _normalized_optional_path(value: str | None) -> str | None:
    return None if value is None else _normalized_path(Path(value))


def _stat_identity(value: os.stat_result) -> tuple[int, int]:
    return int(value.st_dev), int(value.st_ino)


__all__ = [
    "WorktreeSnapshot",
    "WorktreeSnapshotError",
    "capture_worktree_snapshot",
    "validate_worktree_snapshot",
]
