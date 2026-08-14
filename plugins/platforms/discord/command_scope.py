"""Discord command guild-scope + installation-context normalization.

Pure logic module (no Discord API dependency) used to decide a slash
command's guild scope and integration (installation) context.
"""

from __future__ import annotations

from dataclasses import dataclass

# Discord snowflakes are unsigned 63-bit integers in [1, 2**63 - 1].
MAX_SNOWFLAKE = (1 << 63) - 1

# Discord integration types (ApplicationIntegrationType):
#   0 = GUILD_INSTALL, 1 = USER_INSTALL
GUILD_INSTALL = 0
USER_INSTALL = 1


class CommandScopeError(ValueError):
    """Raised when a command scope value is invalid."""


@dataclass
class CommandScope:
    """Resolved scope for a Discord command.

    ``guild_id`` is the snowflake the command is restricted to (``None``
    means no guild restriction). ``integration_types`` is the normalized
    list of installation contexts the command is available in.
    """

    guild_id: str | None
    integration_types: list[int]


def _is_snowflake(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    # ASCII-only digit check: str.isdigit() accepts non-ASCII digits
    # (e.g. Arabic-Indic "١٢٣") and superscripts such as "²", which int()
    # cannot parse or would parse as values Discord never uses.
    if not (value.isascii() and value.isdigit()):
        return False
    return 1 <= int(value) <= MAX_SNOWFLAKE


def _validate_snowflake(value: str, label: str) -> None:
    if not _is_snowflake(value):
        raise CommandScopeError(
            f"{label} must be a Discord snowflake "
            f"(string of digits in 1..{MAX_SNOWFLAKE}), got {value!r}"
        )


def resolve_guild_scope(
    config_guild: str | None, *, default_guild: str | None = None
) -> str | None:
    """Resolve the guild a command should be scoped to.

    ``config_guild`` wins when provided; otherwise ``default_guild`` is
    used; otherwise the command is not guild-scoped (``None``). Any
    provided value that is not a valid Discord snowflake raises
    :class:`CommandScopeError`.
    """
    if config_guild is not None:
        _validate_snowflake(config_guild, "config_guild")
        return config_guild
    if default_guild is not None:
        _validate_snowflake(default_guild, "default_guild")
        return default_guild
    return None


def normalize_integration_types(types: list | None) -> list[int]:
    """Normalize a raw integration-types list to valid Discord values.

    ``None`` or an empty list maps to ``[0]`` (GUILD_INSTALL). Only
    ``0`` (guild install) and ``1`` (user install) are accepted; any
    other value raises :class:`CommandScopeError`. Duplicates are removed
    preserving first-seen order.
    """
    if types is None:
        return [GUILD_INSTALL]
    normalized: list[int] = []
    for raw in types:
        if (
            isinstance(raw, bool)
            or not isinstance(raw, int)
            or raw not in (GUILD_INSTALL, USER_INSTALL)
        ):
            raise CommandScopeError(
                "integration type must be 0 (guild install) or 1 (user install), "
                f"got {raw!r}"
            )
        if raw not in normalized:
            normalized.append(raw)
    return normalized or [GUILD_INSTALL]


def is_guild_scoped(scope: CommandScope) -> bool:
    """True when the scope is guild-install only (integration_types == [0])."""
    return scope.integration_types == [GUILD_INSTALL]
