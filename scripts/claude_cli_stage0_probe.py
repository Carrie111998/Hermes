#!/usr/bin/env python3
"""Sanitized live oracle for the Claude CLI Stage-0 subprocess contract.

The managed wrapper is a transparent launcher: the harness appends the official
Claude executable and fixed Stage-0 argv, never invokes a shell, and emits only
bounded counters/booleans. Prompt text, response text, environment values,
session identifiers, PIDs, and raw stderr are never printed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.transports.claude_cli import (  # noqa: E402
    CLAUDE_CLI_FORBIDDEN_FLAGS,
    CLAUDE_CLI_MIN_VERSION,
    ClaudeCLIClient,
    ClaudeCLIError,
    ClaudeCLITurnResult,
    build_claude_cli_env,
    parse_claude_cli_help,
    parse_claude_cli_version,
)

_CAPABILITY_TIMEOUT_SECONDS = 10.0
_FIRST_EVENT_TIMEOUT_SECONDS = 30.0
_IDLE_TIMEOUT_SECONDS = 60.0
_TOTAL_TIMEOUT_SECONDS = 300.0
_CLEAN_EXIT_GRACE_SECONDS = 1.0
_CLEAN_EXIT_TERMINATE_SECONDS = 2.0
_BOUNDED_EXIT_SECONDS = 8.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the official Claude CLI through an approved managed-policy "
            "wrapper and print sanitized Stage-0 evidence."
        )
    )
    parser.add_argument(
        "--managed-wrapper",
        required=True,
        help="Transparent policy-wrapper executable (no shell command string).",
    )
    parser.add_argument(
        "--wrapper-arg",
        action="append",
        default=[],
        help="Non-secret wrapper argument; repeat to preserve argument boundaries.",
    )
    parser.add_argument(
        "--claude-executable",
        default="claude",
        help="Official Claude CLI executable passed to the wrapper (default: claude).",
    )
    return parser


def _boundary_prefix(args: argparse.Namespace) -> list[str]:
    return [args.managed_wrapper, *args.wrapper_arg, args.claude_executable]


def _check_capabilities(prefix: Sequence[str], env: dict[str, str]) -> str:
    completed = []
    for flag in ("--version", "--help"):
        try:
            proc = subprocess.run(
                [*prefix, flag],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_CAPABILITY_TIMEOUT_SECONDS,
                env=env,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClaudeCLIError(
                "managed Claude CLI capability check could not complete",
                category="unavailable",
            ) from exc
        completed.append(proc)
    if any(proc.returncode != 0 for proc in completed):
        raise ClaudeCLIError(
            "managed Claude CLI capability check failed", category="unavailable"
        )

    version_text = completed[0].stdout.decode("utf-8", "replace")
    help_text = completed[1].stdout.decode("utf-8", "replace")
    version = parse_claude_cli_version(version_text)
    if version is None or version < CLAUDE_CLI_MIN_VERSION:
        raise ClaudeCLIError(
            "managed Claude CLI version is below the Stage-0 minimum",
            category="unavailable",
        )
    if not parse_claude_cli_help(help_text).stage0_ready:
        raise ClaudeCLIError(
            "managed Claude CLI is missing required Stage-0 capabilities",
            category="unavailable",
        )
    return ".".join(str(part) for part in version)


def evaluate_probe(
    *,
    version: str,
    first: ClaudeCLITurnResult,
    second: ClaudeCLITurnResult,
    first_pid: int | None,
    second_pid: int | None,
    spawned_argv: Sequence[str],
    returncode: int | None,
    cleanup_seconds: float,
) -> dict[str, Any]:
    """Evaluate only sanitized observations; never include content or identifiers."""
    turns = (first, second)
    replay_acknowledgements = sum(
        turn.event_counts.get("user", 0) for turn in turns
    )
    results = sum(turn.event_counts.get("result", 0) for turn in turns)
    tool_events = sum(
        count
        for turn in turns
        for event_type, count in turn.event_counts.items()
        if "tool" in event_type.lower()
    )
    forbidden_flags = sum(
        1 for token in spawned_argv if token in CLAUDE_CLI_FORBIDDEN_FLAGS
    )
    same_child_pid = first_pid is not None and first_pid == second_pid
    session_consistent = bool(
        first.session_id and first.session_id == second.session_id
    )
    process_generations = len({first.generation, second.generation})
    init_tools_empty = first.event_counts.get("system", 0) >= 1
    clean_exit = returncode == 0
    bounded_exit = cleanup_seconds <= _BOUNDED_EXIT_SECONDS

    passed = all(
        (
            same_child_pid,
            session_consistent,
            process_generations == 1,
            replay_acknowledgements == 2,
            results == 2,
            init_tools_empty,
            tool_events == 0,
            forbidden_flags == 0,
            clean_exit,
            bounded_exit,
            not first.interrupted,
            not second.interrupted,
        )
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "version": version,
        "same_child_pid": same_child_pid,
        "session_consistent": session_consistent,
        "process_generations": process_generations,
        "replay_acknowledgements": replay_acknowledgements,
        "results": results,
        "init_tools_empty": init_tools_empty,
        "tool_events": tool_events,
        "forbidden_flags": forbidden_flags,
        "clean_exit": clean_exit,
        "bounded_exit": bounded_exit,
    }


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    env = build_claude_cli_env()
    prefix = _boundary_prefix(args)
    version = _check_capabilities(prefix, env)
    spawned: list[list[str]] = []

    def spawn(argv: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        boundary_argv = [*prefix, *list(argv)[1:]]
        spawned.append(boundary_argv)
        return subprocess.Popen(boundary_argv, **kwargs)

    client = ClaudeCLIClient(env=env, popen_factory=spawn)
    proc: subprocess.Popen[bytes] | None = None
    try:
        first = client.run_turn(
            "Reply with the single word one.",
            first_event_timeout=_FIRST_EVENT_TIMEOUT_SECONDS,
            idle_timeout=_IDLE_TIMEOUT_SECONDS,
            total_timeout=_TOTAL_TIMEOUT_SECONDS,
        )
        first_pid = client.pid
        second = client.run_turn(
            "Reply with the single word two.",
            first_event_timeout=_FIRST_EVENT_TIMEOUT_SECONDS,
            idle_timeout=_IDLE_TIMEOUT_SECONDS,
            total_timeout=_TOTAL_TIMEOUT_SECONDS,
        )
        second_pid = client.pid
        proc = client._proc
    finally:
        cleanup_started = time.monotonic()
        client.close(
            grace=_CLEAN_EXIT_GRACE_SECONDS,
            terminate_timeout=_CLEAN_EXIT_TERMINATE_SECONDS,
        )
        cleanup_seconds = time.monotonic() - cleanup_started

    return evaluate_probe(
        version=version,
        first=first,
        second=second,
        first_pid=first_pid,
        second_pid=second_pid,
        spawned_argv=spawned[0] if spawned else (),
        returncode=getattr(proc, "returncode", None),
        cleanup_seconds=cleanup_seconds,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_probe(args)
    except ClaudeCLIError as exc:
        result = {
            "status": "FAIL",
            "error_category": exc.category,
            "error": str(exc),
        }
    except Exception:
        result = {
            "status": "FAIL",
            "error_category": "harness",
            "error": "Stage-0 probe could not complete",
        }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
