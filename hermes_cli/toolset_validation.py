"""Validation for toolset-related config declarations.

Pure, side-effect-free helpers so the logic is unit-testable without importing
the tool registry or launching Hermes (mirrors the decoupled-helper pattern used
elsewhere in the CLI).

Motivated by #38798: a config migration silently rewrote the valid toolset name
``hermes-cli`` to the non-existent ``hermes``. ``resolve_toolset('hermes')``
returns an empty list, so every tool silently disappeared with no error, warning,
or log entry — the agent degraded to text-only replies and the cause took
significant debugging to find. Surfacing invalid toolset names (and the
zero-tools end state) loudly turns that silent failure into an actionable one.
"""

from collections.abc import Mapping
from typing import Callable, Collection, List, Optional

from agent.skill_utils import parse_config_string_list


LEGACY_TOOLSET_NAMES = frozenset({
    "web_tools",
    "terminal_tools",
    "vision_tools",
    "image_tools",
    "skills_tools",
    "browser_tools",
    "cronjob_tools",
    "file_tools",
    "tts_tools",
})


def effective_toolset_validator(
    config: object,
    is_valid_toolset: Callable[[str], bool],
) -> Callable[[str], bool]:
    """Include configured extension toolsets in a validity predicate.

    Plugin and MCP aliases are registered dynamically, after some config
    diagnostics run.  Discover their declared names through the same cached
    APIs used by the toolset picker so an early doctor or migration pass does
    not mislabel a valid extension deny as inert.
    """
    dynamic_names = set()
    if isinstance(config, dict):
        mcp_servers = config.get("mcp_servers")
        if isinstance(mcp_servers, Mapping):
            dynamic_names.update(
                name for name in mcp_servers if isinstance(name, str) and name
            )

    try:
        from hermes_cli.plugins import (
            get_plugin_toolset_keys_nowait,
            get_portable_mcp_server_names_nowait,
        )

        dynamic_names.update(get_plugin_toolset_keys_nowait())
        dynamic_names.update(get_portable_mcp_server_names_nowait())
    except Exception:
        # Diagnostics remain best-effort. Runtime validation after discovery is
        # authoritative and will still warn about genuinely unknown names.
        pass

    return lambda name: is_valid_toolset(name) or name in dynamic_names


def validate_platform_toolsets(
    platform_toolsets: object,
    is_valid_toolset: Callable[[str], bool],
) -> List[str]:
    """Return human-readable warnings for a ``platform_toolsets`` mapping.

    Two failure modes are reported:

    1. A toolset name that ``is_valid_toolset`` rejects — usually a corrupted or
       renamed entry. When ``hermes-<platform>`` would have been valid (the exact
       #38798 shape, where ``cli`` held ``hermes`` instead of ``hermes-cli``),
       the warning includes that as a suggestion.
    2. The mapping is non-empty but resolves to *zero* valid toolsets, so the
       agent would start with no tools at all.

    ``is_valid_toolset`` is injected (normally :func:`toolsets.validate_toolset`)
    so this function performs no imports or I/O and is testable in isolation.

    Args:
        platform_toolsets: The raw ``platform_toolsets`` value from config. Only
            ``dict`` values carry toolset entries; anything else yields no
            warnings (nothing to validate).
        is_valid_toolset: Predicate returning ``True`` for a known toolset name.

    Returns:
        A list of warning strings (empty when everything is valid).
    """
    warnings: List[str] = []
    if not isinstance(platform_toolsets, dict) or not platform_toolsets:
        return warnings

    valid_count = 0
    for platform, raw in platform_toolsets.items():
        names = raw if isinstance(raw, list) else [raw]
        for name in names:
            if not isinstance(name, str) or not name:
                continue
            if is_valid_toolset(name):
                valid_count += 1
                continue
            suggestion = f"hermes-{platform}"
            hint = (
                f" — did you mean '{suggestion}'?"
                if is_valid_toolset(suggestion)
                else ""
            )
            warnings.append(
                f"platform '{platform}' references unknown toolset "
                f"'{name}'{hint}"
            )

    if valid_count == 0:
        warnings.append(
            "platform_toolsets resolves to zero valid toolsets — the agent will "
            "have no tools. Run `hermes tools` to reconfigure."
        )
    return warnings


def validate_disabled_toolset_declarations(
    config: object,
    is_valid_toolset: Callable[[str], bool],
    *,
    environ: Optional[Mapping[str, str]] = None,
    legacy_names: Collection[str] = LEGACY_TOOLSET_NAMES,
) -> List[str]:
    """Report deny declarations that cannot affect the active tool surface.

    Values are only named when they are toolset identifiers; arbitrary config
    and environment values are never included in diagnostics.
    """
    warnings: List[str] = []
    if environ is not None and "HERMES_DISABLED_TOOLSETS" in environ:
        warnings.append(
            "HERMES_DISABLED_TOOLSETS is not supported and has no effect; "
            "configure agent.disabled_toolsets in config.yaml instead"
        )

    if not isinstance(config, dict):
        return warnings

    if "disabled_toolsets" in config:
        warnings.append(
            "root-level 'disabled_toolsets' has no effect; move it under "
            "agent.disabled_toolsets"
        )

    agent_config = config.get("agent")
    if not isinstance(agent_config, dict):
        return warnings

    for raw_name in parse_config_string_list(agent_config.get("disabled_toolsets")):
        name = raw_name.strip()
        if not name or is_valid_toolset(name) or name in legacy_names:
            continue
        warnings.append(
            f"agent.disabled_toolsets references unknown toolset '{name}'; "
            "this entry has no effect. Run `hermes tools list` to see valid "
            "toolset names"
        )
    return warnings
