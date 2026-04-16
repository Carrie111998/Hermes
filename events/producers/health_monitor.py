"""GatewayHealthMonitor — emits events on platform health state changes.

Only emits gateway_health events on transitions (up->down or down->up),
not on every check cycle.  Tracks each platform independently.
"""

import logging
from typing import Dict, Optional

from events.bus import EventBus
from events.schema import EventType

logger = logging.getLogger(__name__)


class GatewayHealthMonitor:
    """Tracks platform health and emits events on state transitions."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._last_state: Dict[str, bool] = {}  # platform -> healthy

    def report_health(
        self,
        platform: str,
        healthy: bool,
        detail: Optional[str] = None,
    ) -> Optional[str]:
        """Report a platform's health.  Emits event only on state change.

        Returns event_id if an event was emitted, None otherwise.
        """
        prev = self._last_state.get(platform)
        self._last_state[platform] = healthy

        if prev == healthy:
            return None  # No state change

        status = "up" if healthy else "down"
        logger.info("Gateway health: %s -> %s", platform, status)

        return self.bus.emit(
            event_type=EventType.GATEWAY_HEALTH,
            source="system",
            payload={
                "platform": platform,
                "status": status,
                "detail": detail or "",
            },
        )
