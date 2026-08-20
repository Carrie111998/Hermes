#!/usr/bin/env python3
"""Read which OS window sits directly underneath the Hermes desktop window.

The window list lives with the OS, so this tool round-trips through the
gateway's blocking-prompt bridge — the same one `read_terminal` uses:
tui_gateway emits ``window.read.request``, the desktop renderer asks its main
process (which owns native window enumeration) and answers with
``window.read.respond``. This module is just schema + a thin dispatcher over
the platform-injected callback.
"""

import json
import socket
from typing import Callable, Optional

from tools.registry import registry, tool_error


def _agent_host(payload: dict) -> Optional[dict]:
    """Describe the machine gap when the window is not one we can drive.

    The renderer sets ``agent_on_this_machine`` false when the desktop app is
    driving a remote gateway, because only it can know: an SSH tunnel makes the
    client look like loopback from here. It sends the bare flag and we name
    ourselves, so no host or connection detail has to cross.

    Absent on a local session, so the common case costs nothing.
    """
    if payload.get("agent_on_this_machine") is not False:
        return None

    try:
        host = socket.gethostname().strip()
    except Exception:
        host = ""

    where = f"on {host}" if host else "on another machine"

    return {
        "same_machine": False,
        "name": host or None,
        "note": (
            f"This window is on the user's screen. You are running {where}, so "
            "computer_use drives that machine's desktop and cannot click, type "
            "into, or screenshot this window. Say so and tell the user what to "
            "do in it, rather than acting somewhere they aren't looking."
        ),
    }


def read_window_below_tool(callback: Optional[Callable] = None) -> str:
    """Return the window underneath the Hermes window as a JSON string."""
    if callback is None:
        return tool_error(
            "read_window_below is only available in the Hermes desktop app."
        )

    try:
        raw = callback()
    except Exception as exc:
        return tool_error(f"Failed to read the window below: {exc}")

    if not raw:
        return tool_error(
            "Could not determine the window underneath (the desktop app did "
            "not answer, or window enumeration is unavailable on this system)."
        )

    # Desktop answers with a JSON object; pass it through, else wrap the raw text.
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return json.dumps({"text": str(raw)}, ensure_ascii=False)

    if isinstance(payload, dict):
        agent_host = _agent_host(payload)
        payload.pop("agent_on_this_machine", None)

        if agent_host:
            payload["agent_host"] = agent_host

    return json.dumps(payload, ensure_ascii=False)


READ_WINDOW_BELOW_SCHEMA = {
    "name": "read_window_below",
    "description": (
        "Identify the application window directly underneath (behind) the "
        "Hermes desktop window — what the user is working in behind this app. "
        "Returns JSON: {window: {app, title, bounds{x,y,width,height}, id}, "
        "frontmost: {app, title}, platform}. An `agent_host` key appears only "
        "when you are running on a different machine than the user's screen "
        "— its `note` says what you can and cannot do with the window, so "
        "relay it rather than trying anyway. `title` may be empty when the OS "
        "withholds window titles (e.g. macOS without the Screen Recording "
        "permission — never prompted for, noted in `note`). Other Hermes "
        "windows are skipped: the nearest non-Hermes window is reported. "
        "Returns {error, platform} instead where the OS cannot enumerate "
        "windows at all (e.g. a Wayland session); `error` says what would fix "
        "it, so relay it rather than retrying. "
        "Metadata only; this never captures pixels or content of other windows."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


registry.register(
    name="read_window_below",
    toolset="desktop_ui",
    schema=READ_WINDOW_BELOW_SCHEMA,
    handler=lambda args, **kw: read_window_below_tool(callback=kw.get("callback")),
    emoji="🪟",
)
