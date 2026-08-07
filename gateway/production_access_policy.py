"""Exact production owner/team capability projection.

This module is deliberately mechanical.  It does not classify intent or infer
authority from message text, names, roles, or channel content.  The privileged
owner role exists only when the authenticated Discord connector supplied the
one reviewed author ID.  Every other admitted Discord author receives the
fixed team projection for the lifetime of that agent instance.

The projection is resolved before :class:`run_agent.AIAgent` construction so
its system-prompt inputs and tool schema remain byte-stable for the whole
conversation.  It never mutates a live agent's tool set mid-conversation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from gateway.support_ops_team_registry import TEAM_MEMBERS


PRODUCTION_ACCESS_SCHEMA = "muncho-production-owner-team-access.v2"
PRODUCTION_OWNER_DISCORD_USER_ID = "1279454038731264061"
PRODUCTION_PLAN_OPERATOR_DISCORD_USER_IDS = frozenset(
    member.discord_user_id
    for member in TEAM_MEMBERS
    if member.discord_user_id != PRODUCTION_OWNER_DISCORD_USER_ID
)
PRODUCTION_TOP_TRUSTED_OPERATOR_DISCORD_USER_IDS = frozenset(
    {
        "1282938967888498720",  # Nassi
        "1391703330711142472",  # Ivs
        "1282940511962791959",  # Alex
        "1282940574533423125",  # Plamenka
    }
)
if not PRODUCTION_TOP_TRUSTED_OPERATOR_DISCORD_USER_IDS <= (
    PRODUCTION_PLAN_OPERATOR_DISCORD_USER_IDS
):
    raise RuntimeError("top-trusted operators must be registered team operators")

OWNER_ROLE = "owner"
TEAM_ROLE = "team"

# These commands are read-only and scoped to the caller's current gateway
# interaction.  Commands with mixed read/write subcommands (for example
# /tools and /skills) stay owner-only because argument-level authorization
# would create a second, easier-to-drift permission surface.
TEAM_SLASH_COMMANDS = frozenset(
    {
        "commands",
        "help",
        "status",
        "usage",
        "version",
        "whoami",
    }
)

TEAM_REMOVED_TOOLSETS = frozenset({"skills"})
TEAM_ADDED_TOOLSETS = frozenset({"skills_readonly"})

# Interactive owner-only management surface.  These are added solely from the
# connector-authenticated owner identity; they are never inherited by cron
# agents or team sessions and never selected from message text.
OWNER_ADDED_TOOLSETS = frozenset({"cronjob"})


@dataclass(frozen=True)
class ProductionAgentAccess:
    """Frozen inputs that must be applied at AIAgent construction."""

    role: str
    enabled_toolsets: tuple[str, ...]
    skip_memory: bool


def _platform_value(source: Any) -> str:
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "")


def production_discord_role(source: Any) -> str | None:
    """Return the exact production role, or ``None`` for non-Discord work.

    ``delivered_via_upstream_relay`` is an internal, non-serialized marker set
    only by the privileged Discord connector transport.  Requiring the literal
    boolean ``True`` prevents a locally constructed source with the owner's ID
    from acquiring owner authority.
    """

    if _platform_value(source) != "discord":
        return None
    user_id = str(getattr(source, "user_id", "") or "")
    if (
        getattr(source, "delivered_via_upstream_relay", False) is True
        and user_id == PRODUCTION_OWNER_DISCORD_USER_ID
    ):
        return OWNER_ROLE
    return TEAM_ROLE


def project_production_agent_access(
    enabled_toolsets: Iterable[str],
    source: Any,
) -> ProductionAgentAccess | None:
    """Project a stable owner/team tool surface for a production source."""

    # The privileged writer's sealed preflight imports this module only for
    # the fixed operator ID sets.  Keep the model-facing tool dependency out
    # of that owner-runtime import closure and load it only when constructing
    # an actual gateway agent surface.
    from gateway.production_capability_prerequisites import FIRST_WAVE_TOOLSETS

    role = production_discord_role(source)
    if role is None:
        return None
    # Fail closed if a generic platform default ever grows: production users
    # may receive only the reviewed first-wave surface.  The strict production
    # config supplies that surface explicitly for Discord, but the intersection
    # is a second mechanical boundary at agent construction time.
    normalized = {
        str(item)
        for item in enabled_toolsets
        if str(item) in FIRST_WAVE_TOOLSETS
    }
    if role == OWNER_ROLE:
        normalized.update(OWNER_ADDED_TOOLSETS)
        return ProductionAgentAccess(
            role=role,
            enabled_toolsets=tuple(sorted(normalized)),
            skip_memory=False,
        )

    normalized.difference_update(TEAM_REMOVED_TOOLSETS)
    normalized.update(TEAM_ADDED_TOOLSETS)
    return ProductionAgentAccess(
        role=role,
        enabled_toolsets=tuple(sorted(normalized)),
        skip_memory=False,
    )


def production_slash_allowed(source: Any, canonical_command: str) -> bool | None:
    """Return the production Discord slash decision, or no decision.

    Owners may reach the existing command handlers (which retain their own
    approval and production route-lock boundaries).  Team members receive the
    exact read-only command set above.  This is an ID/capability ACL, never an
    intent classifier.
    """

    role = production_discord_role(source)
    if role is None:
        return None
    if role == OWNER_ROLE:
        return True
    command = str(canonical_command or "").strip().lstrip("/").lower()
    return command in TEAM_SLASH_COMMANDS


def production_access_config() -> dict[str, Any]:
    """Return the exact public, secret-free config projection."""

    return {
        "schema": PRODUCTION_ACCESS_SCHEMA,
        "owner_discord_user_id": PRODUCTION_OWNER_DISCORD_USER_ID,
        "plan_operator_discord_user_ids": sorted(
            PRODUCTION_PLAN_OPERATOR_DISCORD_USER_IDS
        ),
        "top_trusted_operator_discord_user_ids": sorted(
            PRODUCTION_TOP_TRUSTED_OPERATOR_DISCORD_USER_IDS
        ),
        "owner_added_toolsets": sorted(OWNER_ADDED_TOOLSETS),
        "team_agent": {
            "skip_memory": False,
            "added_toolsets": sorted(TEAM_ADDED_TOOLSETS),
            "removed_toolsets": sorted(TEAM_REMOVED_TOOLSETS),
        },
        "team_slash_commands": sorted(TEAM_SLASH_COMMANDS),
    }


__all__ = [
    "OWNER_ROLE",
    "OWNER_ADDED_TOOLSETS",
    "PRODUCTION_ACCESS_SCHEMA",
    "PRODUCTION_OWNER_DISCORD_USER_ID",
    "PRODUCTION_PLAN_OPERATOR_DISCORD_USER_IDS",
    "PRODUCTION_TOP_TRUSTED_OPERATOR_DISCORD_USER_IDS",
    "ProductionAgentAccess",
    "TEAM_REMOVED_TOOLSETS",
    "TEAM_ROLE",
    "TEAM_SLASH_COMMANDS",
    "production_access_config",
    "production_discord_role",
    "production_slash_allowed",
    "project_production_agent_access",
]
