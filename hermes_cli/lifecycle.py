"""Hermes lifecycle dispatch for first-party observers and plugins."""

from __future__ import annotations

import logging
from typing import Any, List

logger = logging.getLogger(__name__)


def invoke_hook(hook_name: str, **kwargs: Any) -> List[Any]:
    """Notify first-party observers, then invoke compatibility plugin hooks."""
    from hermes_cli import plugins

    # Temporary sessions: observe-only hooks are suppressed for BOTH the
    # first-party observers and plugins, and anything still delivered carries
    # ephemeral=True. plugins.invoke_hook applies the same contract
    # idempotently for callers that reach it directly.
    deliver, kwargs = plugins.ephemeral_hook_delivery(hook_name, kwargs)
    if not deliver:
        return []

    try:
        from hermes_cli.observability import observe_lifecycle

        observe_lifecycle(hook_name, **kwargs)
    except Exception:
        logger.warning("Built-in observability hook failed", exc_info=True)

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
    from hermes_cli import plugins

    # Temporary sessions: skip the observe-side (first-party observer and the
    # plugin on_session_finalize below, which plugins.invoke_hook suppresses
    # itself) but ALWAYS run the core Relay hard-close — that is resource
    # cleanup, not persistence, and skipping it would leak the conversation
    # slot.
    _deliver, kwargs = plugins.ephemeral_hook_delivery("on_session_finalize", kwargs)
    if _deliver:
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
