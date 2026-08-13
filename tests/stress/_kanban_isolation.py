"""Fail-closed disposable Kanban environment for standalone stress scripts."""

from __future__ import annotations

import os
from pathlib import Path


_WORKER_CONTEXT_KEYS = {
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_GOAL_MAX_TURNS",
    "HERMES_KANBAN_GOAL_MODE",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_WORKSPACE",
}


def isolate_kanban_env(home: str | Path) -> Path:
    """Replace every inherited board/worker pin with disposable paths."""
    root = Path(home).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    for key in _WORKER_CONTEXT_KEYS:
        os.environ.pop(key, None)
    os.environ.update(
        {
            "HOME": str(root),
            "HERMES_HOME": str(root),
            "HERMES_KANBAN_HOME": str(root),
            "HERMES_KANBAN_DB": str(root / "kanban.db"),
            "HERMES_KANBAN_BOARD": "default",
            "HERMES_KANBAN_WORKSPACES_ROOT": str(
                root / "kanban" / "boards" / "default" / "workspaces"
            ),
        }
    )
    return root
