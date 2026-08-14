"""The bounded tool surface for the DevFlow coding agent.

These four tools are the ONLY way the agent touches anything. There is no
general shell and no network tool: the agent sees the repository and the work
request, and nothing else. Every rejection raises ``ToolError``, whose message
is handed back to the model as a tool result so it can correct course.
"""
from __future__ import annotations

import os
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from typing import List

from devflow_delegation.agent_policy import scrubbed_env
from devflow_delegation.allowlist import TargetConfig, path_allowed

MAX_READ_CHARS = 100_000
MAX_TEST_OUTPUT_CHARS = 20_000


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
            # item.relative_to(root) alone is lexical: a pattern such as
            # "../*.py" yields glob results with a literal ".." component,
            # which is still a lexical prefix-match against root and would
            # NOT raise here. Resolve first so escapes are caught the same
            # way _relative() catches them.
            rel = item.resolve().relative_to(root)
        except ValueError:
            continue
        results.append(rel.as_posix())
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
    if destination.exists() and not destination.is_file():
        raise ToolError(f"cannot write {rel}: an existing directory occupies that path")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(str(content), encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"failed to write {rel}: {exc}") from exc
    return f"wrote {rel} ({len(str(content))} chars)"


def run_tests(worktree: Path, target: TargetConfig, *, timeout_seconds: int = 600) -> str:
    """Run ONLY the target's allowlisted ``test_commands``.

    Takes no arguments from the model: this is what makes "fix it and make the
    tests pass" possible without ever handing the agent a shell.
    """
    commands = [cmd for cmd in target.test_commands if cmd]
    if not commands:
        raise ToolError("this target has no configured test commands")
    env = scrubbed_env(dict(os.environ))
    chunks: List[str] = []
    for command in commands:
        if isinstance(command, str):
            raise ToolError("test commands must be argv lists, not shell strings")
        argv = [str(part) for part in command]
        try:
            completed = subprocess.run(
                argv, cwd=str(worktree), env=env, shell=False, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"test command timed out after {timeout_seconds}s: {' '.join(argv)}")
        except OSError as exc:
            raise ToolError(f"test command could not start: {exc}") from exc
        status = "PASSED" if completed.returncode == 0 else "FAILED"
        body = (completed.stdout or "") + (completed.stderr or "")
        chunks.append(f"[{status}] {' '.join(argv)}\n{body}")
    output = "\n".join(chunks)
    if len(output) > MAX_TEST_OUTPUT_CHARS:
        return output[:MAX_TEST_OUTPUT_CHARS] + "\n… [output truncated]"
    return output


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file inside the working tree.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Repo-relative path."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the working tree matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string", "description": "Glob, e.g. src/**/*.py"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a file. Only paths inside the target's allowed scope are permitted.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repo-relative path."},
                    "content": {"type": "string", "description": "Full new file content."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run this repository's configured test suite. Takes no arguments.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]
