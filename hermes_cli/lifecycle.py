"""Hermes lifecycle dispatch for first-party observers and plugins."""

from __future__ import annotations

import logging
from typing import Any, List

logger = logging.getLogger(__name__)

# Machine-readable producer seal for governed plugins. Consumers must inspect
# this production module instead of inferring support from hook names or tests.
# An explicit reset edge includes initial construction as ``None -> new``.
# Desktop close/create is ``finalize(old)`` followed by ``None -> new``; it is
# not a replacement edge and consumers must not infer one from process state.
CORTEX_PLUGIN_PRODUCER_CONTRACT = (
    "pre_llm.authoritative_workspace.v1",
    "pre_llm.desktop_session_workspace.v1",
    "session_reset.explicit_edge.v1",
)


def invoke_hook(hook_name: str, **kwargs: Any) -> List[Any]:
    """Notify first-party observers, then invoke compatibility plugin hooks."""
    try:
        from hermes_cli.observability import observe_lifecycle

        observe_lifecycle(hook_name, **kwargs)
    except Exception:
        logger.warning("Built-in observability hook failed", exc_info=True)

    from hermes_cli import plugins

    return plugins.invoke_hook(hook_name, **kwargs)


def has_hook(hook_name: str) -> bool:
    """Return whether a first-party observer or plugin consumes a hook."""
    try:
        from hermes_cli.observability import handles_hook

        if handles_hook(hook_name):
            return True
    except Exception:
        logger.warning("Unable to inspect built-in observability hooks", exc_info=True)

    from hermes_cli import plugins

    return plugins.has_hook(hook_name)


def finalize_session(**kwargs: Any) -> List[Any]:
    """Notify observers and hard-close one core-owned Relay conversation."""
    try:
        from hermes_cli.observability import observe_lifecycle

        observe_lifecycle("on_session_finalize", **kwargs)
    except Exception:
        logger.warning("Built-in observability hook failed", exc_info=True)

    session_id = str(kwargs.get("session_id") or "")
    if session_id:
        try:
            from agent import relay_runtime

            relay_runtime.SESSION_COORDINATOR.finalize_conversation(
                profile_key=relay_runtime.current_profile_key(),
                session_id=session_id,
            )
        except Exception:
            logger.warning("Core Relay session finalization failed", exc_info=True)

    from hermes_cli import plugins

    return plugins.invoke_hook("on_session_finalize", **kwargs)
