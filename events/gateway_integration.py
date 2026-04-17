"""Gateway integration — wires EventBus, producers, and subscribers into the gateway lifecycle.

Called from gateway/run.py during startup and shutdown.  All components
run within the gateway process — no new daemons or threads.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

from events.bus import EventBus
from events.producers.health_monitor import GatewayHealthMonitor
from events.producers.mailbox_watcher import MailboxWatcher
from events.subscribers.base import SubscriberRegistry
from events.subscribers.audit_logger import AuditLogger
from events.subscribers.telegram_notifier import TelegramNotifier
from events.subscribers.whatsapp_escalator import WhatsAppEscalator
from events.subscribers.digest_composer import DigestComposer, DIGEST_SCHEDULE_HOURS
from events.subscribers.memory_writer import MemoryWriter
from events.subscribers.telegram_mirror import TelegramMirror
from events.subscribers.mailbox_translator import MailboxTranslator

logger = logging.getLogger(__name__)

# Subscriber lag alert threshold — alert when any subscriber falls behind
# by this many events.  Should be larger than normal burst sizes but small
# enough that "silent failure" becomes visible quickly.
LAG_ALERT_THRESHOLD = 100
# Cooldown between successive agent_error emissions for the same subscriber.
LAG_ALERT_COOLDOWN_SECONDS = 900  # 15 minutes

_bus: Optional[EventBus] = None
_registry: Optional[SubscriberRegistry] = None
_health_monitor: Optional[GatewayHealthMonitor] = None
_mailbox_watcher: Optional[MailboxWatcher] = None
_subscriber_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def startup(adapters: Optional[Dict] = None) -> None:
    """Initialize EventBus, register all subscribers, start polling thread."""
    global _bus, _registry, _health_monitor, _mailbox_watcher, _subscriber_thread

    if _bus is not None:
        shutdown()

    logger.info("EventBus: initializing communication layer...")

    _bus = EventBus()
    _registry = SubscriberRegistry()
    _health_monitor = GatewayHealthMonitor(_bus)
    _mailbox_watcher = MailboxWatcher(_bus)

    # Register subscribers
    _registry.register(AuditLogger(_bus))
    _registry.register(TelegramNotifier(_bus))
    _registry.register(WhatsAppEscalator(_bus))
    _registry.register(DigestComposer(_bus))
    _registry.register(MemoryWriter(_bus))
    _registry.register(TelegramMirror(_bus))
    _registry.register(MailboxTranslator(_bus))

    _registry.startup_all()

    # Start subscriber polling thread
    _stop_event.clear()
    _subscriber_thread = threading.Thread(
        target=_subscriber_poll_loop,
        daemon=True,
        name="event-subscribers",
    )
    _subscriber_thread.start()

    logger.info("EventBus: %d subscribers registered, polling started",
                len(_registry.subscribers))


def shutdown() -> None:
    """Stop polling and clean up."""
    global _subscriber_thread, _bus
    _stop_event.set()
    if _subscriber_thread:
        _subscriber_thread.join(timeout=5)
        _subscriber_thread = None
    if _registry:
        _registry.shutdown_all()
    if _bus:
        _bus.close()
        _bus = None
    logger.info("EventBus: shutdown complete")


def get_bus() -> Optional[EventBus]:
    """Get the global EventBus instance (for use by CronEventEmitter)."""
    return _bus


def get_health_monitor() -> Optional[GatewayHealthMonitor]:
    """Get the health monitor (for gateway adapter health checks)."""
    return _health_monitor


def _check_subscriber_lag(
    registry: SubscriberRegistry,
    bus: EventBus,
    threshold: int,
    cooldown_seconds: int,
    last_alerted: Dict[str, float],
    now: float,
) -> Dict[str, int]:
    """Emit an agent_error event for each subscriber whose lag > threshold.

    ``last_alerted`` is mutated in place to record the monotonic time of each
    emission so we honour the cooldown on subsequent calls.  Returns the
    current lag report (useful for tests and callers who want to log it).
    """
    report = registry.lag_report()
    for subscriber_id, lag in report.items():
        if lag <= threshold:
            continue
        prev = last_alerted.get(subscriber_id, 0.0)
        if now - prev < cooldown_seconds:
            continue
        from events.schema import EventType, Priority
        try:
            bus.emit(
                event_type=EventType.AGENT_ERROR,
                source="event-bus",
                payload={
                    "subscriber_id": subscriber_id,
                    "lag": lag,
                    "threshold": threshold,
                    "error": f"Subscriber '{subscriber_id}' is {lag} events behind head",
                },
                priority=Priority.HIGH,
            )
            last_alerted[subscriber_id] = now
            logger.warning(
                "EventBus lag alert: %s is %d events behind (>threshold=%d)",
                subscriber_id, lag, threshold,
            )
        except Exception:
            logger.exception("Failed to emit lag alert for %s", subscriber_id)
    return report


def _get_et_hour() -> int:
    """Get current hour in Eastern Time."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("America/New_York")
        return datetime.now(tz).hour
    except Exception:
        return datetime.utcnow().hour  # fallback


