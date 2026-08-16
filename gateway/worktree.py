"""Gateway-owned git worktree allocation for isolated coding sessions."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

_METADATA_KEY = "hermes_worktree"


def _git(repo_root: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo_root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=15)


def _valid_worktree_info(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    required = {"path", "branch", "repo_root"}
    if not required.issubset(value):
        return False
    try:
        return Path(str(value["path"])).is_dir() and Path(str(value["repo_root"])).is_dir()
    except (OSError, RuntimeError, ValueError):
        return False


def ensure_session_worktree(
    session_entry: Any,
    *,
    enabled: bool,
    repo_root: str | None = None,
) -> Dict[str, str] | None:
    """Return the session's worktree, creating and persisting it when needed.

    The CLI owns the canonical Git worktree implementation. Gateway sessions
    use it here so branch naming, remote-base selection, stale-worktree pruning,
    and fail-closed behavior remain identical across Hermes surfaces.
    """
    if not enabled:
        return None

    metadata = getattr(session_entry, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
        session_entry.metadata = metadata

    existing = metadata.get(_METADATA_KEY)
    if _valid_worktree_info(existing):
        return {key: str(existing[key]) for key in ("path", "branch", "repo_root")}

    # Import lazily: gateway startup must not import the interactive CLI
    # machinery until a mutating gateway session actually needs isolation.
    from cli import _setup_worktree

    # Gateway systemd units intentionally run from HERMES_HOME, not the
    # project checkout. The caller therefore supplies the configured project
    # root; falling back to the CLI discovery path preserves standalone use.
    info = _setup_worktree(repo_root) if repo_root else _setup_worktree()
    if not info:
        raise RuntimeError(
            "worktree isolation is enabled, but Hermes could not create a "
            "gateway session worktree"
        )

    metadata[_METADATA_KEY] = {
        "path": str(info["path"]),
        "branch": str(info["branch"]),
        "repo_root": str(info["repo_root"]),
    }
    return metadata[_METADATA_KEY]


def cleanup_session_worktree(info: Dict[str, str] | None, *, reason: str = "session_end") -> Dict[str, Any]:
    """Idempotently release clean, zero-ahead gateway worktrees."""
    if not info:
        return {"state": "none", "reason": reason}
    path = str(info.get("path") or "")
    branch = str(info.get("branch") or "")
    repo_root = str(info.get("repo_root") or "")
    if not path or not branch or not repo_root or not Path(repo_root).is_dir():
        return {"state": "retained", "reason": "invalid_metadata"}
    if not Path(path).exists():
        return {"state": "released", "reason": "already_removed"}
    status = _git(repo_root, "-C", path, "status", "--porcelain")
    if status.returncode != 0:
        return {"state": "retained", "reason": "status_failed", "detail": status.stderr.strip()}
    if status.stdout.strip():
        return {"state": "retained", "reason": "dirty"}
    ahead = _git(repo_root, "-C", path, "rev-list", "--count", "origin/main..HEAD")
    if ahead.returncode != 0:
        return {"state": "retained", "reason": "ahead_check_failed", "detail": ahead.stderr.strip()}
    try:
        if int(ahead.stdout.strip() or "0") != 0:
            return {"state": "retained", "reason": "unpushed"}
    except ValueError:
        return {"state": "retained", "reason": "ahead_check_invalid"}
    unlock = _git(repo_root, "worktree", "unlock", path)
    if unlock.returncode != 0 and "not locked" not in unlock.stderr.lower():
        logger.warning("Could not unlock session worktree %s: %s", path, unlock.stderr.strip())
    remove = _git(repo_root, "worktree", "remove", path)
    if remove.returncode != 0:
        if not Path(path).exists():
            return {"state": "released", "reason": "removed_concurrently"}
        return {"state": "retained", "reason": "remove_failed", "detail": remove.stderr.strip()}
    delete = _git(repo_root, "branch", "-d", branch)
    _git(repo_root, "worktree", "prune")
    return {"state": "released", "reason": reason, "branch_deleted": delete.returncode == 0}
