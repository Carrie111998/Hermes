"""Exact structural validation for user-configured MCP server entries.

MCP stdio transports intentionally execute arbitrary user-selected programs.
The host therefore validates only the transport contract it must satisfy: one
transport, non-empty transport coordinates, and SDK-compatible argument,
environment, and header shapes.  It does not interpret command, argument, or
environment text and does not classify programs, shells, destinations, or
intent.

Execution safety belongs to explicit authorization and process boundaries
(profile-scoped config writes, filtered inherited environment, executable-path
resolution, child-process ownership, and teardown), not prose heuristics.
"""
from __future__ import annotations

from typing import Any


def _validate_string_mapping(
    name: str,
    field: str,
    value: Any,
) -> list[str]:
    if not isinstance(value, dict):
        return [f"MCP server '{name}' field '{field}' must be an object"]

    issues: list[str] = []
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            issues.append(
                f"MCP server '{name}' field '{field}' keys must be non-empty strings"
            )
        if not isinstance(item, str):
            issues.append(
                f"MCP server '{name}' field '{field}.{key}' must be a string"
            )
    return issues


def validate_mcp_server_entry(name: str, entry: Any) -> list[str]:
    """Return exact MCP transport/schema violations for *entry*.

    Values inside ``command``, ``args``, ``env``, and ``headers`` are opaque.
    A structurally valid entry receives the same verdict regardless of the
    words, executable family, addresses, paths, or payloads it contains.
    """
    if not isinstance(name, str) or not name.strip():
        return ["MCP server name must be a non-empty string"]
    if not isinstance(entry, dict):
        return [f"MCP server '{name}' must be an object"]

    issues: list[str] = []

    url = entry.get("url")
    command = entry.get("command")
    has_url = isinstance(url, str) and bool(url.strip())
    has_command = isinstance(command, str) and bool(command.strip())

    if "url" in entry and not has_url:
        issues.append(f"MCP server '{name}' field 'url' must be a non-empty string")
    if "command" in entry and not has_command:
        issues.append(
            f"MCP server '{name}' field 'command' must be a non-empty string"
        )
    if has_url == has_command:
        issues.append(
            f"MCP server '{name}' must define exactly one of 'url' or 'command'"
        )

    if "args" in entry:
        args = entry["args"]
        if not isinstance(args, list):
            issues.append(f"MCP server '{name}' field 'args' must be a list")
        else:
            for index, item in enumerate(args):
                if not isinstance(item, str):
                    issues.append(
                        f"MCP server '{name}' field 'args[{index}]' must be a string"
                    )

    if "env" in entry:
        issues.extend(_validate_string_mapping(name, "env", entry["env"]))
    if "headers" in entry:
        issues.extend(
            _validate_string_mapping(name, "headers", entry["headers"])
        )

    if has_url and "args" in entry:
        issues.append(
            f"MCP server '{name}' field 'args' is only valid for stdio transport"
        )
    if has_url and "env" in entry:
        issues.append(
            f"MCP server '{name}' field 'env' is only valid for stdio transport"
        )

    return issues
