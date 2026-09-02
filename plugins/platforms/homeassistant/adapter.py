"""
Home Assistant platform adapter.

Connects to the HA WebSocket API for real-time event monitoring.
State-change events are converted to MessageEvent objects and forwarded
to the agent for processing.  Outbound messages are delivered as HA
persistent notifications.

Requires:
- aiohttp (already in messaging extras)
- HASS_TOKEN env var (Long-Lived Access Token)
- HASS_URL env var (default: http://homeassistant.local:8123)
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Set

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    aiohttp = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    gateway_trust_env,
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret


def _get_scoped_secret(name, default=None):
    """Scope-aware credential read with the default-profile startup fallback.

    Secondary profiles construct their adapters under a profile secret
    scope -- the scope is authoritative and a scoped miss returns ``default``
    (no cross-profile borrow from ``os.environ``, which may hold another
    profile's value). The DEFAULT profile's adapter constructs and sends
    *unscoped* under multiplexing, where a bare ``get_secret`` would raise
    ``UnscopedSecretError`` and crash this path; there ``os.environ`` is that
    profile's own value, so fall back to it. Same pattern as the Slack
    ``SLACK_APP_TOKEN`` read (#59739) and
    ``gateway/platforms/whatsapp_common.py::_get_wsecret``.
    """
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


logger = logging.getLogger(__name__)


def check_ha_requirements() -> bool:
    """Check if Home Assistant runtime dependencies are available."""
    return AIOHTTP_AVAILABLE


def validate_ha_config(config: PlatformConfig) -> bool:
    """Return True when Home Assistant has enough credential config to connect."""
    token = (getattr(config, "token", None) or _get_scoped_secret("HASS_TOKEN", "")).strip()
    return bool(token)


class HomeAssistantAdapter(BasePlatformAdapter):
    """
    Home Assistant WebSocket adapter.

    Subscribes to ``state_changed`` events and forwards them as
    MessageEvent objects.  Supports domain/entity filtering and
    per-entity cooldowns to avoid event floods.
    """

    MAX_MESSAGE_LENGTH = 4096

    # Reconnection backoff schedule (seconds)
    _BACKOFF_STEPS = [5, 10, 30, 60]

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.HOMEASSISTANT)

        # Connection state
        self._session: Optional["aiohttp.ClientSession"] = None
        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
        self._rest_session: Optional["aiohttp.ClientSession"] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._msg_id: int = 0

        # Configuration from extra
        extra = config.extra or {}
        token = config.token or _get_scoped_secret("HASS_TOKEN", "")
        url = extra.get("url") or os.getenv("HASS_URL", "http://homeassistant.local:8123")
        self._hass_url: str = url.rstrip("/")
        self._hass_token: str = token

        # Event filtering
        self._watch_domains: Set[str] = set()
        self._watch_entities: Set[str] = set()
        self._ignore_entities: Set[str] = set(extra.get("ignore_entities", []))
        self._watch_all: bool = bool(extra.get("watch_all", False))
        self._cooldown_seconds: int = int(extra.get("cooldown_seconds", 30))

        # Deliver target overrides (issue #35060)
        # Per-entry override keyed by entity_id or domain name.
        self._deliver_overrides: Dict[str, str] = {}
        # Default deliver target: "homeassistant" unless overridden top-level.
        self._default_deliver: str = "homeassistant"

        # Parse watch_entities — plain strings and dict-form entries
        for entry in (extra.get("watch_entities") or []):
            if isinstance(entry, str):
                self._watch_entities.add(entry)
            elif isinstance(entry, dict):
                if len(entry) != 1:
                    logger.warning(
                        "[%s] Malformed watch_entities entry (dict with != 1 key): %s, skipping",
                        self.name, entry,
                    )
                    continue
                entity_id, cfg = next(iter(entry.items()))
                if not isinstance(entity_id, str):
                    logger.warning(
                        "[%s] Malformed watch_entities entry (non-string key): %s, skipping",
                        self.name, entry,
                    )
                    continue
                self._watch_entities.add(entity_id)
                if isinstance(cfg, dict) and "deliver" in cfg:
                    dv = cfg["deliver"]
                    if isinstance(dv, str):
                        self._deliver_overrides[entity_id] = dv
                    else:
                        logger.warning(
                            "[%s] Malformed watch_entities entry (deliver not str): %s, skipping deliver target",
                            self.name, entry,
                        )
                elif not isinstance(cfg, dict):
                    logger.warning(
                        "[%s] Malformed watch_entities entry (config not a dict): %s, ignoring deliver target",
                        self.name, entry,
                    )
            else:
                logger.warning(
                    "[%s] Malformed watch_entities entry (not str or dict): %s, skipping",
                    self.name, entry,
                )

        # Parse watch_domains — plain strings and dict-form entries
        for entry in (extra.get("watch_domains") or []):
            if isinstance(entry, str):
                self._watch_domains.add(entry)
            elif isinstance(entry, dict):
                if len(entry) != 1:
                    logger.warning(
                        "[%s] Malformed watch_domains entry (dict with != 1 key): %s, skipping",
                        self.name, entry,
                    )
                    continue
                domain, cfg = next(iter(entry.items()))
                if not isinstance(domain, str):
                    logger.warning(
                        "[%s] Malformed watch_domains entry (non-string key): %s, skipping",
                        self.name, entry,
                    )
                    continue
                self._watch_domains.add(domain)
                if isinstance(cfg, dict) and "deliver" in cfg:
                    dv = cfg["deliver"]
                    if isinstance(dv, str):
                        self._deliver_overrides[domain] = dv
                    else:
                        logger.warning(
                            "[%s] Malformed watch_domains entry (deliver not str): %s, skipping deliver target",
                            self.name, entry,
                        )
                elif not isinstance(cfg, dict):
                    logger.warning(
                        "[%s] Malformed watch_domains entry (config not a dict): %s, ignoring deliver target",
                        self.name, entry,
                    )
            else:
                logger.warning(
                    "[%s] Malformed watch_domains entry (not str or dict): %s, skipping",
                    self.name, entry,
                )

        # Top-level default deliver target
        top_deliver = extra.get("deliver") or extra.get("default_deliver")
        if isinstance(top_deliver, str):
            self._default_deliver = top_deliver

        # Cooldown tracking: entity_id -> last_event_timestamp
        self._last_event_time: Dict[str, float] = {}

    def _next_id(self) -> int:
        """Return the next WebSocket message ID."""
        self._msg_id += 1
        return self._msg_id

    def resolve_deliver_target(self, entity_id: str) -> str:
        """Resolve the deliver target platform for a watched entity.

        Precedence: per-entry override (entity_id, then its domain) in
        ``_deliver_overrides``, else the top-level default (``_default_deliver``,
        itself "homeassistant" when unset). Pure resolution — no routing or
        I/O happens here; the routing layer calls this to pick the platform.
        """
        if entity_id in self._deliver_overrides:
            return self._deliver_overrides[entity_id]
        domain = entity_id.split(".")[0] if "." in entity_id else entity_id
        if domain in self._deliver_overrides:
            return self._deliver_overrides[domain]
        return self._default_deliver

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to HA WebSocket API and subscribe to events."""
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp not installed. Run: pip install aiohttp", self.name)
            return False

        if not self._hass_token:
            logger.warning("[%s] No HASS_TOKEN configured", self.name)
            return False

        try:
            success = await self._ws_connect()
            if not success:
                return False

            # Dedicated REST session for send() calls
            self._rest_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                trust_env=gateway_trust_env(),
            )

            # Warn if no event filters are configured
            if not self._watch_domains and not self._watch_entities and not self._watch_all:
                logger.warning(
                    "[%s] No watch_domains, watch_entities, or watch_all configured. "
                    "All state_changed events will be dropped. Configure filters in "
                    "your HA platform config to receive events.",
                    self.name,
                )

            # Start background listener
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._running = True
            logger.info("[%s] Connected to %s", self.name, self._hass_url)
            # Plugin-registered native handlers (ctx.register_platform_handler).
            self._wire_plugin_handlers(None)
            return True

        except Exception as e:
            logger.error("[%s] Failed to connect: %s", self.name, e)
            return False

    async def _ws_connect(self) -> bool:
        """Establish WebSocket connection and authenticate."""
        ws_url = self._hass_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_url}/api/websocket"

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=gateway_trust_env(),
        )
        self._ws = await self._session.ws_connect(ws_url, heartbeat=30, timeout=30)

        # Step 1: Receive auth_required
        msg = await self._ws.receive_json()
        if msg.get("type") != "auth_required":
            logger.error("Expected auth_required, got: %s", msg.get("type"))
            await self._cleanup_ws()
            return False

        # Step 2: Send auth
        await self._ws.send_json({
            "type": "auth",
            "access_token": self._hass_token,
        })

        # Step 3: Wait for auth_ok
        msg = await self._ws.receive_json()
        if msg.get("type") != "auth_ok":
            logger.error("Auth failed: %s", msg)
            await self._cleanup_ws()
            return False

        # Step 4: Subscribe to state_changed events
        sub_id = self._next_id()
        await self._ws.send_json({
            "id": sub_id,
            "type": "subscribe_events",
            "event_type": "state_changed",
        })

        # Verify subscription acknowledgement
        msg = await self._ws.receive_json()
        if not msg.get("success"):
            logger.error("Failed to subscribe to events: %s", msg)
            await self._cleanup_ws()
            return False

        return True

    async def _cleanup_ws(self) -> None:
        """Close WebSocket and session."""
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def disconnect(self) -> None:
        """Disconnect from Home Assistant."""
        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        await self._cleanup_ws()
        if self._rest_session and not self._rest_session.closed:
            await self._rest_session.close()
        self._rest_session = None
        logger.info("[%s] Disconnected", self.name)

    # ------------------------------------------------------------------
    # Event listener
    # ------------------------------------------------------------------

    async def _listen_loop(self) -> None:
        """Main event loop with automatic reconnection."""
        backoff_idx = 0

        while self._running:
            try:
                await self._read_events()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("[%s] WebSocket error: %s", self.name, e)

            if not self._running:
                return

            # Reconnect with backoff
            delay = self._BACKOFF_STEPS[min(backoff_idx, len(self._BACKOFF_STEPS) - 1)]
            logger.info("[%s] Reconnecting in %ds...", self.name, delay)
            await asyncio.sleep(delay)
            backoff_idx += 1

            try:
                await self._cleanup_ws()
                success = await self._ws_connect()
                if success:
                    backoff_idx = 0  # Reset on successful reconnect
                    logger.info("[%s] Reconnected", self.name)
            except Exception as e:
                logger.warning("[%s] Reconnection failed: %s", self.name, e)

    async def _read_events(self) -> None:
        """Read events from WebSocket until disconnected."""
        if self._ws is None or self._ws.closed:
            return
        async for ws_msg in self._ws:
            if ws_msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(ws_msg.data)
                    if data.get("type") == "event":
                        await self._handle_ha_event(data.get("event", {}))
                except json.JSONDecodeError:
                    logger.debug("Invalid JSON from HA WS: %s", ws_msg.data[:200])
            elif ws_msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                break

    async def _handle_ha_event(self, event: Dict[str, Any]) -> None:
        """Process a state_changed event from Home Assistant."""
        event_data = event.get("data", {})
        entity_id: str = event_data.get("entity_id", "")

        if not entity_id:
            return

        # Apply ignore filter
        if entity_id in self._ignore_entities:
            return

        # Apply domain/entity watch filters (closed by default — require
        # explicit watch_domains, watch_entities, or watch_all to forward)
        domain = entity_id.split(".")[0] if "." in entity_id else ""
        if self._watch_domains or self._watch_entities:
            domain_match = domain in self._watch_domains if self._watch_domains else False
            entity_match = entity_id in self._watch_entities if self._watch_entities else False
            if not domain_match and not entity_match:
                return
        elif not self._watch_all:
            # No filters configured and watch_all is off — drop the event
            return

        # Apply cooldown
        now = time.time()
        last = self._last_event_time.get(entity_id, 0)
        if (now - last) < self._cooldown_seconds:
            return
        self._last_event_time[entity_id] = now

        # Build human-readable message
        old_state = event_data.get("old_state", {})
        new_state = event_data.get("new_state", {})
        message = self._format_state_change(entity_id, old_state, new_state)

        if not message:
            return

        # Resolve cross-platform deliver target (issue #35060)
        target = self.resolve_deliver_target(entity_id)
        if target != "homeassistant":
            _chat_id = f"ha_events:{target}"
        else:
            _chat_id = "ha_events"

        # Build MessageEvent and forward to handler
        source = self.build_source(
            chat_id=_chat_id,
            chat_name="Home Assistant Events",
            chat_type="channel",
            user_id="homeassistant",
            user_name="Home Assistant",
        )

        msg_event = MessageEvent(
            text=message,
            message_type=MessageType.TEXT,
            source=source,
            message_id=f"ha_{entity_id}_{int(now)}",
            timestamp=datetime.now(),
        )

        await self.handle_message(msg_event)

    @staticmethod
    def _format_state_change(
        entity_id: str,
        old_state: Dict[str, Any],
        new_state: Dict[str, Any],
    ) -> Optional[str]:
        """Convert a state_changed event into a human-readable description."""
        if not new_state:
            return None

        old_val = old_state.get("state", "unknown") if old_state else "unknown"
        new_val = new_state.get("state", "unknown")

        # Skip if state didn't actually change
        if old_val == new_val:
            return None

        friendly_name = new_state.get("attributes", {}).get("friendly_name", entity_id)
        domain = entity_id.split(".")[0] if "." in entity_id else ""

        # Domain-specific formatting
        if domain == "climate":
            attrs = new_state.get("attributes", {})
            temp = attrs.get("current_temperature", "?")
            target = attrs.get("temperature", "?")
            return (
                f"[Home Assistant] {friendly_name}: HVAC mode changed from "
                f"'{old_val}' to '{new_val}' (current: {temp}, target: {target})"
            )

        if domain == "sensor":
            unit = new_state.get("attributes", {}).get("unit_of_measurement", "")
            return (
                f"[Home Assistant] {friendly_name}: changed from "
                f"{old_val}{unit} to {new_val}{unit}"
            )

        if domain == "binary_sensor":
            return (
                f"[Home Assistant] {friendly_name}: "
                f"{'triggered' if new_val == 'on' else 'cleared'} "
                f"(was {'triggered' if old_val == 'on' else 'cleared'})"
            )

        if domain in {"light", "switch", "fan"}:
            return (
                f"[Home Assistant] {friendly_name}: turned "
                f"{'on' if new_val == 'on' else 'off'}"
            )

        if domain == "alarm_control_panel":
            return (
                f"[Home Assistant] {friendly_name}: alarm state changed from "
                f"'{old_val}' to '{new_val}'"
            )

        # Generic fallback
        return (
            f"[Home Assistant] {friendly_name} ({entity_id}): "
            f"changed from '{old_val}' to '{new_val}'"
        )

    # ------------------------------------------------------------------
    # Outbound messaging
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a notification via HA REST API (persistent_notification.create),
        or route cross-platform when chat_id carries the ``ha_events:<target>`` tag.

        Uses the REST API instead of WebSocket to avoid a race condition
        with the event listener loop that reads from the same WS connection.
        """
        # Cross-platform routing for tagged chat_ids (issue #35060)
        if chat_id and chat_id.startswith("ha_events:"):
            platform_name = chat_id.split(":", 1)[1]
            if not platform_name:
                return await self._send_ha_notification(content)
            if not self.gateway_runner:
                logger.warning(
                    "[%s] No gateway runner for cross-platform delivery to '%s'; "
                    "falling back to HA notification",
                    self.name, platform_name,
                )
                return await self._send_ha_notification(content)
            try:
                # Accept user-capitalized names ("WhatsApp", "Telegram") —
                # Platform enum values are lowercase.
                target_platform = Platform(platform_name.strip().lower())
            except ValueError:
                logger.warning(
                    "[%s] Unknown deliver platform '%s'; "
                    "falling back to HA notification",
                    self.name, platform_name,
                )
                return await self._send_ha_notification(content)

            # Resolve target adapter (primary + profile fallback, mirroring webhook)
            adapter = self.gateway_runner.adapters.get(target_platform)
            if not adapter:
                for _prof, amap in (
                    getattr(self.gateway_runner, "_profile_adapters", None) or {}
                ).items():
                    if not isinstance(amap, dict):
                        continue
                    cand = amap.get(target_platform)
                    if cand is not None:
                        adapter = cand
                        break

            if not adapter:
                logger.warning(
                    "[%s] Adapter '%s' not connected; "
                    "falling back to HA notification",
                    self.name, platform_name,
                )
                return await self._send_ha_notification(content)

            # Resolve home channel for the target platform
            home = self.gateway_runner.config.get_home_channel(target_platform)
            if not home or not getattr(home, "chat_id", None):
                logger.warning(
                    "[%s] No home channel for platform '%s'; "
                    "falling back to HA notification",
                    self.name, platform_name,
                )
                return await self._send_ha_notification(content)

            # Fail-safe: a raise from the target adapter must never escape
            # send() — fall back to the HA notification instead of dropping
            # the alert. (asyncio.CancelledError is BaseException in 3.8+,
            # so cancellation still propagates.)
            try:
                return await adapter.send(home.chat_id, content, metadata=metadata)
            except Exception as e:
                logger.warning(
                    "[%s] Cross-platform delivery to '%s' failed (%s); "
                    "falling back to HA notification",
                    self.name, platform_name, e,
                )
                return await self._send_ha_notification(content)

        # Local HA notification delivery (or fallback after routing failure)
        return await self._send_ha_notification(content)

    async def _send_ha_notification(self, content: str) -> SendResult:
        """Send a notification via HA REST API (persistent_notification.create).

        Used directly for local delivery and as the fallback for cross-platform
        routing.  The REST API is used instead of WebSocket to avoid a race
        condition with the event listener loop that reads from the same WS
        connection.
        """
        url = f"{self._hass_url}/api/services/persistent_notification/create"
        headers = {
            "Authorization": f"Bearer {self._hass_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "title": "Hermes Agent",
            "message": content[:self.MAX_MESSAGE_LENGTH],
        }

        try:
            if self._rest_session:
                async with self._rest_session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status < 300:
                        return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
                    else:
                        body = await resp.text()
                        return SendResult(success=False, error=f"HTTP {resp.status}: {body}")
            else:
                async with aiohttp.ClientSession(trust_env=gateway_trust_env()) as session:
                    async with session.post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status < 300:
                            return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
                        else:
                            body = await resp.text()
                            return SendResult(success=False, error=f"HTTP {resp.status}: {body}")

        except asyncio.TimeoutError:
            return SendResult(success=False, error="Timeout sending notification to HA")
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """No typing indicator for Home Assistant."""

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about the HA event channel."""
        return {
            "name": "Home Assistant Events",
            "type": "channel",
            "url": self._hass_url,
        }


