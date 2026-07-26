"""Cost and subscription-bridge control errors."""

from __future__ import annotations


class SubscriptionBridgeHaltedError(RuntimeError):
    """Raised before a Pro-bridge dispatch when its turn budget is halted."""


class ProgrammeGatePausedAtIngress(Exception):
    """Programme is not RUNNING; reject a new turn before dispatch or spend."""


__all__ = [
    "ProgrammeGatePausedAtIngress",
    "SubscriptionBridgeHaltedError",
]
