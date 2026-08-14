"""The bounded tool surface for the DevFlow coding agent.

These four tools are the ONLY way the agent touches anything. There is no
general shell and no network tool: the agent sees the repository and the work
request, and nothing else. Every rejection raises ``ToolError``, whose message
is handed back to the model as a tool result so it can correct course.
"""
from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import List

from devflow_delegation.allowlist import TargetConfig, path_allowed

MAX_READ_CHARS = 100_000


class ToolError(Exception):
    """A tool refused the request. Recoverable: returned to the model."""


def _relative(worktree: Path, path: str) -> str:
    """Resolve ``path`` inside ``worktree`` or raise. Rejects traversal/absolute."""
    root = Path(worktree).resolve()
    candidate = (root / str(path or "")).resolve()
    try:
        rel = candidate.relative_to(root)
    except ValueError as exc:
        raise ToolError(f"path escapes the worktree: {path}") from exc
    text = rel.as_posix()
    if not text or text == ".":
        raise ToolError("path must name a file")
    return text


def _denied(target: TargetConfig, rel: str) -> bool:
    return any(
        fnmatch(rel, glob) or (glob.startswith("**/") and fnmatch(rel, glob[3:]))
        for glob in target.denied_globs
    )


def read_file(worktree: Path, target: TargetConfig, path: str) -> str:
    rel = _relative(worktree, path)
    if _denied(target, rel):
        raise ToolError(f"reading a denied path is not permitted: {rel}")
    target_path = Path(worktree).resolve() / rel
    if not target_path.is_file():
        raise ToolError(f"no such file: {rel}")
    body = target_path.read_text(encoding="utf-8", errors="replace")
    if len(body) > MAX_READ_CHARS:
        return body[:MAX_READ_CHARS] + "\n… [truncated]"
    return body


def list_files(worktree: Path, pattern: str = "**/*") -> List[str]:
    root = Path(worktree).resolve()
    results = []
    for item in sorted(root.glob(str(pattern or "**/*"))):
        if not item.is_file():
            continue
        try:
            results.append(item.relative_to(root).as_posix())
        except ValueError:
            continue
    return results


def write_file(worktree: Path, target: TargetConfig, path: str, content: str) -> str:
    rel = _relative(worktree, path)
    if _denied(target, rel):
        raise ToolError(f"writing a denied path is not permitted: {rel}")
    if not path_allowed(target, rel):
        raise ToolError(
            f"{rel} is outside this target's allowed scope; you may only write to: "
            + ", ".join(target.allowed_globs)
        )
    destination = Path(worktree).resolve() / rel
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(str(content), encoding="utf-8")
    return f"wrote {rel} ({len(str(content))} chars)"
