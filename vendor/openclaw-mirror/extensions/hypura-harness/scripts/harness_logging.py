"""Stable log locations shared by Hypura Harness modules."""

from __future__ import annotations

import os
from pathlib import Path


def _profile_home() -> Path:
    configured_home = os.getenv("HERMES_HOME", "").strip()
    return (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".hermes"
    )


def _safe_parts(parts: tuple[str, ...]) -> tuple[str, ...]:
    if not parts or any(
        not part or part in {".", ".."} or Path(part).name != part for part in parts
    ):
        raise ValueError("Harness state path must contain filename components only")
    return parts


def harness_log_path(filename: str) -> Path:
    """Return a writable, profile-aware path for a harness log file."""
    if Path(filename).name != filename:
        raise ValueError("Harness log filename must not contain a directory")

    logs_dir = _profile_home() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / filename


def harness_state_dir(*parts: str) -> Path:
    """Return a writable directory under the current Hermes profile."""
    state_dir = _profile_home() / "harness"
    if parts:
        state_dir = state_dir.joinpath(*_safe_parts(parts))
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def harness_state_path(*parts: str) -> Path:
    """Return a writable file path under the current Hermes profile."""
    safe_parts = _safe_parts(parts)
    parent = harness_state_dir(*safe_parts[:-1])
    return parent / safe_parts[-1]
