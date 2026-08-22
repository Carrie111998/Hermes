"""Fail-closed settings for the optional Codex-to-Kanban projection."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from hermes_constants import get_hermes_home


logger = logging.getLogger("gateway.codex_kanban_projection")


@dataclass(frozen=True)
class KanbanProjectionSettings:
    """Fail-closed non-secret settings from ``config.yaml``."""

    enabled: bool = False
    board: str = "default"
    stale_claim_seconds: int = 60
    retry_initial_seconds: float = 1.0
    retry_max_seconds: float = 30.0
    shutdown_timeout_seconds: float = 5.0

    @classmethod
    def from_mapping(cls, value: Any) -> "KanbanProjectionSettings":
        data = value if isinstance(value, Mapping) else {}
        raw_board = str(data.get("board", "default")).strip().lower()
        board = raw_board if raw_board else "default"
        if not all(ch.isalnum() or ch in "-_" for ch in board) or len(board) > 64:
            return cls()
        try:
            stale_claim_seconds = max(5, int(data.get("stale_claim_seconds", 60)))
        except (TypeError, ValueError):
            stale_claim_seconds = 60
        try:
            retry_initial_seconds = max(
                0.05, float(data.get("retry_initial_seconds", 1.0))
            )
            retry_max_seconds = max(
                retry_initial_seconds, float(data.get("retry_max_seconds", 30.0))
            )
            shutdown_timeout_seconds = max(
                0.1, float(data.get("shutdown_timeout_seconds", 5.0))
            )
        except (TypeError, ValueError):
            retry_initial_seconds = 1.0
            retry_max_seconds = 30.0
            shutdown_timeout_seconds = 5.0
        return cls(
            enabled=data.get("enabled") is True,
            board=board,
            stale_claim_seconds=stale_claim_seconds,
            retry_initial_seconds=retry_initial_seconds,
            retry_max_seconds=retry_max_seconds,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
        )


def load_kanban_projection_settings(
    config_path: Path | None = None,
) -> KanbanProjectionSettings:
    """Load the opt-in flag without importing or opening Kanban."""

    path = config_path or (get_hermes_home() / "config.yaml")
    if not path.exists():
        return KanbanProjectionSettings()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Could not load Kanban projection config from %s: %s", path, exc)
        return KanbanProjectionSettings()
    return KanbanProjectionSettings.from_mapping(data.get("kanban_projection"))
