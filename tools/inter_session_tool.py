"""Inter-session mailbox tool."""

from __future__ import annotations

import json
from typing import Any

from tools.registry import registry


def _load_raw_config() -> dict:
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def check_inter_session_requirements() -> bool:
    """Expose the tool only when the profile has inter-session messaging."""
    try:
        from gateway.inter_session import parse_inter_session_config

        cfg = parse_inter_session_config(_load_raw_config())
        return bool(cfg.enabled and cfg.sessions)
    except Exception:
        return False


def _current_source_from_env():
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.session_context import get_session_env

    platform_raw = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    if not platform_raw:
        return None
    try:
        platform = Platform(platform_raw)
    except Exception:
        return None
    return SessionSource(
        platform=platform,
        chat_id=get_session_env("HERMES_SESSION_CHAT_ID", "").strip(),
        chat_name=get_session_env("HERMES_SESSION_CHAT_NAME", "").strip() or None,
        chat_type="dm" if platform == Platform.LOCAL else "group",
        user_id=get_session_env("HERMES_SESSION_USER_ID", "").strip() or None,
        user_name=get_session_env("HERMES_SESSION_USER_NAME", "").strip() or None,
        thread_id=get_session_env("HERMES_SESSION_THREAD_ID", "").strip() or None,
    )


def send_session_message(args: dict[str, Any], **kwargs) -> str:
    """Persist a message to another configured Hermes session."""
    from gateway.inter_session import (
        configured_session_key,
        find_current_session,
        parse_inter_session_config,
    )
    from gateway.session_context import get_session_env
    from hermes_state import SessionDB

    raw_cfg = _load_raw_config()
    cfg = parse_inter_session_config(raw_cfg)
    if not cfg.enabled:
        return json.dumps({"success": False, "error": "inter_session is disabled"})
    if not cfg.sessions:
        return json.dumps({"success": False, "error": "no inter_session sessions configured"})

    to_name = str(args.get("to") or "").strip()
    body = str(args.get("body") or "").strip()
    if not to_name:
        return json.dumps({"success": False, "error": "to is required"})
    if not body:
        return json.dumps({"success": False, "error": "body is required"})

    to_peer = cfg.peer(to_name)
    if to_peer is None:
        return json.dumps({
            "success": False,
            "error": f"unknown inter_session target '{to_name}'",
            "available_targets": sorted(cfg.sessions.keys()),
        })

    current_key = get_session_env("HERMES_SESSION_KEY", "").strip()
    current_source = _current_source_from_env()
    from_peer = find_current_session(
        cfg,
        session_key=current_key,
        source=current_source,
        raw_config=raw_cfg,
    )
    if from_peer is None:
        return json.dumps({
            "success": False,
            "error": "current turn is not a configured inter_session session",
        })
    if to_peer.name not in from_peer.can_send_to:
        return json.dumps({
            "success": False,
            "error": f"session '{from_peer.name}' cannot send to '{to_peer.name}'",
            "can_send_to": list(from_peer.can_send_to),
        })

    from_key = current_key or configured_session_key(from_peer, raw_cfg)
    to_key = configured_session_key(to_peer, raw_cfg)
    from_session_id = get_session_env("HERMES_SESSION_ID", "").strip() or None
    source_turn_id = kwargs.get("task_id") or None

    db = SessionDB()
    try:
        row = db.create_session_mailbox_message(
            agent_id=cfg.agent_id,
            from_session_name=from_peer.name,
            from_session_key=from_key,
            from_session_id=from_session_id,
            to_session_name=to_peer.name,
            to_session_key=to_key,
            body=body,
            source_turn_id=source_turn_id,
        )
    finally:
        db.close()

    return json.dumps({
        "success": True,
        "id": row["id"],
        "status": row["status"],
        "to": to_peer.name,
        "from": from_peer.name,
        "correlation_id": row["correlation_id"],
    }, ensure_ascii=False)


registry.register(
    name="send_session_message",
    toolset="inter_session",
    schema={
        "name": "send_session_message",
        "description": (
            "Send a durable internal message to another configured Hermes session. "
            "Use only session names listed in the current session's can_send_to map."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Configured destination session name, for example management or amk_ops.",
                },
                "body": {
                    "type": "string",
                    "description": "Message body for the destination session. Hermes adds provenance metadata.",
                },
            },
            "required": ["to", "body"],
        },
    },
    handler=send_session_message,
    check_fn=check_inter_session_requirements,
    description="Send durable messages between configured Hermes sessions",
    emoji="✉️",
)
