"""Hermes team-memory plugin.

The plugin is deliberately separate from ``plugins/memory``. Hermes supports
one conversational memory provider, while team memory is a small, explicitly
scoped knowledge store that agents may search without changing the provider
used for personal/session memory.
"""

from __future__ import annotations

def register(ctx) -> None:
    """Register the gated search tool and operator CLI."""
    # Keep storage-only imports (seed loaders, offline reports, migrations)
    # free of CLI/config side effects. The plugin loader calls this function
    # only after it has deliberately selected the plugin for a Hermes process.
    from .cli import register_cli, team_memory_command
    from .tool import (
        TEAM_MEMORY_SEARCH_SCHEMA,
        check_team_memory_requirements,
        handle_team_memory_search,
    )

    ctx.register_tool(
        name="team_memory_search",
        toolset="team_memory",
        schema=TEAM_MEMORY_SEARCH_SCHEMA,
        handler=handle_team_memory_search,
        check_fn=check_team_memory_requirements,
        emoji="🧠",
    )
    ctx.register_cli_command(
        name="team-memory",
        help="Manage scoped shared team memory",
        setup_fn=register_cli,
        handler_fn=team_memory_command,
        description=(
            "Initialize, inspect, search, and maintain the reviewed shared "
            "memory store. Agent writes are intentionally not exposed."
        ),
    )
