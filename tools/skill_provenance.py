"""Skill write-origin provenance — ContextVar for distinguishing agent-sediment skill writes from foreground user-directed writes.

The curator only consolidates/prunes skills it autonomously created via the
background self-improvement review fork. Skills a user asks a foreground
agent to write belong to the user and must never be auto-curated.

This module exposes a ContextVar that run_agent.py sets before each tool
loop so tool handlers (e.g. skill_manage create) can check whether they
are executing inside the background-review fork.

The signal piggybacks on AIAgent._memory_write_origin, which is already
set to "background_review" for review-fork instances (see
_spawn_background_review in run_agent.py) and defaults to "assistant_tool"
for normal (foreground) agents.

Usage:
    from tools.skill_provenance import (
        set_current_write_origin,
        reset_current_write_origin,
        get_current_write_origin,
    )

    token = set_current_write_origin("background_review")
    try:
        ...  # tool runs here
    finally:
        reset_current_write_origin(token)

    # inside a tool:
    if get_current_write_origin() == "background_review":
        mark_agent_created(skill_name)
"""

import contextvars


_write_origin: contextvars.ContextVar[str] = contextvars.ContextVar(
    "skill_write_origin",
    default="foreground",
)

# The sentinel value the background review fork uses; mirrors
# run_agent.py's AIAgent._memory_write_origin override in
# _spawn_background_review().
BACKGROUND_REVIEW = "background_review"


def set_current_write_origin(origin: str) -> contextvars.Token[str]:
    """Bind the active write origin to the current context.

    Returns a Token the caller must pass to reset_current_write_origin
    in a finally block.
    """
    return _write_origin.set(origin or "foreground")


def reset_current_write_origin(token: contextvars.Token[str]) -> None:
    """Restore the prior write origin context."""
    _write_origin.reset(token)


def get_current_write_origin() -> str:
    """Return the active write origin.

    Default: "foreground" — any tool call made by a regular (non-review)
    agent, from the CLI, the gateway, cron, or a subagent.

    "background_review" — the self-improvement review fork; only skills
    created under this origin should be marked agent-created for curator
    management.
    """
    return _write_origin.get()


def is_background_review() -> bool:
    """Convenience: True iff the current write origin is the background
    review fork."""
    return get_current_write_origin() == BACKGROUND_REVIEW


# --- Review-turn identity -------------------------------------------------
#
# The read-before-write guard in skill_manager_tool needs to know *which*
# review turn is running, so a skill_view in turn N cannot authorise a write
# in turn N+1. This is a ContextVar and is only ever READ from worker threads
# (contextvars.copy_context() copies current values in, so reads are correct);
# the guard's own bookkeeping must not live in a ContextVar because writes
# made inside a worker's copied context are discarded when that tool call
# returns. See the comment on _background_review_read_paths.
_review_turn_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "skill_review_turn_id",
    default="",
)


def begin_review_turn(turn_id: str) -> contextvars.Token[str]:
    """Bind an identifier for the review turn about to run."""
    return _review_turn_id.set(turn_id or "")


def reset_review_turn(token: contextvars.Token[str]) -> None:
    _review_turn_id.reset(token)


def current_background_review_id() -> str:
    """Identifier of the active review turn ("" when not in one)."""
    return _review_turn_id.get()
