"""Deep-merge helpers for shared config ⊕ machine overlay."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


# Keys that belong on a per-machine overlay (not shared profile config).
MACHINE_LOCAL_TOP_KEYS = frozenset({
    "terminal",
    "browser",
    "mcp_servers",
    "environment_hint",
    "agent.environment_hint",
})

MACHINE_LOCAL_TERMINAL_KEYS = frozenset({
    "cwd", "backend", "sandbox_dir", "shell_init_files", "home_mode",
    "env_passthrough", "docker_image", "docker_volumes", "docker_network",
    "docker_env", "docker_forward_env", "docker_extra_args", "docker_run_as_host_user",
    "ssh_host", "ssh_user", "ssh_port", "ssh_key", "singularity_image",
    "container_cpu", "container_memory", "container_disk",
})


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge overlay onto a deepcopy of base (overlay wins)."""
    result = deepcopy(base) if base else {}
    if not overlay:
        return result
    for key, value in overlay.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def extract_machine_overlay(config: dict[str, Any]) -> dict[str, Any]:
    """Pull machine-local keys out of a full config for storage in machine_*."""
    overlay: dict[str, Any] = {}
    terminal = config.get("terminal")
    if isinstance(terminal, dict):
        local_term = {
            k: deepcopy(v) for k, v in terminal.items()
            if k in MACHINE_LOCAL_TERMINAL_KEYS
        }
        if local_term:
            overlay["terminal"] = local_term

    browser = config.get("browser")
    if isinstance(browser, dict) and browser:
        overlay["browser"] = deepcopy(browser)

    mcp = config.get("mcp_servers")
    if isinstance(mcp, dict) and mcp:
        overlay["mcp_servers"] = deepcopy(mcp)

    platforms = config.get("platforms")
    if isinstance(platforms, dict):
        api = platforms.get("api_server")
        if isinstance(api, dict) and api:
            overlay.setdefault("platforms", {})["api_server"] = {
                k: deepcopy(v) for k, v in api.items()
                if k in ("host", "port", "cors_origins")
            }

    agent = config.get("agent")
    if isinstance(agent, dict):
        hint = agent.get("environment_hint")
        if hint:
            overlay.setdefault("agent", {})["environment_hint"] = deepcopy(hint)

    return overlay


def strip_machine_local(config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of config without machine-local keys (for shared profile)."""
    result = deepcopy(config)
    if "terminal" in result and isinstance(result["terminal"], dict):
        for key in list(result["terminal"].keys()):
            if key in MACHINE_LOCAL_TERMINAL_KEYS:
                result["terminal"].pop(key, None)
    result.pop("browser", None)
    result.pop("mcp_servers", None)
    platforms = result.get("platforms")
    if isinstance(platforms, dict):
        api = platforms.get("api_server")
        if isinstance(api, dict):
            for key in ("host", "port"):
                api.pop(key, None)
    agent = result.get("agent")
    if isinstance(agent, dict):
        agent.pop("environment_hint", None)
    return result
