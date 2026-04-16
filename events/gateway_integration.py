"""Gateway integration — wires EventBus, producers, and subscribers into the gateway lifecycle.

Called from gateway/run.py during startup and shutdown.  All components
run within the gateway process — no new daemons or threads.
"""

import logging
import threading
import time
from typing import Any, Dict, Optional

from events.bus import EventBus
from events.producers.health_monitor import GatewayHealthMonitor
from events.producers.mailbox_watcher import MailboxWatcher
from events.subscribers.base import SubscriberRegistry
from events.subscribers.audit_logger import AuditLogger
from events.subscribers.telegram_notifier import TelegramNotifier
from events.subscribers.whatsapp_escalator import WhatsAppEscalator
from events.subscribers.digest_composer import DigestComposer
from events.subscribers.memory_writer import MemoryWriter
from events.subscribers.telegram_mirror import TelegramMirror

logger = logging.getLogger(__name__)

_bus: Optional[EventBus] = None
_registry: Optional[SubscriberRegistry] = None
_health_monitor: Optional[GatewayHealthMonitor] = None
_mailbox_watcher: Optional[MailboxWatcher] = None
_subscriber_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def startup(adapters: Optional[Dict] = None) -> None:
    """Initialize EventBus, register all subscribers, start polling thread."""
    global _bus, _registry, _health_monitor, _mailbox_watcher, _subscriber_thread

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
    global _subscriber_thread
    _stop_event.set()
    if _subscriber_thread:
        _subscriber_thread.join(timeout=5)
        _subscriber_thread = None
    if _registry:
        _registry.shutdown_all()
    logger.info("EventBus: shutdown complete")


def get_bus() -> Optional[EventBus]:
    """Get the global EventBus instance (for use by CronEventEmitter)."""
    return _bus


def get_health_monitor() -> Optional[GatewayHealthMonitor]:
    """Get the health monitor (for gateway adapter health checks)."""
    return _health_monitor


def _subscriber_poll_loop() -> None:
    """Background thread that polls all subscribers at their configured intervals."""
    last_poll_times: Dict[str, float] = {}
    last_mailbox_scan: float = 0
    last_cleanup: float = 0

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