# ---------------------------------------------------------------------------
# Standalone (out-of-process) sender — used by cron deliver=homeassistant
# ---------------------------------------------------------------------------


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Send a notification via the HA ``notify.notify`` service without a
    live gateway adapter.

    Used by ``tools/send_message_tool._send_via_adapter`` when the gateway
    runner is not in this process (typical for cron jobs running
    out-of-process).  The HTTP path is the same one the legacy
    ``_send_homeassistant`` helper used in ``tools/send_message_tool.py``
    before this migration.

    Reads ``HASS_TOKEN`` from ``pconfig.token`` (set by the gateway config
    loader from env) and falls back to the ``HASS_TOKEN`` env var.  Server
    URL comes from ``pconfig.extra["url"]`` (seeded by the env loader in
    ``gateway/config.py``) or the ``HASS_URL`` env var.

    ``thread_id``, ``media_files`` and ``force_document`` are accepted for
    signature parity with other standalone senders.  HA notifications have
    no native threading or attachment model — these arguments are ignored.
    """
    if not AIOHTTP_AVAILABLE:
        return {"error": "aiohttp not installed. Run: pip install aiohttp"}

    extra = getattr(pconfig, "extra", {}) or {}
    hass_url = (extra.get("url") or os.getenv("HASS_URL", "")).rstrip("/")
    token = (getattr(pconfig, "token", None) or _get_scoped_secret("HASS_TOKEN", "")).strip()
    if not hass_url or not token:
        return {
            "error": (
                "Home Assistant standalone send: HASS_URL and HASS_TOKEN "
                "must both be set"
            )
        }

    url = f"{hass_url}/api/services/notify/notify"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"message": message, "target": chat_id}

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=gateway_trust_env(),
        ) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status not in {200, 201}:
                    body = await resp.text()
                    return {
                        "error": (
                            f"Home Assistant API error ({resp.status}): {body}"
                        )
                    }
        return {
            "success": True,
            "platform": "homeassistant",
            "chat_id": chat_id,
        }
    except asyncio.TimeoutError:
        return {"error": "Timeout sending notification to Home Assistant"}
    except Exception as e:
        return {"error": f"Home Assistant send failed: {e}"}


# ---------------------------------------------------------------------------
# is_connected probe
# ---------------------------------------------------------------------------


def _is_connected(config) -> bool:
    """Home Assistant is considered connected when ``HASS_TOKEN`` is set.

    Looks up via ``hermes_cli.gateway.get_env_value`` at call time (not via
    the plugin's own bound import) so tests that patch
    ``gateway_mod.get_env_value`` can suppress ambient ``HASS_TOKEN`` env
    vars.  Matches what the legacy connected-platforms check did before
    this migration.
    """
    import hermes_cli.gateway as gateway_mod
    return bool((gateway_mod.get_env_value("HASS_TOKEN") or "").strip())


# ---------------------------------------------------------------------------
# Plugin registration entry point
# ---------------------------------------------------------------------------


def _build_adapter(config):
    """Factory wrapper that constructs HomeAssistantAdapter from a PlatformConfig."""
    return HomeAssistantAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="homeassistant",
        label="Home Assistant",
        adapter_factory=_build_adapter,
        check_fn=check_ha_requirements,
        validate_config=validate_ha_config,
        is_connected=_is_connected,
        required_env=["HASS_TOKEN"],
        install_hint="pip install aiohttp",
        # Out-of-process cron delivery via the HA ``notify.notify`` service.
        # Without this hook, ``deliver=homeassistant`` cron jobs would fail
        # with "No live adapter" when cron runs separately from the gateway.
        # Mirrors the Discord / Teams / Mattermost pattern.
        standalone_sender_fn=_standalone_send,
        # HA notification message cap — matches MAX_MESSAGE_LENGTH on the
        # adapter class above.
        max_message_length=HomeAssistantAdapter.MAX_MESSAGE_LENGTH,
        # Display
        emoji="🏠",
        allow_update_command=True,
    )
