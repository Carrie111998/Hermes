"""Read-only collection adapters for the AgentOps Phase 2 observer."""

from plugins.agentops.control.collectors.base import Collector, collect_all

__all__ = ("Collector", "collect_all")
