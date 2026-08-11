import logging
from typing import Any, Dict

from src.registry import registry

logger = logging.getLogger(__name__)


def _send_to_child(subagent_id: str, text: str) -> Dict[str, Any]:
    handle = registry.resolve(subagent_id)
    if handle is None or handle.state != "running":
        return {
            "ok": False,
            "error": f"subagent_id={subagent_id!r} is not running or not found",
            "subagent_id": subagent_id,
        }
    handle_dict: Dict[str, Any] = {
        "subagent_id": handle.subagent_id,
        "session_id": handle.session_id,
        "state": handle.state,
    }
    logger.debug("subagent_send queued for %s: %r", subagent_id, text[:120])
    return {
        "ok": True,
        "subagent_send": handle_dict,
        "queued": True,
    }


def handle_subagent_send(params: Dict[str, Any]) -> Dict[str, Any]:
    subagent_id = str((params or {}).get("subagent_id") or "").strip()
    text = str((params or {}).get("text") or "").strip()
    if not subagent_id:
        return {"ok": False, "error": "subagent_id is required"}
    if not text:
        return {"ok": False, "error": "text is required"}
    return _send_to_child(subagent_id, text)


def handle_cancel_subagent(params: Dict[str, Any]) -> Dict[str, Any]:
    subagent_id = str((params or {}).get("subagent_id") or "").strip()
    if not subagent_id:
        return {"ok": False, "error": "subagent_id is required"}
    handle = registry.resolve(subagent_id)
    if handle is None:
        return {"ok": False, "error": f"subagent_id={subagent_id!r} not found", "subagent_id": subagent_id}
    if handle.state != "running":
        return {
            "ok": False,
            "error": f"subagent_id={subagent_id!r} is not running",
            "subagent_id": subagent_id,
            "state": handle.state,
        }
    updated = registry.set_state(subagent_id, "cancelled")
    if not updated:
        return {"ok": False, "error": f"subagent_id={subagent_id!r} could not be cancelled", "subagent_id": subagent_id}
    return {
        "ok": True,
        "subagent_id": subagent_id,
        "state": "cancelled",
        "session_id": handle.session_id,
    }


SCHEMA = {
    "subagent_send": {
        "name": "subagent_send",
        "description": "Send steering text to a running subagent by handle.",
        "parameters": {
            "type": "object",
            "properties": {
                "subagent_id": {"type": "string", "description": "Target subagent_id"},
                "text": {"type": "string", "description": "Steer message to deliver"},
            },
            "required": ["subagent_id", "text"],
        },
    },
    "cancel_subagent": {
        "name": "cancel_subagent",
        "description": "Cancel a running subagent by subagent_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "subagent_id": {"type": "string", "description": "subagent_id to cancel"},
            },
            "required": ["subagent_id"],
        },
    },
}


def register_tools(ctx) -> None:
    try:
        ctx.register_tool(SCHEMA["subagent_send"], handle_subagent_send)
    except Exception as exc:
        logger.debug("register_tool subagent_send failed: %s", exc)
    try:
        ctx.register_tool(SCHEMA["cancel_subagent"], handle_cancel_subagent)
    except Exception as exc:
        logger.debug("register_tool cancel_subagent failed: %s", exc)
