"""Hermes Control Center (Phase D of ADR-0020).

Lightweight FastAPI + HTMX dashboard at http://localhost:9120 that gives
Diego a single pane of glass for:

  * Approval queue: paused LangGraph runs awaiting HITL approval
  * Critic proposals queue: items awaiting apply/skip decision
  * Health summary: read from architecture-map/status.json (60 probes)
  * Activity feed: recent events from ~/.hermes/events/event_bus.db
  * Quick links: Langfuse, DevFlow Mission Control, Telegram supergroup,
    existing Hermes admin dashboard at :9119

Why a separate port (:9120 not :9119): :9119 already runs the React-based
Hermes admin dashboard with a Vite plugin system. Adding a new tab there
requires a UMD bundle build pipeline. Phase D MVP ships HTML+HTMX directly
(no JS toolchain), keeping the runner trivial.
"""

from .app import app

__all__ = ["app"]
