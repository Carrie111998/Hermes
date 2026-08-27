"""Runtime policy boundary for artifact-certified turns.

Certification defers passive publication until the artifact verdict. It never
bypasses request transforms, execution middleware, Relay admission, or tool
authorization. Keeping that distinction here prevents the legacy agent-loop
surfaces from becoming certification policy owners.
"""

from __future__ import annotations

from typing import Any, Callable


def publication_deferred(agent: Any) -> bool:
    """Return whether passive turn publication is deferred for certification."""

    return getattr(agent, "_certification_persistence_deferred", False) is True


def apply_llm_request(
    agent: Any,
    request: dict[str, Any],
    **context: Any,
):
    """Apply request middleware regardless of publication state."""

    from hermes_cli.middleware import apply_llm_request_middleware

    return apply_llm_request_middleware(
        request,
        required=publication_deferred(agent),
        **context,
    )


def run_llm_execution(
    agent: Any,
    request: dict[str, Any],
    next_call: Callable[[dict[str, Any]], Any],
    **context: Any,
) -> Any:
    """Run provider execution through the installation's privacy boundary."""

    from hermes_cli.middleware import (
        llm_execution_middleware_required,
        run_llm_execution_middleware,
    )

    return run_llm_execution_middleware(
        request,
        next_call,
        required=publication_deferred(agent) or llm_execution_middleware_required(),
        **context,
    )


def run_relay_llm_execution(
    agent: Any,
    request: dict[str, Any],
    next_call: Callable[[dict[str, Any]], Any],
    **context: Any,
) -> Any:
    """Run Relay's canonical provider admission path for every turn."""

    del agent
    from agent import relay_llm

    return relay_llm.execute(request, next_call, **context)


__all__ = [
    "apply_llm_request",
    "publication_deferred",
    "run_llm_execution",
    "run_relay_llm_execution",
]
