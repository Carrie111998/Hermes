"""Profile-based routing for the gateway with hierarchical matching.

Allows a single Hermes instance to route specific Discord guilds/channels/threads
to different profiles — each with their own model, tools, memory, and persona.

Matching priority (most specific first):
  1. platform + chat_id + thread_id (exact thread)  — specificity 14
  2. platform + chat_id (channel route)             — specificity 6
  3. platform + guild_id (guild/server route)       — specificity 2
  4. No match                                       → default profile

Parent-chain matching:
For Discord threads and forum posts, ``parent_chat_id`` carries the
direct parent (the channel for a thread, the forum channel for a post).
Routes keyed on a channel match both direct messages and messages in
any thread/post whose parent is that channel.

Configuration (config.yaml):

    gateway:
      profile_routes:
        - name: server-default
          platform: discord
          guild_id: "YOUR_GUILD_ID"
          profile: server-profile

        - name: special-channel
          platform: discord
          guild_id: "YOUR_GUILD_ID"
          chat_id: "YOUR_CHANNEL_ID"
          profile: channel-profile

        - name: thread-route
          platform: discord
          chat_id: "YOUR_CHANNEL_ID"
          thread_id: "YOUR_THREAD_ID"
          profile: thread-profile
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import logging

logger = logging.getLogger(__name__)


class ProfileRouteRejected(RuntimeError):
    """An explicit route matched a profile this gateway does not serve."""


@dataclass(frozen=True)
class ProfileRouteAuthorization:
    """Principals admitted only when their profile route matches."""

    allowed_users: tuple[str, ...] = ()
    allowed_roles: tuple[int, ...] = ()


@dataclass(frozen=True)
class ProfileRoute:
    """A single routing rule that maps a platform scope to a profile."""

    name: str
    platform: str
    profile: str
    guild_id: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None
    enabled: bool = True
    authorization: Optional[ProfileRouteAuthorization] = None

    @property
    def specificity(self) -> int:
        """Higher value = more specific match."""
        s = 0
        if self.guild_id:
            s += 2
        if self.chat_id:
            s += 4
        if self.thread_id:
            s += 8
        return s

    def matches(
        self,
        platform: str,
        guild_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        parent_chat_id: Optional[str] = None,
    ) -> bool:
        """Return True if this route matches the given source fields.

        All configured discriminators are matched conjunctively (AND): every
        discriminator that the route declares must hold. ``chat_id`` supports
        hierarchical matching for Discord forums/threads:
        - Direct channel match: chat_id == route.chat_id
        - Thread in channel: parent_chat_id == route.chat_id
        A route declaring both ``guild_id`` and ``chat_id`` requires both to
        match (a chat match alone does not satisfy a guild constraint).
        """
        if not self.enabled:
            return False
        if self.platform != platform:
            return False
        if self.thread_id and self.thread_id != thread_id:
            return False
        if self.chat_id and self.chat_id != chat_id and self.chat_id != parent_chat_id:
            return False
        if self.guild_id and self.guild_id != guild_id:
            return False
        return True


def _parse_discord_snowflakes(values: Any, *, route_name: str, field_name: str) -> frozenset[str]:
    """Validate one route-scoped Discord principal list."""
    if not isinstance(values, (list, tuple)):
        raise ValueError(
            f"profile route {route_name!r} authorization.{field_name} must be a list"
        )
    normalized = frozenset(str(value).strip() for value in values)
    if any(not value.isdigit() or int(value) <= 0 for value in normalized):
        raise ValueError(
            f"profile route {route_name!r} authorization.{field_name} "
            "must contain positive decimal Discord IDs"
        )
    return normalized


def _parse_route_authorization(
    entry: Dict[str, Any], *, name: str, platform: str
) -> Optional[ProfileRouteAuthorization]:
    """Parse an optional fail-closed transport authorization policy."""
    if "authorization" not in entry:
        return None
    raw = entry.get("authorization")
    if not isinstance(raw, dict):
        raise ValueError(f"profile route {name!r} authorization must be a mapping")
    if platform != "discord":
        raise ValueError(
            f"profile route {name!r} authorization is currently supported only for discord"
        )
    unknown = set(raw) - {"allowed_users", "allowed_roles"}
    if unknown:
        raise ValueError(
            f"profile route {name!r} authorization has unknown field(s): "
            f"{', '.join(sorted(unknown))}"
        )
    users = _parse_discord_snowflakes(
        raw.get("allowed_users", []), route_name=name, field_name="allowed_users"
    )
    roles = _parse_discord_snowflakes(
        raw.get("allowed_roles", []), route_name=name, field_name="allowed_roles"
    )
    if roles and not entry.get("guild_id"):
        raise ValueError(
            f"profile route {name!r} authorization.allowed_roles requires guild_id"
        )
    return ProfileRouteAuthorization(
        allowed_users=tuple(sorted(users)),
        allowed_roles=tuple(sorted(int(role_id) for role_id in roles)),
    )


def parse_profile_routes(raw: Optional[List[Dict[str, Any]]]) -> List[ProfileRoute]:
    """Parse profile_routes from config.yaml into ProfileRoute objects.

    Returns routes sorted by specificity (most specific first).
    """
    if not raw:
        return []
    routes: List[ProfileRoute] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        platform = entry.get("platform", "")
        profile = entry.get("profile", "")
        if not platform or not profile:
            if "authorization" in entry:
                raise ValueError(
                    f"profile route {name or '<unnamed>'!r} with authorization "
                    "requires platform and profile"
                )
            logger.warning(
                "Skipping profile route %s: missing platform or profile",
                name,
            )
            continue
        # Validate profile name to prevent path traversal. Lazy import avoids a
        # circular dependency at module load time.
        try:
            from hermes_cli.profiles import (
                normalize_profile_name,
                validate_profile_name,
            )
            profile = normalize_profile_name(profile)
            validate_profile_name(profile)
        except (ValueError, ImportError) as exc:
            if "authorization" in entry:
                raise ValueError(
                    f"profile route {name or '<unnamed>'!r} with authorization "
                    f"has invalid profile name {profile!r}"
                ) from exc
            logger.warning("Skipping profile route %s: invalid profile name %r", name, profile)
            continue
        authorization = _parse_route_authorization(
            entry, name=name or profile, platform=platform
        )
        route_ids = {
            discriminator: entry.get(discriminator)
            for discriminator in ("guild_id", "chat_id", "thread_id")
        }
        if authorization is not None:
            if "enabled" in entry and not isinstance(entry["enabled"], bool):
                raise ValueError(
                    f"profile route {name or profile!r} authorization requires enabled to be boolean"
                )
            for discriminator in ("guild_id", "chat_id", "thread_id"):
                value = entry.get(discriminator)
                if value is None:
                    continue
                normalized = str(value).strip()
                if not normalized.isdigit() or int(normalized) <= 0:
                    raise ValueError(
                        f"profile route {name or profile!r} {discriminator} must be a "
                        "positive decimal Discord ID when authorization is configured"
                    )
                route_ids[discriminator] = normalized
        if authorization is not None and profile == "default":
            raise ValueError(
                f"profile route {name or profile!r} authorization cannot grant "
                "access to the privileged default profile"
            )
        routes.append(
            ProfileRoute(
                name=name,
                platform=platform,
                profile=profile,
                guild_id=route_ids["guild_id"],
                chat_id=route_ids["chat_id"],
                thread_id=route_ids["thread_id"],
                enabled=entry.get("enabled", True),
                authorization=authorization,
            )
        )
    # Sort: most specific first so the first match wins.
    routes.sort(key=lambda r: r.specificity, reverse=True)
    logger.debug("Loaded %d profile routes (most-specific-first)", len(routes))
    return routes


def match_profile_route(
    routes: List[ProfileRoute],
    platform: str,
    guild_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    parent_chat_id: Optional[str] = None,
) -> Optional[ProfileRoute]:
    """Return the best-matching route, or None for no match."""
    for route in routes:
        if route.matches(platform, guild_id=guild_id, chat_id=chat_id, thread_id=thread_id, parent_chat_id=parent_chat_id):
            return route
    return None
