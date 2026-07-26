"""Buzz plugin — registers toolset ``buzz`` with runtime-gated handlers."""
from __future__ import annotations

from plugins.buzz.tools import (
    buzz_channel_create,
    buzz_channel_list,
    buzz_keypair,
    buzz_message_read,
    buzz_message_send,
    buzz_relay_start,
    buzz_relay_status,
    buzz_relay_stop,
)

_TOOLS = [
    ("buzz_relay_status", buzz_relay_status, "🐝"),
    ("buzz_relay_start", buzz_relay_start, "▶️"),
    ("buzz_relay_stop", buzz_relay_stop, "⏹️"),
    ("buzz_channel_create", buzz_channel_create, "➕"),
    ("buzz_channel_list", buzz_channel_list, "📋"),
    ("buzz_message_send", buzz_message_send, "📤"),
    ("buzz_message_read", buzz_message_read, "📥"),
    ("buzz_keypair", buzz_keypair, "🔐"),
]


def register(ctx) -> None:
    for _name, _handler, _emoji in _TOOLS:
        try:
            ctx.register_tool(
                name=_name,
                toolset="buzz",
                schema=_buzz_schema(_name),
                handler=_handler,
                emoji=_emoji,
            )
        except TypeError:
            ctx.register_tool(_name, _buzz_schema(_name), _handler)


def _buzz_schema(name: str) -> dict:
    base = {"type": "object", "properties": {}, "required": []}
    return {
        "buzz_relay_status": {
            "name": "buzz_relay_status",
            "description": "Probe Buzz relay container health.",
            "parameters": base,
        },
        "buzz_relay_start": {
            "name": "buzz_relay_start",
            "description": "Start a local Buzz relay via docker compose.",
            "parameters": {
                **base,
                "properties": {
                    "compose_path": {
                        "type": "string",
                        "description": "Compose dir. Default: vendor/buzz.",
                    }
                },
            },
        },
        "buzz_relay_stop": {
            "name": "buzz_relay_stop",
            "description": "Bring down the local Buzz relay.",
            "parameters": {
                **base,
                "properties": {
                    "compose_path": {
                        "type": "string",
                        "description": "Compose dir. Default: vendor/buzz.",
                    }
                },
            },
        },
        "buzz_channel_create": {
            "name": "buzz_channel_create",
            "description": "Create a Buzz channel.",
            "parameters": {
                **base,
                "properties": {
                    "name": {"type": "string", "description": "Channel name."},
                    "private": {"type": "boolean", "description": "Hide from public listing.", "default": False},
                },
                "required": ["name"],
            },
        },
        "buzz_channel_list": {
            "name": "buzz_channel_list",
            "description": "List accessible Buzz channels.",
            "parameters": {
                **base,
                "properties": {
                    "limit": {"type": "integer", "description": "Optional result cap.", "default": 20}
                },
            },
        },
        "buzz_message_send": {
            "name": "buzz_message_send",
            "description": "Send a message to a Buzz channel.",
            "parameters": {
                **base,
                "properties": {
                    "channel": {"type": "string", "description": "Channel id or name."},
                    "text": {"type": "string", "description": "Message body."},
                },
                "required": ["channel", "text"],
            },
        },
        "buzz_message_read": {
            "name": "buzz_message_read",
            "description": "Read recent messages from a Buzz channel.",
            "parameters": {
                **base,
                "properties": {
                    "channel": {"type": "string", "description": "Channel id or name."},
                    "limit": {"type": "integer", "description": "Max messages.", "default": 20},
                },
                "required": ["channel"],
            },
        },
        "buzz_keypair": {
            "name": "buzz_keypair",
            "description": "Inspect the configured Nostr identity (BUZZ_PRIVATE_KEY).",
            "parameters": {**base},
        },
    }.get(name, {"name": name, "description": name, "parameters": base})
