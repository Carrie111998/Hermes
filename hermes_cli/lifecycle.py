"""Hermes lifecycle dispatch for first-party observers and plugins."""

from __future__ import annotations

import logging
from typing import Any, List

logger = logging.getLogger(__name__)


def invoke_hook(hook_name: str, **kwargs: Any) -> List[Any]:
    """Notify first-party observers, then invoke compatibility plugin hooks."""
    try:
        from hermes_cli.observability import observe_lifecycle

        observe_lifecycle(hook_name, **kwargs)
    except Exception:
        logger.warning(
            "Built-in observability hook failed",
            exc_info=hook_name != "pre_api_request",
        )

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


def has_mandatory_hook(hook_name: str) -> bool:
    """Return whether a plugin hook has a fail-closed contract configured."""
    from hermes_cli import plugins

    return plugins.has_mandatory_hook(hook_name)


def invoke_mandatory_hook(
    hook_name: str,
    **kwargs: Any,
) -> tuple[List[Any], List[str]]:
    """Run the mandatory security phase without invoking observers."""
    from hermes_cli import plugins

    return plugins.invoke_mandatory_hook(hook_name, **kwargs)


def invoke_hook_observers(
    hook_name: str,
    mandatory_plugins: List[str],
    **kwargs: Any,
) -> List[Any]:
    """Notify first-party and compatibility observers after mandatory allow."""
    try:
        from hermes_cli.observability import observe_lifecycle

        observe_lifecycle(hook_name, **kwargs)
    except Exception:
        logger.warning(
            "Built-in observability hook failed",
            exc_info=hook_name != "pre_api_request",
        )

    from hermes_cli import plugins

    return plugins.invoke_hook_observers(
        hook_name, mandatory_plugins, **kwargs
    )


def invoke_hook_enforced(hook_name: str, **kwargs: Any) -> List[Any]:
    """Enforce mandatory callbacks before any lifecycle observer side effect."""
    if not has_mandatory_hook(hook_name):
        # Preserve the public observer seam (including callers/tests that patch
        # module-level invoke_hook) when no mandatory contract is configured.
        return invoke_hook(hook_name, **kwargs)

    mandatory_results, required = invoke_mandatory_hook(hook_name, **kwargs)
    observer_results = invoke_hook_observers(
        hook_name, required, **kwargs
    )
    return mandatory_results + observer_results


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
