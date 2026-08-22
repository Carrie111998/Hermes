"""Compatibility helper for explicit agent stop producers."""

from __future__ import annotations

import inspect
from typing import Any


def request_hard_interrupt(agent: Any, message: str | None = None) -> bool:
    """Request an explicit stop, falling back to the legacy interrupt ABI.

    New agents expose ``hard_interrupt(message=None)``. Third-party agents and
    old test doubles may only expose ``interrupt(message=None)``; keep those
    usable without sending the newer ``hard_cancel=`` keyword they do not know.
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
    if message is None:
        interrupt()
    else:
        interrupt(message)
    return True


INTERRUPT_JOIN_MAX_TICKS = 50  # 50 * 0.2s = 10s, same bound as before this incident


def interrupt_join_should_stop(
    *,
    ticks_elapsed: int,
    agent_thread_alive: bool,
    should_exit: bool,
    max_ticks: int = INTERRUPT_JOIN_MAX_TICKS,
) -> bool:
    """Bounded interrupt wait: stop on death, force-exit, or timeout.

    An unbounded wait freezes the CLI when a child ignores interrupt.
    """
    if not agent_thread_alive or should_exit:
        return True
    return ticks_elapsed >= max_ticks


def interrupt_followup_disposition(
    *,
    agent_thread_alive: bool,
    should_exit: bool = False,
) -> str:
    """What to do with the user's post-interrupt message.

    enqueue: thread is dead; starting a new turn is safe.
    park: thread still alive after the bounded wait; hold the text.
    drop: process is exiting (second Ctrl+C).
    """
    if should_exit:
        return "drop"
    if agent_thread_alive:
        return "park"
    return "enqueue"


def should_enqueue_interrupt_followup(*, agent_thread_alive: bool) -> bool:
    """Whether CLI may start a new user turn after /stop or interrupt.

    A still-running agent thread shares the Codex stream writer with the next
    turn. Enqueueing follow-up while it is alive is what produces
    ``Codex streaming attempt superseded`` plus post-stop tool/API calls.
    """
    return interrupt_followup_disposition(agent_thread_alive=agent_thread_alive) == "enqueue"
