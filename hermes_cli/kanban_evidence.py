"""Verified workspace evidence for evidence-fenced Kanban leases.

This module has no database responsibilities.  It computes a deterministic
snapshot from Git itself so a worker cannot renew a progress lease by merely
asserting that it changed something.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


_GIT_TIMEOUT_SECONDS = 15
_MAX_GIT_OUTPUT_BYTES = 16 * 1024 * 1024
_MAX_EVIDENCE_FILES = 256
_MAX_UNTRACKED_BYTES = 16 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_CANONICAL_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def _hardened_git_command(repo: str | Path, *args: str) -> list[str]:
    return [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "credential.helper=",
        "-c",
        "core.pager=cat",
        "-C",
        str(repo),
        "--literal-pathspecs",
        *args,
    ]


def _hardened_git_env() -> dict[str, str]:
    env = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": os.devnull,
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", ""),
    }
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


@dataclass(frozen=True)
class WorkspaceEvidence:
    """A deterministic in-scope workspace delta relative to a pinned commit."""

    digest: str | None
    paths: tuple[str, ...]


def normalize_evidence_paths(patterns: Iterable[str]) -> tuple[str, ...]:
    """Return validated, de-duplicated repository-relative glob patterns."""

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in patterns:
        pattern = str(raw).strip().replace("\\", "/")
        if not pattern:
            continue
        parsed = PurePosixPath(pattern)
        if parsed.is_absolute() or ".." in parsed.parts:
            raise ValueError(f"evidence path must stay repository-relative: {raw!r}")
        if pattern.startswith("./"):
            pattern = pattern[2:]
        if pattern not in seen:
            seen.add(pattern)
            normalized.append(pattern)
    if not normalized:
        raise ValueError("evidence lease requires at least one evidence path")
    return tuple(normalized)


def _git(repo: Path, *args: str) -> bytes:
    """Run Git with bounded output and configuration code paths disabled."""

    try:
        proc = subprocess.Popen(
            _hardened_git_command(repo, *args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_hardened_git_env(),
        )
    except OSError as exc:
        raise ValueError(f"cannot inspect evidence workspace: {exc}") from exc

    streams: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    output_bytes = 0
    overflow = False
    lock = threading.Lock()

    def drain(name: str, pipe) -> None:
        nonlocal output_bytes, overflow
        try:
            while chunk := pipe.read(_READ_CHUNK_BYTES):
                with lock:
                    remaining = _MAX_GIT_OUTPUT_BYTES - output_bytes
                    if remaining > 0:
                        streams[name].extend(chunk[:remaining])
                    output_bytes += len(chunk)
                    if output_bytes > _MAX_GIT_OUTPUT_BYTES:
                        overflow = True
                        proc.kill()
                        break
        finally:
            pipe.close()

    assert proc.stdout is not None and proc.stderr is not None
    threads = [
        threading.Thread(target=drain, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", proc.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        returncode = proc.wait(timeout=_GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait()
        raise ValueError(f"git {' '.join(args)} timed out") from exc
    finally:
        for thread in threads:
            thread.join(timeout=2)

    if overflow:
        raise ValueError("git evidence output exceeds byte budget")
    if returncode != 0:
        stderr = bytes(streams["stderr"]).decode("utf-8", "replace")
        raise ValueError(f"git {' '.join(args)} failed: {stderr.strip()}")
    return bytes(streams["stdout"])


def _decode_z_paths(raw: bytes) -> set[str]:
    paths: set[str] = set()
    for value in raw.split(b"\0"):
        if not value:
            continue
        path = value.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or ".." in parsed.parts:
            raise ValueError(f"git returned unsafe workspace path: {path!r}")
        paths.add(path)
    return paths


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _canonical_workspace(workspace: str | os.PathLike[str]) -> Path:
    repo = Path(workspace).expanduser().absolute()
    try:
        resolved = repo.resolve(strict=True)
        mode = repo.stat(follow_symlinks=False).st_mode
    except OSError as exc:
        raise ValueError(f"evidence workspace is not accessible: {repo}") from exc
    if resolved != repo:
        raise ValueError("workspace path must not traverse symlinks")
    if not stat.S_ISDIR(mode):
        raise ValueError(f"evidence workspace is not a directory: {repo}")
    return repo


def _path_parts(relative: str) -> tuple[str, ...]:
    rel = PurePosixPath(relative)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise ValueError(f"git returned unsafe workspace path: {relative!r}")
    return rel.parts


def _hash_untracked_file(repo_fd: int, rel_path: str) -> tuple[str, int]:
    """Hash a stable regular file without following any symlink component."""

    parts = _path_parts(rel_path)
    current_fd = os.dup(repo_fd)
    file_fd = -1
    try:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            for part in parts[:-1]:
                next_fd = os.open(
                    part,
                    directory_flags | nofollow,
                    dir_fd=current_fd,
                )
                os.close(current_fd)
                current_fd = next_fd
            file_fd = os.open(
                parts[-1],
                os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0),
                dir_fd=current_fd,
            )
        except OSError as exc:
            raise ValueError(f"symlink evidence is not accepted: {rel_path}") from exc

        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"untracked evidence is not a regular file: {rel_path}")

        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(file_fd, _READ_CHUNK_BYTES):
            size += len(chunk)
            if size > _MAX_UNTRACKED_BYTES:
                raise ValueError("untracked evidence exceeds byte budget")
            digest.update(chunk)

        after = os.fstat(file_fd)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if before_identity != after_identity or size != after.st_size:
            raise ValueError(
                f"workspace changed during evidence inspection: {rel_path}"
            )
        return digest.hexdigest(), size
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(current_fd)


def _workspace_paths(repo: Path, pin_sha: str) -> tuple[set[str], set[str]]:
    tracked = _decode_z_paths(
        _git(
            repo,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--name-only",
            "-z",
            "--diff-filter=ACDMRTUXB",
            pin_sha,
            "--",
        )
    )
    untracked = _decode_z_paths(
        _git(repo, "ls-files", "--others", "--exclude-standard", "-z", "--")
    )
    if len(tracked | untracked) > _MAX_EVIDENCE_FILES:
        raise ValueError("workspace has too many changed files for evidence collection")
    return tracked, untracked


def _scoped_diff(repo: Path, pin_sha: str, selected: tuple[str, ...]) -> bytes:
    return _git(
        repo,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        pin_sha,
        "--",
        *selected,
    )


def _untracked_hashes(
    repo: Path,
    selected: tuple[str, ...],
    untracked: set[str],
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    total_bytes = 0
    repo_fd = os.open(
        repo,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        for rel_path in selected:
            if rel_path not in untracked:
                continue
            file_hash, size = _hash_untracked_file(repo_fd, rel_path)
            total_bytes += size
            if total_bytes > _MAX_UNTRACKED_BYTES:
                raise ValueError("untracked evidence exceeds byte budget")
            hashes[rel_path] = file_hash
    finally:
        os.close(repo_fd)
    return hashes


def compute_workspace_evidence(
    workspace: str | os.PathLike[str],
    *,
    pin_sha: str,
    evidence_paths: Iterable[str],
) -> WorkspaceEvidence:
    """Hash a new in-scope Git delta relative to ``pin_sha``.

    Tracked, committed, deleted, renamed, and mode-changed content comes from a
    binary Git diff. Untracked regular files are hashed without following
    symlinks. Out-of-scope paths have no effect on the digest.
    """

    repo = _canonical_workspace(workspace)
    patterns = normalize_evidence_paths(evidence_paths)

    top_level = Path(
        _git(repo, "rev-parse", "--show-toplevel")
        .decode("utf-8", errors="strict")
        .strip()
    ).absolute()
    if top_level != repo:
        raise ValueError(
            f"evidence workspace must be the Git top level: {repo} != {top_level}"
        )

    canonical_pin = str(pin_sha).strip().lower()
    if not _CANONICAL_COMMIT_RE.fullmatch(canonical_pin):
        raise ValueError("pin_sha must be an exact canonical commit id")
    resolved_pin = (
        _git(repo, "rev-parse", "--verify", f"{canonical_pin}^{{commit}}")
        .decode("ascii", errors="strict")
        .strip()
        .lower()
    )
    if resolved_pin != canonical_pin:
        raise ValueError("pin_sha must be an exact canonical commit id")

    tracked, untracked = _workspace_paths(repo, canonical_pin)
    selected = tuple(
        sorted(path for path in tracked | untracked if _matches(path, patterns))
    )
    if not selected:
        return WorkspaceEvidence(digest=None, paths=())

    diff_bytes = _scoped_diff(repo, canonical_pin, selected)
    untracked_hashes = _untracked_hashes(repo, selected, untracked)

    # Reject concurrent writes rather than authenticating a torn workspace view.
    tracked_after, untracked_after = _workspace_paths(repo, canonical_pin)
    selected_after = tuple(
        sorted(
            path for path in tracked_after | untracked_after if _matches(path, patterns)
        )
    )
    if selected_after != selected:
        raise ValueError("workspace changed during evidence inspection")
    if _scoped_diff(repo, canonical_pin, selected) != diff_bytes:
        raise ValueError("workspace changed during evidence inspection")
    if _untracked_hashes(repo, selected, untracked_after) != untracked_hashes:
        raise ValueError("workspace changed during evidence inspection")

    hasher = hashlib.sha256()
    hasher.update(b"hermes-kanban-workspace-evidence-v2\0")
    hasher.update(canonical_pin.encode("ascii"))
    hasher.update(b"\0")
    for rel_path in selected:
        hasher.update(rel_path.encode("utf-8", errors="surrogateescape"))
        hasher.update(b"\0")

    hasher.update(diff_bytes)
    for rel_path in selected:
        if rel_path in untracked_hashes:
            hasher.update(b"untracked\0")
            hasher.update(rel_path.encode("utf-8", errors="surrogateescape"))
            hasher.update(b"\0")
            hasher.update(untracked_hashes[rel_path].encode("ascii"))
            hasher.update(b"\0")

    return WorkspaceEvidence(digest=hasher.hexdigest(), paths=selected)
