"""Backward-compatible import path for the Stage 1 plugin tool."""

from plugins.team_memory.tool import (  # noqa: F401
    TEAM_MEMORY_SEARCH_SCHEMA,
    check_team_memory_requirements,
    handle_team_memory_search,
    is_feature_enabled,
    team_memory_search,
)
