"""Validation for the ``platform_toolsets`` config section.

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

from typing import Callable, List


def validate_platform_toolsets(
    platform_toolsets: object,
    is_valid_toolset: Callable[[str], bool],
    plugin_toolset_names: object = None,
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
        plugin_toolset_names: Optional iterable or per-platform mapping of
            plugin-registered toolset names (e.g. ``eikon``, ``buzz``, ``a2a``).
            These only appear in the tool registry after plugins load — which
            is after config-time validation runs — so ``is_valid_toolset``
            alone flags them as unknown even though they resolve fine at
            runtime. Two accepted shapes:

            - An iterable of names: every name is treated as valid for *every*
              platform (legacy behavior; callers passing ``known_plugin_toolsets``
              as a flat union use this).
            - A ``{platform: [names]}`` mapping: names are valid only for their
              own platform. This preserves the per-platform precision that
              ``known_plugin_toolsets`` encodes — a plugin known only for one
              platform cannot mask an invalid entry on another.

    Returns:
        A list of warning strings (empty when everything is valid).
    """
    warnings: List[str] = []
    if not isinstance(platform_toolsets, dict) or not platform_toolsets:
        return warnings

    # Normalize plugin names to per-platform lookup. Mapping input is used
    # keyed by platform; a flat iterable is replicated to every platform so
    # both shapes share one code path below.
    if isinstance(plugin_toolset_names, dict):
        known_plugin = {
            platform: {n for n in names if isinstance(n, str)}
            for platform, names in plugin_toolset_names.items()
        }
    else:
        _flat = {
            n for n in (plugin_toolset_names or ()) if isinstance(n, str)
        }
        known_plugin = {platform: set(_flat) for platform in platform_toolsets}

    def _is_valid(name: str, platform: str) -> bool:
        return bool(is_valid_toolset(name)) or name in known_plugin.get(
            platform, ()
        )

    valid_count = 0
    for platform, raw in platform_toolsets.items():
        names = raw if isinstance(raw, list) else [raw]
        for name in names:
            if not isinstance(name, str) or not name:
                continue
            if _is_valid(name, platform):
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