def _subscriber_poll_loop() -> None:
    """Background thread that polls all subscribers at their configured intervals."""
    last_poll_times: Dict[str, float] = {}
    last_mailbox_scan: float = 0
    last_health_check: float = 0
    last_lag_check: float = 0
    last_cleanup: float = 0
    last_digest_hour: int = -1
    last_flush_fired: bool = False
    lag_alerts_sent: Dict[str, float] = {}  # subscriber_id -> monotonic timestamp

    while not _stop_event.is_set():
        now = time.monotonic()

        # Poll each subscriber at its own interval
        if _registry:
            for sub in _registry.subscribers:
                last = last_poll_times.get(sub.subscriber_id, 0)
                if now - last >= sub.poll_interval_seconds:
                    try:
                        sub.poll()
                    except Exception:
                        logger.exception("Subscriber poll failed: %s", sub.subscriber_id)
                    last_poll_times[sub.subscriber_id] = now

        # Timed triggers for DigestComposer and WhatsAppEscalator
        if _registry:
            et_hour = _get_et_hour()

            # Digest: fire at 8am, 1pm, 6pm ET (once per hour)
            if et_hour in DIGEST_SCHEDULE_HOURS and et_hour != last_digest_hour:
                for sub in _registry.subscribers:
                    if isinstance(sub, DigestComposer):
                        try:
                            digest = sub.compose()
                            logger.info("Digest composed at hour %d ET (%d chars)",
                                        et_hour, len(digest))
                        except Exception:
                            logger.exception("Digest compose failed")
                last_digest_hour = et_hour
            elif et_hour not in DIGEST_SCHEDULE_HOURS:
                last_digest_hour = -1

            # WhatsApp morning flush: deliver queued messages when quiet hours end (7am ET)
            if et_hour == 7 and not last_flush_fired:
                for sub in _registry.subscribers:
                    if isinstance(sub, WhatsAppEscalator):
                        try:
                            count = sub.flush_queue()
                            if count:
                                logger.info("WhatsApp morning flush: %d messages", count)
                        except Exception:
                            logger.exception("WhatsApp flush failed")
                last_flush_fired = True
            elif et_hour != 7:
                last_flush_fired = False

        # Subscriber lag check every 5 minutes
        if _registry and _bus and now - last_lag_check >= 300:
            try:
                _check_subscriber_lag(
                    _registry, _bus,
                    LAG_ALERT_THRESHOLD, LAG_ALERT_COOLDOWN_SECONDS,
                    lag_alerts_sent, now,
                )
            except Exception:
                logger.exception("Lag check failed")
            last_lag_check = now

        # Active health checks every 60 seconds
        if _health_monitor and now - last_health_check >= 60:
            try:
                _health_monitor.check()
            except Exception:
                logger.exception("Health check failed")
            last_health_check = now

        # Scan mailbox every 60 seconds
        if _mailbox_watcher and now - last_mailbox_scan >= 60:
            try:
                _mailbox_watcher.scan()
            except Exception:
                logger.exception("Mailbox scan failed")
            last_mailbox_scan = now

        # Daily cleanup (every 24 hours)
        if _bus and now - last_cleanup >= 86400:
            try:
                _bus.cleanup(retention_days=30)
            except Exception:
                logger.exception("Event cleanup failed")
            last_cleanup = now

        _stop_event.wait(timeout=1)  # tick every 1 second
