#!/usr/bin/env python3
"""Post one message into an existing Bot Mode group room.

Lives in the ``desktop_ui`` toolset, which the GUI gateway enables only for a
session whose source is the desktop app. Round-trips through the gateway's
blocking-prompt bridge like ``tour``: tui_gateway emits
``bots.group.send.request``, the bundled Bot Mode plugin feeds the existing
room engine and answers ``bots.group.send.respond``. Unknown groups and empty
payloads fail closed. This is not a headless CLI.
"""

import json
from typing import Callable, Optional

from tools.registry import registry, tool_error


def send_bot_group_tool(
    group: str = "",
    message: str = "",
    thread: str = "",
    callback: Optional[Callable] = None,
) -> str:
    """Ask Bot Mode to post ``message`` into the existing ``group`` room."""
    if callback is None:
        return tool_error("Bot Mode group send is only available in the Hermes desktop app.")

    if not isinstance(group, str) or not group.strip():
        return tool_error("group is required — the existing Bot Mode room name, e.g. 'Workshop'.")
    if not isinstance(message, str) or not message.strip():
        return tool_error("message is required — the text to post into the group room.")
    if thread not in (None, "") and not isinstance(thread, str):
        return tool_error("thread must be a string.")

    payload = {"group": group.strip(), "text": message.strip()}
    topic = (thread or "").strip() if isinstance(thread, str) else ""
    if topic:
        payload["thread"] = topic

    try:
        raw = callback(payload)
    except Exception as exc:
        return tool_error(f"Failed to send to Bot Mode group '{payload['group']}': {exc}")
    if not raw:
        return tool_error(
            "No Hermes Desktop window answered the group send. Update the desktop app "
            "and start a new session if Bot Mode is older than this tool."
        )

    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return json.dumps({"text": str(raw)}, ensure_ascii=False)

    if isinstance(parsed, dict) and parsed.get("error"):
        return tool_error(str(parsed["error"]))
    return json.dumps(parsed, ensure_ascii=False)


SEND_BOT_GROUP_SCHEMA = {
    "name": "send_bot_group",
    "description": (
        "Post one message into an existing Bot Mode group room (Workshop, Ops, "
        "Strategy, or another room on this desktop). Every bot in the room hears "
        "the same send and the normal mention/round-robin rules apply. Use this "
        "instead of typing through the UI or sending separate 1:1 Bot Chats. "
        "Returns queued delivery only after the room accepts the send. "
        "Desktop sessions only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group": {
                "type": "string",
                "description": "Existing Bot Mode group name, e.g. 'Workshop'.",
            },
            "message": {
                "type": "string",
                "description": "The room message. @name directs; @everyone addresses all.",
            },
            "thread": {
                "type": "string",
                "description": "Optional existing thread id to continue. Omit to start a new thread.",
            },
        },
        "required": ["group", "message"],
    },
}


registry.register(
    name="send_bot_group",
    toolset="desktop_ui",
    schema=SEND_BOT_GROUP_SCHEMA,
    handler=lambda args, **kw: send_bot_group_tool(
        group=args.get("group", ""),
        message=args.get("message", ""),
        thread=args.get("thread", ""),
        callback=kw.get("callback"),
    ),
    emoji="💬",
)
