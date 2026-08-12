"""Deterministic Stage-2 worktree validation for the DevFlow Delegation Plane.

Validation runs only allowlist-owned argv vectors in an isolated worktree. It
never reads commands or environment values from a work request.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence

from devflow_delegation.allowlist import TargetConfig
from hermes_cli._subprocess_compat import run_text_capture

MAX_OUTPUT_CHARS = 12_000


@dataclass(frozen=True)
class CommandResult:
    command: str
    category: str
    status: str  # passed | failed | timed_out | invalid
    returncode: int | None
    stdout: str
    stderr: str
    evidence: str


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    commands: Sequence[CommandResult]

    @property
    def evidence_refs(self) -> List[str]:
        return [result.evidence for result in self.commands]


def _bounded(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value or "")
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + "\n… [output truncated]"


def _commands(target: TargetConfig) -> List[tuple[str, tuple[str, ...]]]:
    commands: List[tuple[str, tuple[str, ...]]] = []
    for category, configured in (
        ("test", target.test_commands),
        ("lint", target.lint_commands),
        ("typecheck", target.typecheck_commands),
        ("build", target.build_commands),
    ):
        for command in configured:
            # Existing allowlists have string commands. They are intentionally
            # invalid for Stage 2 rather than handed to a shell.
            if isinstance(command, str):
                commands.append((category, ()))
            else:
                commands.append((category, tuple(command)))
    return commands


def _expected_success(result: subprocess.CompletedProcess[str]) -> bool:
    """A zero exit code without observable output is not a green check."""
    return result.returncode == 0 and bool((result.stdout or result.stderr).strip())


def validate_worktree(
    worktree_path: Path,
    target: TargetConfig,
    *,
    env: dict[str, str] | None = None,
) -> ValidationResult:
    worktree_path = Path(worktree_path).resolve()
    if not worktree_path.is_dir():
        return ValidationResult(False, (
            CommandResult("", "setup", "invalid", None, "", "worktree directory is missing", "validation:invalid-worktree"),
        ))

    run_env = os.environ.copy()
    run_env.update({"GIT_TERMINAL_PROMPT": "0"})
    if env:
        run_env.update(env)

    configured = _commands(target)
    if not configured:
        return ValidationResult(False, (
            CommandResult("", "setup", "invalid", None, "", "target has no configured validation commands", "validation:no-commands"),
        ))

    results: List[CommandResult] = []
    for category, argv in configured:
        if not argv:
            results.append(CommandResult(
                "", category, "invalid", None, "", "validation commands must be argv vectors", f"validation:{category}:invalid"
            ))
            return ValidationResult(False, tuple(results))
        try:
            # run_text_capture, not capture_output=True: `argv` is an
            # allowlist-owned *build/test* command (pytest, npm test), so a
            # grandchild is the norm rather than the exception. On Windows a
            # grandchild inherits the capture pipes and holds their write end
            # open, so `timeout` never fires — subprocess.run kills only the
            # direct child, then blocks re-draining a pipe that can no longer
            # reach EOF, and a wedged test command hangs the tick forever.
            # Capturing into temp files removes the pipes entirely.
            completed = run_text_capture(
                list(argv), cwd=str(worktree_path), env=run_env,
                timeout=target.command_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            results.append(CommandResult(
                " ".join(argv), category, "timed_out", None, _bounded(exc.stdout), _bounded(exc.stderr),
                f"validation:{category}:timeout",
            ))
            return ValidationResult(False, tuple(results))
        except OSError as exc:
            results.append(CommandResult(
                " ".join(argv), category, "failed", None, "", _bounded(str(exc)), f"validation:{category}:oserror",
            ))
            return ValidationResult(False, tuple(results))

        status = "passed" if _expected_success(completed) else "failed"
        results.append(CommandResult(
            " ".join(argv), category, status, completed.returncode,
            _bounded(completed.stdout), _bounded(completed.stderr), f"validation:{category}:{status}",
        ))
        if status != "passed":
            return ValidationResult(False, tuple(results))

    required = set(target.required_checks)
    observed = {result.category for result in results if result.status == "passed"}
    missing = required - observed
    if missing:
        results.append(CommandResult(
            "", "setup", "invalid", None, "", f"required checks missing: {sorted(missing)}", "validation:required-checks-missing",
        ))
        return ValidationResult(False, tuple(results))
    return ValidationResult(True, tuple(results))
