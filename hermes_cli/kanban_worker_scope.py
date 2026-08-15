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

# Session-routing values scrubbed by ``kanban_db._default_spawn`` before the
# child starts. Their absence is dispatcher authority too: a profile dotenv
# must not reconnect a detached worker to a gateway/cron delivery target.
# Keep this in sync with ``gateway.session_context._VAR_MAP``; the startup
# isolation tests enforce parity without importing gateway code here.
DISPATCHER_SESSION_ENV_KEYS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_SOURCE",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_SESSION_CHAT_NAME",
    "HERMES_SESSION_THREAD_ID",
    "HERMES_SESSION_USER_ID",
    "HERMES_SESSION_USER_NAME",
    "HERMES_SESSION_KEY",
    "HERMES_SESSION_ID",
    "HERMES_UI_SESSION_ID",
    "HERMES_SESSION_MESSAGE_ID",
    "HERMES_SESSION_PROFILE",
    "HERMES_CRON_SESSION",
    "HERMES_CRON_AUTO_DELIVER_PLATFORM",
    "HERMES_CRON_AUTO_DELIVER_CHAT_ID",
    "HERMES_CRON_AUTO_DELIVER_THREAD_ID",
)

# Values pinned by the dispatcher before the profile process starts. Profile
# dotenv/config reloads may populate ordinary user configuration, but they must
# never replace this process authority or execution location. Missing values
# are authority too: lifecycle workers must not acquire an interactive UI,
# hook consent, or tenant from a later startup bridge.
PINNED_WORKER_ENV_KEYS = (
    WORKER_SCOPE_ENV,
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BRANCH",
    "HERMES_KANBAN_GOAL_MODE",
    "HERMES_KANBAN_GOAL_MAX_TURNS",
    "HERMES_PROFILE",
    "HERMES_HOME",
    "HERMES_TENANT",
    *DISPATCHER_SESSION_ENV_KEYS,
    "HERMES_TUI",
    "HERMES_ACCEPT_HOOKS",
    "TERMINAL_CWD",
    "TERMINAL_TIMEOUT",
    "TERMINAL_MAX_FOREGROUND_TIMEOUT",
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
