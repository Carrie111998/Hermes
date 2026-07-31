"""Antigravity CLI (via MCP Bridge) provider profile."""

from providers import register_provider
from providers.base import ProviderProfile

antigravity_provider = ProviderProfile(
    name="antigravity",
    aliases=("agy", "antigravity_cli"),
    api_mode="antigravity_mcp",
    env_vars=(),
    base_url="mcp://antigravity-cli",
    auth_type="mcp_local",
)

register_provider(antigravity_provider)
