"""The bounded tool surface for the DevFlow coding agent.

These four tools are the ONLY way the agent touches anything. There is no
general shell and no network tool: the agent sees the repository and the work
request, and nothing else. Every rejection raises ``ToolError``, whose message
is handed back to the model as a tool result so it can correct course.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional

from devflow_delegation.agent_policy import scrubbed_env
from devflow_delegation.allowlist import TargetConfig, _glob_matches, path_allowed

MAX_READ_CHARS = 100_000
MAX_TEST_OUTPUT_CHARS = 20_000
# pytest (and most test runners) put the failure summary at the END of the
# output; collection/setup noise is at the start. A head-only truncation of a
# large failing suite hands the agent collection noise and nothing about what
# actually broke, so it burns iterations blind. Keep both ends instead.
MAX_TEST_OUTPUT_HEAD_CHARS = 5_000
MAX_TEST_OUTPUT_TAIL_CHARS = 15_000
MAX_LIST_FILES_ENTRIES = 500


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
    # Delegates to allowlist._glob_matches (the same helper path_allowed uses)
    # rather than re-deriving the "**/"-root-form match locally, so the two
    # can never silently drift out of agreement (D3).
    return any(_glob_matches(rel, glob) for glob in target.denied_globs)


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


def list_files(worktree: Path, target: TargetConfig, pattern: str = "**/*") -> List[str]:
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
        rel_str = rel.as_posix()
        # read_file and write_file both refuse denied_globs; listing must not
        # leak those filenames either, even though it never exposes content (D4).
        if _denied(target, rel_str):
            continue
        results.append(rel_str)
    if len(results) > MAX_LIST_FILES_ENTRIES:
        total = len(results)
        results = results[:MAX_LIST_FILES_ENTRIES]
        results.append(
            f"… [truncated: showing {MAX_LIST_FILES_ENTRIES} of {total} files; narrow the pattern]"
        )
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


def run_tests(worktree: Path, target: TargetConfig, *, timeout_seconds: Optional[int] = None) -> str:
    """Run ONLY the target's allowlisted ``test_commands``.

    Takes no arguments from the model: this is what makes "fix it and make the
    tests pass" possible without ever handing the agent a shell.

    ``timeout_seconds`` defaults to the target's own ``command_timeout_seconds``
    (D2) rather than a value hard-coded here, so a target's configured ceiling
    is actually honored. An explicit ``timeout_seconds`` argument still wins
    over the target's value -- callers that need a different bound (tests,
    a future caller with its own policy) are not overridden by the target.
    """
    commands = [cmd for cmd in target.test_commands if cmd]
    if not commands:
        raise ToolError("this target has no configured test commands")
    effective_timeout = (
        target.command_timeout_seconds if timeout_seconds is None else timeout_seconds
    )
    env = scrubbed_env(dict(os.environ))
    chunks: List[str] = []
    for command in commands:
        if isinstance(command, str):
            raise ToolError("test commands must be argv lists, not shell strings")
        argv = [str(part) for part in command]
        try:
            completed = subprocess.run(
                argv, cwd=str(worktree), env=env, shell=False, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"test command timed out after {effective_timeout}s: {' '.join(argv)}")
        except OSError as exc:
            raise ToolError(f"test command could not start: {exc}") from exc
        status = "PASSED" if completed.returncode == 0 else "FAILED"
        body = (completed.stdout or "") + (completed.stderr or "")
        chunks.append(f"[{status}] {' '.join(argv)}\n{body}")
    output = "\n".join(chunks)
    if len(output) > MAX_TEST_OUTPUT_CHARS:
        head = output[:MAX_TEST_OUTPUT_HEAD_CHARS]
        tail = output[-MAX_TEST_OUTPUT_TAIL_CHARS:]
        omitted = len(output) - MAX_TEST_OUTPUT_HEAD_CHARS - MAX_TEST_OUTPUT_TAIL_CHARS
        return f"{head}\n… [output truncated, {omitted} chars omitted] …\n{tail}"
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
