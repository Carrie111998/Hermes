"""GatewayHealthMonitor — emits events on platform health state changes.

Only emits gateway_health events on transitions (up->down or down->up),
not on every check cycle.  Tracks each platform independently.

Active mode: check() pings WhatsApp bridge health endpoint and tests
Telegram bot connectivity (cached 5 min).
"""

import logging
import time
from typing import Dict, Optional

from events.bus import EventBus
from events.schema import EventType

logger = logging.getLogger(__name__)


class GatewayHealthMonitor:
    """Tracks platform health and emits events on state transitions.

    Call check() every 60 seconds from the subscriber poll loop
    to actively probe platform health.
    """

    TELEGRAM_CACHE_TTL = 300  # 5-minute cache for Telegram connectivity
    # Consecutive failed probes required before whatsapp is reported down.
    # The bridge's node event loop can stall past the probe timeout for a
    # single 60s cycle during a Baileys resync; one blip is not an outage.
    WHATSAPP_DOWN_THRESHOLD = 2

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._last_state: Dict[str, bool] = {}  # platform -> healthy
        self._telegram_cache: Optional[bool] = None
        self._telegram_cache_ts: float = 0
        self._whatsapp_fail_streak: int = 0

    def check(self) -> None:
        """Actively check all platform health endpoints.

        Pings WhatsApp bridge HTTP health endpoint.
        Checks Telegram bot connectivity (cached for 5 minutes).
        """
        self._check_whatsapp()
        self._check_telegram()

    def _check_whatsapp(self) -> None:
        """Ping the WhatsApp bridge health endpoint."""
        try:
            import requests
            from hermes_constants import get_hermes_home
            import json as _json

            # Read bridge port from config
            config_path = get_hermes_home() / "config.yaml"
            port = 3000  # default
            if config_path.exists():
                import yaml
                # Read via Path.read_text (io.open), not builtins.open: this
                # runs on a background thread that can outlive a test patching
                # builtins.open, and yaml on a mock file object never reaches
                # EOF — the reader spins forever.
                cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
                port = cfg.get("whatsapp", {}).get("bridge_port", 3000)

            resp = requests.get(f"http://127.0.0.1:{port}/health", timeout=5)
            healthy = resp.status_code == 200
            detail = f"HTTP {resp.status_code}" if not healthy else ""
        except Exception as e:
            healthy = False
            detail = str(e)[:200]

        if healthy:
            self._whatsapp_fail_streak = 0
        else:
            self._whatsapp_fail_streak += 1
            if self._whatsapp_fail_streak < self.WHATSAPP_DOWN_THRESHOLD:
                return  # single blip — wait for the next probe before alerting

        self.report_health("whatsapp", healthy, detail)

    def _check_telegram(self) -> None:
        """Check Telegram bot connectivity (cached for 5 minutes)."""
        now = time.monotonic()
        if self._telegram_cache is not None and now - self._telegram_cache_ts < self.TELEGRAM_CACHE_TTL:
            return  # Use cached result, no state change possible

        try:
            import requests
            import os
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            if not bot_token:
                return  # Telegram not configured, skip

            resp = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getMe",
                timeout=10,
            )
            healthy = resp.status_code == 200
            detail = "" if healthy else f"HTTP {resp.status_code}"
        except Exception as e:
            healthy = False
            detail = str(e)[:200]

        self._telegram_cache = healthy
        self._telegram_cache_ts = now
        self.report_health("telegram", healthy, detail)

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
