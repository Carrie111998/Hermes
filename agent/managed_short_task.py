"""Pure, side-effect-free managed short-task process predicates.

This module intentionally imports only the standard library.  Startup and
tool-boundary code may consult it before importing plugins, terminal backends,
checkpoint helpers, context engines, or any other executable capability.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


def _env_value(env: Mapping[str, str] | None, key: str) -> str:
    source = os.environ if env is None else env
    return str(source.get(key, "") or "").strip()


def managed_short_task_lane_claimed(
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return whether a process claims a managed implementation/review lane."""
    return bool(
        _env_value(env, "HERMES_KANBAN_TASK")
        and _env_value(env, "HERMES_KANBAN_MANAGED_LANE")
        in {"implementation", "review"}
    )


def verified_managed_short_task_lane(
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return whether the earliest CLI gate attested the claimed lane."""
    if not managed_short_task_lane_claimed(env):
        return False
    source = os.environ if env is None else env
    lane = _env_value(source, "HERMES_KANBAN_MANAGED_LANE")
    review_mode = _env_value(source, "HERMES_KANBAN_REVIEW_MODE") == "1"
    return bool(
        (lane == "review") == review_mode
        and _env_value(source, "HERMES_KANBAN_MANAGED_BOOTSTRAP") == "1"
        and _env_value(source, "HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED")
        == "1"
        and not _env_value(source, "HERMES_KANBAN_MANAGED_BOOTSTRAP_ERROR")
    )


def managed_short_task_lane(
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return the verified lane name, otherwise ``None``."""
    if not verified_managed_short_task_lane(env):
        return None
    return _env_value(env, "HERMES_KANBAN_MANAGED_LANE")
