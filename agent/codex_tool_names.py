"""Codex-compatible names for projected and replayed function calls."""

from __future__ import annotations

import re


_INVALID_CODEX_TOOL_NAME = re.compile(r"[^A-Za-z0-9_-]")


def normalize_codex_tool_name(name: object, *, default: str = "unknown") -> str:
    """Replace characters rejected by the Responses function-name schema."""
    normalized = _INVALID_CODEX_TOOL_NAME.sub("_", str(name or "")).strip("_")
    return normalized or default
