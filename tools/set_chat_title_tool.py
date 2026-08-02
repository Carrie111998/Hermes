"""Gateway-bound semantic title tool for the current Matrix chat."""

import json

from tools.registry import registry, tool_error


SET_CHAT_TITLE_SCHEMA = {
    "name": "set_chat_title",
    "description": (
        "Set the semantic title of the CURRENT chat only. Pass only the title's "
        "semantic base and omit lifecycle status emoji such as 🟡, ✅, or 🔴; "
        "the gateway adds lifecycle status automatically. Never inspect platform "
        "credentials or write platform API scripts to rename a chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": (
                    "Required semantic-base title for the CURRENT chat only. Omit "
                    "status emoji; the gateway adds lifecycle status."
                ),
            },
        },
        "required": ["title"],
        "additionalProperties": False,
    },
}


def _current_chat_title_callback():
    """Return the task-local gateway capability, failing closed off-gateway."""
    try:
        from gateway.session_context import (
            get_current_chat_rename_callback,
            get_session_env,
        )

        if get_session_env("HERMES_SESSION_PLATFORM", "") != "matrix":
            return None
        callback = get_current_chat_rename_callback()
        return callback if callable(callback) else None
    except Exception:
        return None


def check_set_chat_title_requirements():
    """Keep schema availability stable for the Matrix-only toolset.

    Tool-definition and requirement results are cached across gateway turns,
    so availability must not depend on task-local session ContextVars.  The
    handler performs the authoritative platform and callback checks at every
    dispatch instead.
    """
    return True


def set_chat_title_tool(args, **_kwargs):
    """Rename the bound current chat through the gateway-owned callback."""
    callback = _current_chat_title_callback()
    if callback is None:
        return tool_error(
            "set_chat_title is only available for the current chat in a live, "
            "supported Matrix gateway session."
        )

    title = str(args.get("title") or "").strip()
    if not title:
        return tool_error("'title' must be a non-empty semantic-base title")
    try:
        result = callback(title)
    except Exception as exc:
        return tool_error(f"Current chat title update failed: {exc}")
    if not isinstance(result, dict):
        result = {"success": bool(result)}
    return json.dumps(result)


registry.register(
    name="set_chat_title",
    toolset="chat_title",
    schema=SET_CHAT_TITLE_SCHEMA,
    handler=set_chat_title_tool,
    check_fn=check_set_chat_title_requirements,
    description="Set the semantic title of the current supported gateway chat",
    emoji="🏷️",
)
