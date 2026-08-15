"""Canonical marker stamping for isolated Hermes release snapshots."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

CANONICAL_RELEASE_MARKER = ".hermes-release-sha"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def stamp_release_marker(release_path: Path, source_sha: str) -> Path:
    """Atomically stamp the canonical source SHA into a release directory."""
    source_sha = source_sha.strip().lower()
    if not _SHA_RE.fullmatch(source_sha):
        raise ValueError("source_sha must be a full 40-character Git revision")
    release_path = release_path.resolve()
    if not release_path.is_dir():
        raise FileNotFoundError(release_path)
    marker = release_path / CANONICAL_RELEASE_MARKER
    fd, temp_name = tempfile.mkstemp(prefix=f".{marker.name}.", dir=release_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(source_sha + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, marker)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return marker
