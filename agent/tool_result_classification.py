"""Shared helpers for classifying tool result payloads."""

from __future__ import annotations

import json
import re
from typing import Any


FILE_MUTATING_TOOL_NAMES = frozenset({"write_file", "patch"})


# Tools whose interrupted/dangling execution is safe to discard because they
# cannot mutate either external state or Hermes session state. Unknown/plugin/
# MCP tools stay effect-capable by default.
NO_EFFECT_TOOL_NAMES = frozenset({
    "read_file", "search_files", "session_search", "skill_view", "skills_list",
    "web_extract", "web_search", "vision_analyze", "browser_snapshot",
    "browser_get_images", "browser_console", "read_terminal",
})


def tool_may_have_side_effect(tool_name: str) -> bool:
    return tool_name not in NO_EFFECT_TOOL_NAMES


def file_mutation_result_landed(tool_name: str, result: Any) -> bool:
    """Return True when a file mutation result proves the write landed."""
    if tool_name not in FILE_MUTATING_TOOL_NAMES or not isinstance(result, str):
        return False
    try:
        data = json.loads(result.strip())
    except Exception:
        return False
    if not isinstance(data, dict) or data.get("error"):
        return False
    if tool_name == "write_file":
        return "bytes_written" in data
    if tool_name == "patch":
        return data.get("success") is True
    return False


_TERMINAL_PROGRESS_COMMAND_RE = re.compile(
    r"(?:^|&&\s*|\|\|\s*|[;|]\s*)"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:"
    r"scripts/run_tests\.sh|pytest|uv\s+run\s+pytest|"
    r"npm\s+(?:test|run\s+(?:test|check|build|lint|typecheck))|"
    r"pnpm\s+(?:test|run\s+(?:test|check|build|lint|typecheck))|"
    r"yarn\s+(?:test|run\s+(?:test|check|build|lint|typecheck))|"
    r"cargo\s+(?:test|build|check|clippy)|go\s+(?:test|build)|"
    r"git\s+(?:commit|merge|revert|cherry-pick)"
    r")(?:\s|$)",
    re.IGNORECASE,
)


def tool_result_verified_progress(
    tool_name: str,
    args: dict[str, Any] | None,
    result: Any,
    *,
    failed: bool,
) -> bool:
    """Return True only when a production result proves forward progress.

    File mutations use their structured success contracts. Terminal commands
    are accepted only for successful build/test/check or version-control
    milestones, rather than treating every zero-exit shell command as progress.
    Replay/fingerprint deduplication remains the telemetry layer's job.
    """

    if failed:
        return False
    if file_mutation_result_landed(tool_name, result):
        return True
    if not isinstance(args, dict) or not isinstance(result, str):
        return False
    if tool_name == "apply_patch":
        return bool(args.get("changes")) and result.startswith(
            "apply_patch status="
        )
    if tool_name == "exec_command":
        command = str(args.get("command") or "").strip()
        return bool(command and _TERMINAL_PROGRESS_COMMAND_RE.search(command))
    if tool_name != "terminal":
        return False
    try:
        payload = json.loads(result.strip())
    except Exception:
        return False
    if not isinstance(payload, dict) or payload.get("exit_code") != 0:
        return False
    command = str(args.get("command") or "").strip()
    return bool(command and _TERMINAL_PROGRESS_COMMAND_RE.search(command))
