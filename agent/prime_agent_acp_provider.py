"""Prime Agent launch configuration for the shared ACP transport."""

from __future__ import annotations

PROVIDER = "prime-agent"
MARKER_BASE_URL = "acp://prime-agent"


def resolve_command() -> str:
    return "prime-agent"


def resolve_args() -> list[str]:
    return ["--mode", "acp"]
