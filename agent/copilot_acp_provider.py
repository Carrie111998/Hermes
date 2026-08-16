"""GitHub Copilot launch configuration for the shared ACP transport."""

from __future__ import annotations

import os
import shlex

PROVIDER = "copilot-acp"
MARKER_BASE_URL = "acp://copilot"

_DEPRECATION_REQUIRED = ("gh-copilot",)
_DEPRECATION_MARKERS = ("has been deprecated", "no commands will be executed")


def resolve_command() -> str:
    return (
        os.getenv("HERMES_COPILOT_ACP_COMMAND", "").strip()
        or os.getenv("COPILOT_CLI_PATH", "").strip()
        or "copilot"
    )


def resolve_args() -> list[str]:
    raw = os.getenv("HERMES_COPILOT_ACP_ARGS", "").strip()
    return shlex.split(raw) if raw else ["--acp", "--stdio"]


def is_deprecated_cli_message(stderr_text: str) -> bool:
    lower = stderr_text.lower()
    return any(value in lower for value in _DEPRECATION_REQUIRED) and any(
        marker in lower for marker in _DEPRECATION_MARKERS
    )


def deprecation_error(stderr_text: str) -> str:
    return (
        "Hermes ACP mode requires the NEW GitHub Copilot CLI "
        "(github.com/github/copilot-cli), but the binary it just spawned is "
        "the deprecated `gh copilot` extension.\n\n"
        "Install the new CLI:\n"
        "  npm install -g @github/copilot\n"
        "  # then verify with: copilot --help\n\n"
        "If `copilot` resolves to the new CLI but you still see this, point "
        "Hermes at it with HERMES_COPILOT_ACP_COMMAND.\n\n"
        f"Original error:\n{stderr_text}"
    )
