"""Minimal, read-only evidence for the active runtime route.

The projection is deliberately observational: it never persists, repairs,
reroutes, or exposes endpoint credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


def _host(value: object) -> str:
    parsed = urlsplit(str(value or "").strip())
    return (parsed.hostname or "").lower()


@dataclass(frozen=True)
class ActiveRouteSnapshot:
    provider: str
    model: str
    base_url_host: str
    fallback_active: bool
    cooldown_remaining_s: float


class RoutingSnapshotAdapter:
    """Build credential-free active-route evidence without mutating the agent."""

    @classmethod
    def from_agent(
        cls,
        agent: Any,
        *,
        captured_at_monotonic: float,
    ) -> ActiveRouteSnapshot:
        return ActiveRouteSnapshot(
            provider=str(getattr(agent, "provider", "") or ""),
            model=str(getattr(agent, "model", "") or ""),
            base_url_host=_host(getattr(agent, "base_url", "")),
            fallback_active=bool(getattr(agent, "_fallback_activated", False)),
            cooldown_remaining_s=max(
                0.0,
                float(getattr(agent, "_rate_limited_until", 0) or 0)
                - captured_at_monotonic,
            ),
        )
