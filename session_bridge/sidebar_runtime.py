"""Process-local runtime configuration for native sidebar registration."""

from __future__ import annotations

_SIDEBAR_REGISTRATION_APP_SERVER_ARGS = (
    "-c",
    "mcp_servers={}",
    "--disable",
    "plugins",
)


def sidebar_registration_app_server_args() -> list[str]:
    """Return lean Codex app-server arguments for broker-owned registration."""

    return list(_SIDEBAR_REGISTRATION_APP_SERVER_ARGS)
