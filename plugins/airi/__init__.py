"""Hermes bridge for Project AIRI.

AIRI is a Hermes-managed process worker (VRM/UI/TTS shell). Hermes Agent
gateway api_server is synced as AIRI's OpenAI-compatible chat core.
VRChat control is deliberately local OSC only.
"""
from __future__ import annotations

from .cli import airi_command, register_cli
from .core import (
    AIRI_SCHEMAS,
    configure_hermes,
    restart,
    start,
    status,
    stop,
    sync,
    vrchat_autonomy,
    vrchat_chatbox,
    vrchat_parameter,
)


def register(ctx) -> None:
    handlers = {
        "airi_status": status,
        "airi_sync": sync,
        "airi_configure_hermes": configure_hermes,
        "airi_start": start,
        "airi_stop": stop,
        "airi_restart": restart,
        "airi_vrchat_chatbox": vrchat_chatbox,
        "airi_vrchat_parameter": vrchat_parameter,
        "airi_vrchat_autonomy": vrchat_autonomy,
    }
    for name, handler in handlers.items():
        schema = AIRI_SCHEMAS[name]
        ctx.register_tool(
            name=name,
            toolset="airi",
            schema=schema,
            handler=handler,
            check_fn=lambda: True,
            description=schema.get("description", ""),
            emoji="🧸",
        )

    ctx.register_cli_command(
        name="airi",
        help="Manage AIRI as a Hermes process worker (sync AI core + VRM shell)",
        setup_fn=register_cli,
        handler_fn=airi_command,
        description=(
            "Project AIRI process worker: Hermes Agent OpenAI-compatible AI core, "
            "desktop VRM shell lifecycle, provider/TTS sync, and local VRChat OSC."
        ),
    )
