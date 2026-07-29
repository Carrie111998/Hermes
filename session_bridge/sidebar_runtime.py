"""Process-local runtime configuration for native sidebar registration."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re


_MCP_SERVER_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


def configured_mcp_server_names(response: object) -> tuple[str, ...]:
    """Extract configured server names from one app-server config/read result."""

    if not isinstance(response, Mapping):
        raise ValueError("config/read response must be an object")
    config = response.get("config")
    if not isinstance(config, Mapping):
        raise ValueError("config/read response must contain a config object")
    servers = config.get("mcp_servers")
    if not isinstance(servers, Mapping):
        raise ValueError("config/read mcp_servers must be an object")
    return _validated_server_names(servers)


def sidebar_registration_app_server_args(
    mcp_server_names: Iterable[str],
) -> list[str]:
    """Return lean Codex app-server arguments for broker-owned registration."""

    args = [
        "--disable",
        "apps",
        "--disable",
        "plugins",
    ]
    for name in _validated_server_names(mcp_server_names):
        args.extend((
            "-c",
            f"mcp_servers.{name}.enabled=false",
        ))
    return args


def _validated_server_names(value: object) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        entries = value.keys()
    elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        entries = value
    else:
        raise ValueError("MCP server names must be an iterable")
    names: set[str] = set()
    for entry in entries:
        if (
            not isinstance(entry, str)
            or not entry
            or entry != entry.strip()
            or _MCP_SERVER_NAME_RE.fullmatch(entry) is None
        ):
            raise ValueError("MCP server name is malformed")
        names.add(entry)
    return tuple(sorted(names))
