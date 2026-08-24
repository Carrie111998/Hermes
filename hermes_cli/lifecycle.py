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
    """Notify observers and hard-close one core-owned Relay conversation.

    A required settlement failure fails the RUN but must never make teardown
    optional: the authoritative receipt is attempted first (so the policy still
    observes pre-teardown state), its failure is captured, and built-in
    observability, core Relay finalization, and compatibility observers all
    run before it is propagated.

    ``runtime_run_id`` selects the run lease. It is consumed here rather than
    forwarded, so observers keep their existing payload.
    """
    from hermes_cli import plugins

    session_id = str(kwargs.get("session_id") or "")
    run_id = str(kwargs.pop("runtime_run_id", "") or "")
    receipt = None
    results: List[Any] = []
    settlement_error: BaseException | None = None

    # Skip the lookup entirely when there is nothing to resolve, so an
    # identity-less finalize never triggers plugin discovery.
    resolved_run = (
        plugins.resolve_authoritative_run(run_id=run_id, session_id=session_id)
        if (run_id or session_id)
        else None
    )
    if resolved_run:
        try:
            receipt = plugins.finalize_authoritative_run(
                resolved_run, session_id=session_id,
                **{key: value for key, value in kwargs.items()
                   if key != "session_id"},
            )
        except BaseException as exc:  # noqa: BLE001 — re-raised after teardown
            settlement_error = exc

    try:
        try:
            from hermes_cli.observability import observe_lifecycle

            observe_lifecycle("on_session_finalize", **kwargs)
        except Exception:
            logger.warning("Built-in observability hook failed", exc_info=True)

        if session_id:
            try:
                from agent import relay_runtime

                relay_runtime.SESSION_COORDINATOR.finalize_conversation(
                    profile_key=relay_runtime.current_profile_key(),
                    session_id=session_id,
                )
            except Exception:
                logger.warning("Core Relay session finalization failed", exc_info=True)

        results = plugins.invoke_hook("on_session_finalize", **kwargs)
    finally:
        if settlement_error is not None:
            raise settlement_error

    return ([receipt] if receipt is not None else []) + results
