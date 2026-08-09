"""Direct, conservative read-only Git metadata observation."""

from __future__ import annotations

import configparser
import hashlib
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path

from plugins.agentops.control.collectors.base import failed_batch
from plugins.agentops.control.observer_models import (
    CollectionBatch,
    CollectorHealth,
    LogCursor,
    RawSignal,
    Target,
    utc_now,
)
from plugins.agentops.control.redaction import redact_signal


_OBJECT_ID = re.compile(r"^[0-9a-f]{40,64}$", re.I)
_REF_NAME = re.compile(r"^refs/[A-Za-z0-9._/-]+$")


def _read_regular_text(path: Path) -> str:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OSError("path rejected")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        return os.read(descriptor, 1024 * 1024).decode("utf-8")
    finally:
        os.close(descriptor)


class GitStateCollector:
    name = "git_state"

    def __init__(self, repository: Path, *, dirty_probe: Callable[[Path], bool | None] | None = None) -> None:
        self.repository = Path(repository)
        self._dirty_probe = dirty_probe

    def _head(self, git_dir: Path) -> tuple[str, str | None]:
        content = _read_regular_text(git_dir / "HEAD").strip()
        if content.startswith("ref: "):
            reference = content[5:].strip()
            if not _REF_NAME.fullmatch(reference):
                raise OSError("invalid reference")
            object_id = _read_regular_text(git_dir / reference).strip()
            if not _OBJECT_ID.fullmatch(object_id):
                raise OSError("invalid object id")
            return object_id, reference
        if not _OBJECT_ID.fullmatch(content):
            raise OSError("invalid object id")
        return content, None

    @staticmethod
    def _upstream(git_dir: Path, reference: str | None) -> str | None:
        if reference is None:
            return None
        try:
            parser = configparser.ConfigParser()
            parser.read_string(_read_regular_text(git_dir / "config"))
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
        git_dir = self.repository / ".git"
        try:
            metadata = git_dir.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                return failed_batch(target, self.name, "git_directory_rejected")
            object_id, reference = self._head(git_dir)
            dirty = "unknown"
            if self._dirty_probe is not None:
                reported = self._dirty_probe(self.repository)
                dirty = "dirty" if reported is True else "clean" if reported is False else "unknown"
        except Exception:
            return failed_batch(target, self.name, "git_read_failed")
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
                    "upstream": self._upstream(git_dir, reference),
                    "dirty": dirty,
                    "repository_fingerprint": "sha256:" + hashlib.sha256(os.fspath(self.repository).encode()).hexdigest(),
                },
            )
        )
        return CollectionBatch(
            target_id=target.target_id,
            collector=self.name,
            collected_at=observed_at,
            signals=(signal,),
            health=CollectorHealth(healthy=True),
        )
