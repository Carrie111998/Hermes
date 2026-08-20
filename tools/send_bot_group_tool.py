#!/usr/bin/env python3
"""Post one message into an existing Bot Mode group room.

Lives in the ``desktop_ui`` toolset, which the GUI gateway enables only for a
session whose source is the desktop app. Emits ``bots.group.send`` through the
shared ``desktop_ui`` bridge; the bundled Bot Mode plugin feeds the existing
room engine (``sendToGroupChat``). This queues delivery — it does not wait for
member rounds to settle, and it is not a headless CLI.
"""

import json

from tools import desktop_ui
from tools.registry import registry, tool_error


def send_bot_group_tool(group: str, message: str, thread: str = "") -> str:
    """Ask Bot Mode to post ``message`` into the existing ``group`` room."""
    name = (group or "").strip()
    text = (message or "").strip()
    if not name:
        return tool_error("group is required — the existing Bot Mode room name, e.g. 'Workshop'.")
    if not text:
        return tool_error("message is required — the text to post into the group room.")

    payload = {"group": name, "text": text}
    topic = (thread or "").strip()
    if topic:
        payload["thread"] = topic

    try:
        ok = desktop_ui.emit("bots.group.send", payload)
    except Exception as exc:
        return tool_error(f"Failed to send to Bot Mode group '{name}': {exc}")
    if not ok:
        return tool_error("Bot Mode group send is only available in the Hermes desktop app.")

    result = {"success": True, "queued": True, "group": name}
    if topic:
        result["thread"] = topic
    return json.dumps(result, ensure_ascii=False)


SEND_BOT_GROUP_SCHEMA = {
    "name": "send_bot_group",
    "description": (
        "Post one message into an existing Bot Mode group room (Workshop, Ops, "
        "Strategy, or another room on this desktop). Every bot in the room hears "
        "the same send and the normal mention/round-robin rules apply. Use this "
        "instead of typing through the UI or sending separate 1:1 Bot Chats. "
        "Returns queued delivery — it does not wait for the room to settle. "
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
    ),
    emoji="💬",
)
