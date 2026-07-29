"""Buzz plugin — registers the ``buzz`` toolset with runtime-gated handlers."""
from __future__ import annotations

from plugins.buzz.tools import (
    buzz_channel_create,
    buzz_channel_list,
    buzz_keypair,
    buzz_message_read,
    buzz_message_send,
    buzz_observer_emit,
    buzz_relay_start,
    buzz_relay_status,
    buzz_relay_stop,
    buzz_version,
    buzz_workflow_create,
    buzz_workflow_list,
    buzz_workflow_trigger,
)

_TOOLS = [
    ("buzz_relay_status", buzz_relay_status, "🐝"),
    ("buzz_relay_start", buzz_relay_start, "▶️"),
    ("buzz_relay_stop", buzz_relay_stop, "⏹️"),
    ("buzz_version", buzz_version, "🔎"),
    ("buzz_channel_create", buzz_channel_create, "➕"),
    ("buzz_channel_list", buzz_channel_list, "📋"),
    ("buzz_message_send", buzz_message_send, "📤"),
    ("buzz_message_read", buzz_message_read, "📥"),
    ("buzz_observer_emit", buzz_observer_emit, "🔐"),
    ("buzz_workflow_create", buzz_workflow_create, "🧩"),
    ("buzz_workflow_list", buzz_workflow_list, "📚"),
    ("buzz_workflow_trigger", buzz_workflow_trigger, "⚡"),
    ("buzz_keypair", buzz_keypair, "🔑"),
]


def register(ctx) -> None:
    for name, handler, emoji in _TOOLS:
        schema = _buzz_schema(name)
        try:
            ctx.register_tool(
                name=name,
                toolset="buzz",
                schema=schema,
                handler=handler,
                emoji=emoji,
            )
        except TypeError:
            # Compatibility with older plugin contexts.
            ctx.register_tool(name, schema, handler)


def _schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
    }


def _buzz_schema(name: str) -> dict:
    schemas = {
        "buzz_relay_status": {
            "name": name,
            "description": "Probe Buzz relay health or local compose status.",
            "parameters": _schema({"compose_path": {"type": "string"}}),
        },
        "buzz_relay_start": {
            "name": name,
            "description": "Start the local Buzz infrastructure via docker compose.",
            "parameters": _schema({"compose_path": {"type": "string"}}),
        },
        "buzz_relay_stop": {
            "name": name,
            "description": "Stop the local Buzz infrastructure via docker compose.",
            "parameters": _schema({"compose_path": {"type": "string"}}),
        },
        "buzz_version": {
            "name": name,
            "description": "Check the resolved Buzz CLI and relay configuration.",
            "parameters": _schema(),
        },
        "buzz_channel_create": {
            "name": name,
            "description": "Create a NIP-29 Buzz channel.",
            "parameters": _schema(
                {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "private": {"type": "boolean", "default": False},
                    "type": {"type": "string", "enum": ["stream", "forum"]},
                    "visibility": {"type": "string", "enum": ["open", "private"]},
                },
                ["name"],
            ),
        },
        "buzz_channel_list": {
            "name": name,
            "description": "List accessible NIP-29 Buzz channels.",
            "parameters": _schema({"limit": {"type": "integer", "default": 20}}),
        },
        "buzz_message_send": {
            "name": name,
            "description": "Send a Kind 9 channel message using buzz-sdk build_message.",
            "parameters": _schema(
                {
                    "channel": {"type": "string", "description": "Channel UUID"},
                    "text": {"type": "string"},
                    "content": {"type": "string"},
                    "kind": {"type": "integer", "default": 9, "enum": [9]},
                    "reply_to": {"type": "string"},
                    "broadcast": {"type": "boolean", "default": False},
                    "files": {"type": "array", "items": {"type": "string"}},
                },
                ["channel", "text"],
            ),
        },
        "buzz_message_read": {
            "name": name,
            "description": "Read recent NIP-29 channel messages.",
            "parameters": _schema(
                {
                    "channel": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                    "kinds": {"type": "string", "description": "Comma-separated kinds"},
                },
                ["channel"],
            ),
        },
        "buzz_observer_emit": {
            "name": name,
            "description": "Emit NIP-44 encrypted Kind 24200 observer telemetry/control frame via POST /events.",
            "parameters": _schema(
                {
                    "recipient_pubkey": {"type": "string", "description": "Owner/agent hex pubkey or npub"},
                    "owner_pubkey": {"type": "string"},
                    "agent_pubkey": {"type": "string"},
                    "frame": {"type": "string", "enum": ["telemetry", "control"], "default": "telemetry"},
                    "payload": {"type": "object"},
                    "relay_url": {"type": "string"},
                    "private_key": {"type": "string", "description": "Prefer BUZZ_PRIVATE_KEY env; avoid passing secrets in prompts"},
                },
                ["recipient_pubkey"],
            ),
        },
        "buzz_workflow_create": {
            "name": name,
            "description": "Create a YAML-defined multi-agent workflow in a channel.",
            "parameters": _schema(
                {"channel": {"type": "string"}, "yaml": {"type": "string"}},
                ["channel", "yaml"],
            ),
        },
        "buzz_workflow_list": {
            "name": name,
            "description": "List workflows in a NIP-29 channel.",
            "parameters": _schema({"channel": {"type": "string"}}, ["channel"]),
        },
        "buzz_workflow_trigger": {
            "name": name,
            "description": "Trigger a workflow after a Hermes task completion; sends Kind 46020.",
            "parameters": _schema(
                {"workflow": {"type": "string"}, "workflow_id": {"type": "string"}, "inputs": {"type": "object"}},
                ["workflow"],
            ),
        },
        "buzz_keypair": {
            "name": name,
            "description": "Inspect the configured Nostr identity without exposing the private key.",
            "parameters": _schema(),
        },
    }
    return schemas.get(
        name,
        {"name": name, "description": name, "parameters": _schema()},
    )
