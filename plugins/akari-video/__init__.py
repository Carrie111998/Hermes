from __future__ import annotations

from . import core
from .cli import akari_video_command, register_cli


def register(ctx) -> None:
    ctx.register_tool(
        name="akari_video_status",
        toolset=core.TOOLSET,
        schema=core.STATUS_SCHEMA,
        handler=core.handle_status,
        check_fn=lambda: True,
        description=core.STATUS_SCHEMA["description"],
        emoji="AK",
    )
    ctx.register_tool(
        name="akari_video_skills",
        toolset=core.TOOLSET,
        schema=core.SKILLS_SCHEMA,
        handler=core.handle_skills,
        check_fn=lambda: True,
        description=core.SKILLS_SCHEMA["description"],
        emoji="AK",
    )
    ctx.register_tool(
        name="akari_video_launch",
        toolset=core.TOOLSET,
        schema=core.LAUNCH_SCHEMA,
        handler=core.handle_launch,
        check_fn=core.check_available,
        description=core.LAUNCH_SCHEMA["description"],
        emoji="AK",
    )
    ctx.register_command(
        "akari-video",
        handler=core.handle_slash,
        description="Inspect the AKARI Video submodule status and launch the launcher.",
        args_hint="[status|skills|launch]",
    )
    ctx.register_cli_command(
        name="akari-video",
        help="Run AKARI Video launcher through Hermes",
        setup_fn=register_cli,
        handler_fn=akari_video_command,
        description=(
            "Use AKARI Video's akari launcher (packages/akari-launcher/bin/akari.mjs) "
            "from Hermes with workspace isolation and receipt tracking."
        ),
    )