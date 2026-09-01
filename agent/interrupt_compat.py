"""Compatibility helper for explicit agent stop producers."""

from __future__ import annotations

import inspect
from typing import Any


def _accepts_keyword(callable_obj: Any, name: str) -> bool:
    """Return whether a callable explicitly supports a keyword argument."""
    try:
        parameters = inspect.signature(callable_obj).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == name
            and parameter.kind is not inspect.Parameter.POSITIONAL_ONLY
        )
        for parameter in parameters
    )


def request_hard_interrupt(
    agent: Any,
    message: str | None = None,
    *,
    stop_kind: str | None = None,
    tool_reason: str | None = None,
) -> bool:
    """Request an explicit stop, falling back to the legacy interrupt ABI.

    New agents expose ``hard_interrupt(message=None)``. Third-party agents and
    old test doubles may only expose ``interrupt(message=None)``, so keep those
    usable without sending newer keyword arguments they do not know.

    ``message`` is diagnostic/control-plane text. ``stop_kind``
    (``"user_stop"``/``"client_disconnect"``) carries the structured interrupt
    provenance (#84207); ``tool_reason`` is a trusted, fixed category that may
    be exposed in model-visible tool cancellation output. Each channel is only
    forwarded when the resolved callable explicitly supports it, and
    ``stop_kind`` is additionally stamped onto agents that record it as
    ``_interrupt_stop_kind`` but predate the keyword parameter.
    Returns ``False`` only when neither callable is available.
    """
    # Avoid treating a dynamic ``__getattr__`` proxy (notably an unspecced
    # ``MagicMock`` or a third-party RPC facade) as if it genuinely implements
    # the new ABI. Static lookup proves the attribute exists on the instance or
    # its type before normal descriptor binding retrieves the callable.
    try:
        inspect.getattr_static(agent, "hard_interrupt")
    except AttributeError:
        interrupt = None
    else:
        interrupt = getattr(agent, "hard_interrupt", None)
    if not callable(interrupt):
        interrupt = getattr(agent, "interrupt", None)
    if not callable(interrupt):
        return False

    kwargs = {}
    if stop_kind is not None and _accepts_keyword(interrupt, "stop_kind"):
        kwargs["stop_kind"] = stop_kind
    if tool_reason is not None and _accepts_keyword(interrupt, "tool_reason"):
        kwargs["tool_reason"] = tool_reason

    if message is None:
        interrupt(**kwargs)
    else:
        interrupt(message, **kwargs)

    # Stamp structured provenance when the resolved callable couldn't carry it
    # itself (#84207). Static lookup avoids treating an unspecced MagicMock's
    # auto-attribute as genuine. The stamp runs AFTER the interrupt call above:
    # AIAgent.interrupt() rewrites _interrupt_stop_kind from its own parameter,
    # so stamping before the call would be lost.
    if stop_kind is not None and "stop_kind" not in kwargs:
        try:
            inspect.getattr_static(agent, "_interrupt_stop_kind")
        except (AttributeError, TypeError):
            pass
        else:
            agent._interrupt_stop_kind = stop_kind

    return True
