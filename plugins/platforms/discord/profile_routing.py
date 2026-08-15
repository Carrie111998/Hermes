"""Discord profile routing acceptance matrix (W3).

Pure logic: routes message deliveries to a registered, adapter-ready profile.
Fail-closed: unknown profiles and unready adapters always raise
:class:`ProfileRoutingError` (a ``ValueError`` subclass).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


class ProfileRoutingError(ValueError):
    """Raised when a delivery cannot be routed to a profile/adapter."""


@dataclass(frozen=True)
class ProfileRoute:
    """A resolved route target: a profile id and its adapter readiness."""

    profile_id: str
    adapter_ready: bool


class ProfileRouter:
    """Routes deliveries to registered, adapter-ready profiles."""

    def __init__(self) -> None:
        self._routes: Dict[str, ProfileRoute] = {}

    def register(self, profile_id: str, adapter_ready: bool = True) -> None:
        """Register (or re-register) a profile with its adapter readiness."""
        self._routes[profile_id] = ProfileRoute(
            profile_id=profile_id, adapter_ready=adapter_ready
        )

    def resolve(self, profile_id: str) -> ProfileRoute:
        """Return the route for a profile, or raise if unknown/unready."""
        route = self._routes.get(profile_id)
        if route is None or not route.adapter_ready:
            raise ProfileRoutingError(
                f"profile {profile_id!r} is not registered or its adapter is not ready"
            )
        return route

    def has_profile(self, profile_id: str) -> bool:
        """True if the profile has been registered (regardless of readiness)."""
        return profile_id in self._routes

    def route_for(self, channel_id: str, profile_map: dict) -> str:
        """Map a channel id to a profile id, fail-closed on any gap.

        Raises :class:`ProfileRoutingError` if the channel is missing from
        ``profile_map`` or the mapped profile is not registered/ready.
        """
        if channel_id not in profile_map:
            raise ProfileRoutingError(
                f"channel {channel_id!r} is not present in the profile map"
            )
        profile_id = profile_map[channel_id]
        route = self.resolve(profile_id)
        return route.profile_id
