"""Profile-based routing for the gateway with hierarchical matching.

Allows a single Hermes instance to route specific senders or Discord
guilds/channels/threads to different profiles — each with their own model,
tools, memory, and persona.

Matching priority (most specific first):
  1. sender + location constraints                  — specificity 18-30
  2. platform + user_id (sender route)              — specificity 16
  3. platform + chat_id + thread_id (exact thread)  — specificity 14
  4. platform + chat_id (channel route)             — specificity 6
  5. platform + guild_id (guild/server route)       — specificity 2
  6. No match                                       → default profile

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

        - name: sender-route
          platform: discord
          user_id: "YOUR_USER_ID"
          profile: sender-profile
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProfileRoute:
    """A single routing rule that maps a platform scope to a profile."""

    name: str
    platform: str
    profile: str
    guild_id: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None
    user_id: Optional[str] = None
    enabled: bool = True

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
        if self.user_id:
            s += 16
        return s

    def matches(
        self,
        platform: str,
        guild_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        parent_chat_id: Optional[str] = None,
        user_id: Optional[str] = None,
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
        if self.user_id and self.user_id != user_id:
            return False
        return True


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
        except (ValueError, ImportError):
            logger.warning("Skipping profile route %s: invalid profile name %r", name, profile)
            continue
        user_id = None
        if entry.get("user_id") is not None:
            raw_user_id = entry["user_id"]
            if isinstance(raw_user_id, bool):
                logger.warning(
                    "Skipping profile route %s: invalid user_id %r",
                    name,
                    raw_user_id,
                )
                continue
            if isinstance(raw_user_id, (list, dict, set, tuple)):
                logger.warning(
                    "Skipping profile route %s: invalid user_id %r",
                    name,
                    raw_user_id,
                )
                continue
            if isinstance(raw_user_id, float):
                logger.warning(
                    "Skipping profile route %s: invalid user_id %r",
                    name,
                    raw_user_id,
                )
                continue
            if isinstance(raw_user_id, int):
                if raw_user_id <= 0:
                    logger.warning(
                        "Skipping profile route %s: user_id must be a positive integer, got %r",
                        name,
                        raw_user_id,
                    )
                    continue
                user_id = str(raw_user_id)
            elif isinstance(raw_user_id, str):
                user_id = raw_user_id.strip()
                if not user_id:
                    logger.warning(
                        "Skipping profile route %s: empty user_id",
                        name,
                    )
                    continue
            else:
                logger.warning(
                    "Skipping profile route %s: invalid user_id %r",
                    name,
                    raw_user_id,
                )
                continue
        routes.append(
            ProfileRoute(
                name=name,
                platform=platform,
                profile=profile,
                guild_id=entry.get("guild_id"),
                chat_id=entry.get("chat_id"),
                thread_id=entry.get("thread_id"),
                user_id=user_id,
                enabled=entry.get("enabled", True),
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
    user_id: Optional[str] = None,
) -> Optional[ProfileRoute]:
    """Return the best-matching route, or None for no match."""
    for route in routes:
        if route.matches(
            platform,
            guild_id=guild_id,
            chat_id=chat_id,
            thread_id=thread_id,
            parent_chat_id=parent_chat_id,
            user_id=user_id,
        ):
            return route
    return None
