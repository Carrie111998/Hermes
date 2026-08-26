#!/usr/bin/env python3
"""Bridge desktop-only tools to Hermes-desktop renderer events.

The preview pane, pane focus, and friends live in the desktop renderer, so
desktop-gated tools reach them through an emitter the desktop ``tui_gateway``
installs at session start via :func:`set_emitter`. Everywhere else it stays
``None`` and the tools report "desktop only". Routing keys off
``HERMES_UI_SESSION_ID`` so the event lands on the window that owns the turn
(``_emit``/``write_json`` is ``_stdout_lock``-guarded, so emitting from the
tool's thread is safe).
"""

from typing import Callable, Optional

from gateway.session_context import get_session_env

# (sid, event, payload) sink, installed by the desktop gateway.
_emit: Optional[Callable[[str, str, dict], None]] = None
# Blocking renderer round-trip, installed alongside the event sink.
_request: Optional[Callable[[str, str, dict, float], str]] = None


def set_emitter(fn: Optional[Callable[[str, str, dict], None]]) -> None:
    """Install (or clear) the renderer-event sink. Called by the desktop gateway."""
    global _emit
    _emit = fn


def set_requester(fn: Optional[Callable[[str, str, dict, float], str]]) -> None:
    """Install (or clear) the blocking renderer request bridge."""
    global _request
    _request = fn


def available() -> bool:
    """True when running under the desktop app (an emitter is wired)."""
    return _emit is not None


def emit(event: str, payload: dict) -> bool:
    """Route ``event`` to the window that owns the current turn.

    Returns ``False`` when no emitter is wired (i.e. not the desktop app)."""
    fn = _emit
    if fn is None:
        return False
    fn(get_session_env("HERMES_UI_SESSION_ID", ""), event, payload)
    return True


def request(event: str, payload: dict, *, timeout: float) -> Optional[str]:
    """Route an event and wait for the owning renderer's response.

    Returns ``None`` when no desktop gateway installed the blocking bridge;
    an empty string means a wired renderer did not answer before ``timeout``.
    """
    fn = _request
    if fn is None:
        return None
    return fn(get_session_env("HERMES_UI_SESSION_ID", ""), event, payload, timeout)
