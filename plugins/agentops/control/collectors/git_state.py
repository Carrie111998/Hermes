"""Direct, bounded Git metadata observation with canonical ref containment."""

from __future__ import annotations

import configparser
import hashlib
import os
import re
import stat
import threading
import time
from pathlib import Path

from plugins.agentops.control.collectors.base import failed_batch
from plugins.agentops.control.observer_models import (
    CollectionBatch,
    CollectorHealth,
    LogCursor,
    RawSignal,
    Target,
    asset_source_id,
    target_allows_asset,
    utc_now,
)
from plugins.agentops.control.redaction import redact_signal


_OBJECT_ID = re.compile(r"^[0-9a-f]{40,64}$", re.I)
_REF_NAME = re.compile(r"^refs/(?:heads|tags|remotes)/[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
_GITDIR = re.compile(r"^gitdir:\s*(?P<path>[^\r\n]+)\s*$", re.I)
_MAX_READ_BYTES = 1024 * 1024


def _safe_directory(path: Path) -> Path:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OSError("directory rejected")
    return path.resolve(strict=True)


def _contained_path(root: Path, relative: str) -> Path:
    if not relative or relative.startswith("/"):
        raise OSError("path rejected")
    parts = Path(relative).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise OSError("path rejected")
    candidate = root.joinpath(*parts)
    canonical_root = root.resolve(strict=True)
    canonical_candidate = candidate.resolve(strict=False)
    try:
        canonical_candidate.relative_to(canonical_root)
    except ValueError as exc:
        raise OSError("path rejected") from exc
    current = canonical_root
    for part in parts:
        current = current / part
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise OSError("path rejected")
    return candidate


def _read_regular_text(path: Path, *, root: Path | None = None, max_bytes: int = _MAX_READ_BYTES) -> str:
    if root is not None:
        try:
            relative = os.fspath(path.relative_to(root))
        except ValueError as exc:
            raise OSError("path rejected") from exc
        path = _contained_path(root, relative)
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
        raise OSError("path rejected")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        return os.read(descriptor, max_bytes + 1).decode("utf-8")
    finally:
        os.close(descriptor)


class GitStateCollector:
    name = "git_state"

    def __init__(self, repository: Path, *, min_interval_seconds: float = 0.0) -> None:
        if min_interval_seconds < 0:
            raise ValueError("invalid collector rate")
        self.repository = Path(repository)
        self.source_id = asset_source_id(self.repository)
        self.min_interval_seconds = min_interval_seconds
        self._last_collection = 0.0
        self._rate_lock = threading.Lock()

    def _layout(self) -> tuple[Path, Path]:
        repository = _safe_directory(self.repository)
        dotgit = self.repository / ".git"
        metadata = dotgit.lstat()
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            git_dir = _safe_directory(dotgit)
        elif stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            content = _read_regular_text(dotgit).strip()
            match = _GITDIR.fullmatch(content)
            if match is None:
                raise OSError("gitdir rejected")
            configured = Path(match.group("path")).expanduser()
            candidate = configured if configured.is_absolute() else self.repository / configured
            git_dir = _safe_directory(candidate)
        else:
            raise OSError("git directory rejected")
        common_dir = git_dir
        commondir_file = git_dir / "commondir"
        if commondir_file.exists():
            relative = _read_regular_text(commondir_file, root=git_dir).strip()
            if not relative or Path(relative).is_absolute():
                raise OSError("common directory rejected")
            common_dir = _safe_directory(git_dir / relative)
        return git_dir, common_dir

    @staticmethod
    def _read_packed_ref(common_dir: Path, reference: str) -> str | None:
        packed = common_dir / "packed-refs"
        if not packed.exists():
            return None
        for line in _read_regular_text(packed, root=common_dir).splitlines():
            if not line or line.startswith("#") or line.startswith("^"):
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1] == reference and _OBJECT_ID.fullmatch(parts[0]):
                return parts[0]
        return None

    @classmethod
    def _resolve_ref(cls, common_dir: Path, reference: str) -> str:
        if not _REF_NAME.fullmatch(reference):
            raise OSError("invalid reference")
        try:
            object_id = _read_regular_text(common_dir / reference, root=common_dir).strip()
        except OSError:
            object_id = cls._read_packed_ref(common_dir, reference)
            if object_id is None:
                raise
        if not _OBJECT_ID.fullmatch(object_id):
            raise OSError("invalid object id")
        return object_id

    def _head(self, git_dir: Path, common_dir: Path) -> tuple[str, str | None]:
        content = _read_regular_text(git_dir / "HEAD", root=git_dir).strip()
        if content.startswith("ref: "):
            reference = content[5:].strip()
            return self._resolve_ref(common_dir, reference), reference
        if not _OBJECT_ID.fullmatch(content):
            raise OSError("invalid object id")
        return content, None

    @staticmethod
    def _upstream(common_dir: Path, reference: str | None) -> str | None:
        if reference is None or not reference.startswith("refs/heads/"):
            return None
        try:
            parser = configparser.ConfigParser()
            parser.read_string(_read_regular_text(common_dir / "config", root=common_dir, max_bytes=128 * 1024))
            branch = reference.removeprefix("refs/heads/")
            section = f'branch "{branch}"'
            remote = parser.get(section, "remote", fallback=None)
            merge = parser.get(section, "merge", fallback=None)
            if remote and merge and _REF_NAME.fullmatch(merge):
                return f"{remote}:{merge}"
        except (OSError, configparser.Error):
            return None
        return None

    def collect(self, target: Target, cursor: LogCursor | None = None) -> CollectionBatch:
        if not target_allows_asset(target, self.repository):
            return failed_batch(target, self.name, "asset_unbound", source_id=self.source_id)
        with self._rate_lock:
            now = time.monotonic()
            if now - self._last_collection < self.min_interval_seconds:
                return failed_batch(target, self.name, "collector_rate_limited", source_id=self.source_id)
            self._last_collection = now
        try:
            git_dir, common_dir = self._layout()
            object_id, reference = self._head(git_dir, common_dir)
        except Exception:
            return failed_batch(target, self.name, "git_read_failed", source_id=self.source_id)
        observed_at = utc_now()
        signal = redact_signal(
            RawSignal(
                target_id=target.target_id,
                collector=self.name,
                signal_type="git.state",
                observed_at=observed_at,
                payload={
                    "head": object_id,
                    "reference": reference,
                    "upstream": self._upstream(common_dir, reference),
                    "dirty": "unknown",
                    "repository_fingerprint": self.source_id,
                },
            )
        )
        return CollectionBatch(
            target_id=target.target_id,
            collector=self.name,
            collected_at=observed_at,
            signals=(signal,),
            health=CollectorHealth(healthy=True),
            source_id=self.source_id,
        )
