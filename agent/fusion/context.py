"""Repository/context helpers for Fusion v2."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class FusionContext:
    repo_root: str | None
    cwd: str
    repo_guard_available: bool
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "cwd": self.cwd,
            "repo_guard_available": self.repo_guard_available,
            "notes": list(self.notes),
        }


def _run_git(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def resolve_repo_root(start_path: str | Path | None = None) -> Path | None:
    start = Path(start_path or os.environ.get("TERMINAL_CWD") or os.getcwd()).expanduser()
    try:
        start = start.resolve()
    except OSError:
        start = Path(os.getcwd()).resolve()
    proc = _run_git(["rev-parse", "--show-toplevel"], start)
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    if not root:
        return None
    try:
        return Path(root).resolve()
    except OSError:
        return None


def is_path_inside(root: str | Path, candidate: str | Path) -> bool:
    root_path = Path(root).expanduser().resolve()
    candidate_path = Path(candidate).expanduser().resolve()
    return candidate_path == root_path or candidate_path.is_relative_to(root_path)


def resolve_repo_path(repo_root: str | Path, path: str | Path) -> Path:
    root = Path(repo_root).expanduser().resolve()
    raw = Path(path).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    resolved = candidate.resolve()
    if not (resolved == root or resolved.is_relative_to(root)):
        raise ValueError(f"Path escapes Fusion repository scope: {path}")
    return resolved


def build_context_packet(repo_path: str | None = None) -> FusionContext:
    cwd = str(Path(repo_path or os.environ.get("TERMINAL_CWD") or os.getcwd()).expanduser())
    notes: list[str] = []
    repo_root = resolve_repo_root(repo_path)
    if repo_root is None:
        notes.append("Repository root could not be resolved; repo mutation guard is unavailable.")
        return FusionContext(
            repo_root=None,
            cwd=str(Path(cwd).expanduser()),
            repo_guard_available=False,
            notes=notes,
        )
    return FusionContext(
        repo_root=str(repo_root),
        cwd=str(Path(cwd).expanduser()),
        repo_guard_available=True,
        notes=notes,
    )
