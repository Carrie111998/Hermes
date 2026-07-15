"""Generic runtime context for the current tool-execution outer timeout.

This module exposes the *effective outer timeout* for the current batch of
tool calls — the value already resolved by the core's
``_resolve_concurrent_tool_timeout`` (env ``HERMES_CONCURRENT_TOOL_TIMEOUT_S``,
default 420 s, or ``None`` when unlimited).

The context is propagated to worker threads via
``tools.thread_context.propagate_context_to_thread``, which captures the
caller's ``contextvars.Context`` at submit time and runs the worker inside a
copy of it.  Tool handlers (including plugin backends) can therefore read the
effective outer timeout from *inside* a worker thread without any explicit
parameter passing.

This is intentionally **generic** (not plugin-specific): any tool or plugin
that needs to respect the Hermes outer tool-execution budget can call
``get_current_tool_timeout()``.
"""

from __future__ import annotations

import contextvars
from typing import Optional

#: ContextVar holding the effective outer tool-execution timeout in seconds.
#:
#: ``None`` means "no outer timeout" (unlimited).  A positive float means the
#: batch deadline is that many seconds from the start of the batch.  The value
#: is set by ``tool_executor`` just before submitting workers and is
#: automatically available inside the worker via context propagation.
_current_tool_timeout: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "_current_tool_timeout",
    default=None,
)


def get_current_tool_timeout() -> Optional[float]:
    """Return the effective outer tool-execution timeout for the current context.

    Returns
    -------
    float or None
        The timeout in seconds, or ``None`` if no outer timeout is set
        (e.g. the value was explicitly unlimited, or the code is running
        outside a tool-execution batch — such as a plugin invoked directly).
    """
    return _current_tool_timeout.get()


def set_current_tool_timeout(timeout: Optional[float]) -> contextvars.Token:
    """Set the effective outer tool-execution timeout for the current context.

    Parameters
    ----------
    timeout
        Timeout in seconds, or ``None`` for unlimited.

    Returns
    -------
    contextvars.Token
        Token to pass to :func:`reset_current_tool_timeout` to restore the
        previous value.
    """
    return _current_tool_timeout.set(timeout)


def reset_current_tool_timeout(token: contextvars.Token) -> None:
    """Reset the tool timeout context to its previous value.

    Parameters
    ----------
    token
        The token returned by :func:`set_current_tool_timeout`.
    """
    _current_tool_timeout.reset(token)
