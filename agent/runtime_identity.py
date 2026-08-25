"""Runtime identity and provenance fingerprinting for Hermes Agent.

Provides deterministic identity verification across gateway endpoints,
API servers, and web dashboards without leaking host paths.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home


@dataclass(frozen=True)
class RuntimeIdentity:
    """Immutable snapshot of runtime identity."""

    pid: int
    process_start_time: Optional[int]
    hermes_home_digest: str
    release_tag: Optional[str] = None
    commit_sha: Optional[str] = None

    def to_dict(self, *, public: bool = False) -> dict[str, Any]:
        """Serialize identity. If public=True, redacts sensitive details."""
        if public:
            return {
                "pid": self.pid,
                "process_start_time": self.process_start_time,
                "hermes_home_digest": self.hermes_home_digest,
                "release_tag": self.release_tag,
                "commit_sha": self.commit_sha[:8] if self.commit_sha else None,
            }
        return {
            "pid": self.pid,
            "process_start_time": self.process_start_time,
            "hermes_home_digest": self.hermes_home_digest,
            "release_tag": self.release_tag,
            "commit_sha": self.commit_sha,
        }


def _get_process_start_time(pid: int) -> Optional[int]:
    """Retrieve process birth time directly."""
    if not pid or pid <= 0:
        return None
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        return int(stat_path.read_text(encoding="utf-8").split()[21])
    except Exception:
        pass
    try:
        import psutil
        return int(round(psutil.Process(pid).create_time() * 100))
    except Exception:
        return None


def _resolve_commit_sha() -> Optional[str]:
    """Try reading git commit SHA if in a git repo."""
    try:
        head_file = Path(__file__).parent.parent / ".git" / "HEAD"
        if head_file.exists():
            content = head_file.read_text(encoding="utf-8").strip()
            if content.startswith("ref: "):
                ref_path = Path(__file__).parent.parent / ".git" / content[5:]
                if ref_path.exists():
                    return ref_path.read_text(encoding="utf-8").strip()
            elif len(content) == 40:
                return content
    except Exception:
        pass
    return os.environ.get("HERMES_GIT_COMMIT", None)


def get_runtime_identity(*, public: bool = False) -> dict[str, Any]:
    """Compute and return the current runtime identity dictionary."""
    pid = os.getpid()
    start_time = _get_process_start_time(pid)
    home_path = str(get_hermes_home().resolve())
    home_digest = hashlib.sha256(home_path.encode("utf-8")).hexdigest()[:16]
    release_tag = os.environ.get("HERMES_RELEASE_TAG", None)
    commit_sha = _resolve_commit_sha()

    ident = RuntimeIdentity(
        pid=pid,
        process_start_time=start_time,
        hermes_home_digest=home_digest,
        release_tag=release_tag,
        commit_sha=commit_sha,
    )
    return ident.to_dict(public=public)
