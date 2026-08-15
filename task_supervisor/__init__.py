"""Deterministic active-task supervisor primitives."""

from .watchdog import WatchdogDecision, run_watchdog

__all__ = ["WatchdogDecision", "run_watchdog"]
