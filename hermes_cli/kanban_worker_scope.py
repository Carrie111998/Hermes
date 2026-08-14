"""Fail-closed capability boundary for lifecycle-only Kanban workers.

The assignee profile selects the public ``kanban_lifecycle`` toolset.  The
dispatcher resolves that profile before spawning the child and pins the
selection into an internal process scope so startup and dispatch code can
enforce the same boundary before the first model turn.
"""
from __future__ import annotations

import os
from typing import MutableMapping, Sequence


LIFECYCLE_TOOLSET = "kanban_lifecycle"
WORKER_SCOPE_ENV = "HERMES_KANBAN_WORKER_SCOPE"
LIFECYCLE_SCOPE = "lifecycle-only"
LIFECYCLE_TOOL_NAMES = frozenset(
    {
        "kanban_show",
        "kanban_complete",
        "kanban_block",
        "kanban_heartbeat",
    }
)


def pin_worker_scope(
    env: MutableMapping[str, str],
    toolsets: Sequence[str] | None,
) -> list[str] | None:
    """Pin a resolved lifecycle profile into the child and return its toolsets.

    The value is internal process state, not user-facing configuration.  Clear
    inherited state on every spawn so one task/profile can never broaden or
    narrow another by environment leakage.
    """
    env.pop(WORKER_SCOPE_ENV, None)
    if not toolsets:
        return None
    resolved = [str(item) for item in toolsets]
    if LIFECYCLE_TOOLSET in resolved:
        env[WORKER_SCOPE_ENV] = LIFECYCLE_SCOPE
        return [LIFECYCLE_TOOLSET]
    return resolved


def current_worker_scope() -> str | None:
    """Return the canonical process scope, failing closed on unknown values."""
    raw = str(os.environ.get(WORKER_SCOPE_ENV) or "").strip()
    if not raw:
        return None
    if raw != LIFECYCLE_SCOPE:
        raise ValueError(f"invalid Kanban worker scope: {raw!r}")
    return raw


def is_lifecycle_only_worker() -> bool:
    """True only for a dispatcher-owned task process pinned lifecycle-only."""
    return bool(os.environ.get("HERMES_KANBAN_TASK")) and (
        current_worker_scope() == LIFECYCLE_SCOPE
    )
