"""Frozen internal budgets for the ChatGPT Pro subscription bridge."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BridgeCaps:
    """Conservative turn and latency thresholds for the Pro bridge."""

    soft_turns_daily: int = 500
    hard_turns_daily: int = 800
    degraded_latency_ms: int = 15_000
    nightly_probe_hour_utc: int = 14


BRIDGE_CAPS = BridgeCaps()


__all__ = ["BRIDGE_CAPS", "BridgeCaps"]
