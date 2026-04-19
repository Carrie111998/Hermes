"""Gateway integration — wires EventBus, producers, and subscribers into the gateway lifecycle.

Called from gateway/run.py during startup and shutdown.  All components
run within the gateway process — no new daemons or threads.
"""

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from events.bus import EventBus
from events.paths import digest_state_path, gateway_heartbeat_path, whatsapp_flush_state_path
from events.producers.health_monitor import GatewayHealthMonitor
from events.producers.mailbox_watcher import MailboxWatcher
from events.state import load_state, save_state
from events.subscribers.base import SubscriberRegistry
from events.subscribers.audit_logger import AuditLogger
from events.subscribers.telegram_notifier import TelegramNotifier
from events.subscribers.whatsapp_escalator import WhatsAppEscalator
from events.subscribers.digest_composer import DigestComposer, DIGEST_SCHEDULE_HOURS
from events.subscribers.memory_writer import MemoryWriter
from events.subscribers.telegram_mirror import TelegramMirror
from events.subscribers.mailbox_translator import MailboxTranslator
from events.subscribers.cron_stale_monitor import CronStaleMonitor

logger = logging.getLogger(__name__)

# Subscriber lag alert threshold — alert when any subscriber falls behind
# by this many events.  Should be larger than normal burst sizes but small
# enough that "silent failure" becomes visible quickly.
LAG_ALERT_THRESHOLD = 100
# Cooldown between successive agent_error emissions for the same subscriber.
LAG_ALERT_COOLDOWN_SECONDS = 900  # 15 minutes
# Cooldown for outer poll-loop exception alerts — prevents alert storms if
# the loop catches on every tick.  Separate from lag cooldown so we can tune.
POLL_LOOP_ERROR_COOLDOWN_SECONDS = 900
# Heartbeat write interval — external watchers stat gateway_heartbeat_path()
# and alert on staleness > a few minutes, so this cadence must be tight
# enough that a single missed write stays under the alert threshold.
HEARTBEAT_INTERVAL_SECONDS = 60

_bus: Optional[EventBus] = None
_registry: Optional[SubscriberRegistry] = None
_health_monitor: Optional[GatewayHealthMonitor] = None
_mailbox_watcher: Optional[MailboxWatcher] = None
_subscriber_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_startup_monotonic: float = 0.0


def startup(adapters: Optional[Dict] = None) -> None:
    """Initialize EventBus, register all subscribers, start polling thread."""
    global _bus, _registry, _health_monitor, _mailbox_watcher, _subscriber_thread, _startup_monotonic

    if _bus is not None:
        shutdown()

    logger.info("EventBus: initializing communication layer...")

    _startup_monotonic = time.monotonic()
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
    _registry.register(CronStaleMonitor(_bus))

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


def _write_heartbeat(consecutive_outer_errors: int) -> None:
    """Atomically write a liveness signal file for external watchers.

    Payload keys:
      - ts: current UTC time (ISO8601)
      - pid: gateway process PID
      - subscriber_count: number of registered subscribers
      - uptime_seconds: monotonic time since startup()
      - consecutive_outer_errors: current value of the poll-loop error counter

    External watchers (mission-control frontend, cron probes) stat this
    file's mtime and alert when it exceeds a staleness threshold; reading
    the JSON gives them richer diagnostic context for why the loop is
    degraded even if still writing.
    """
    path = gateway_heartbeat_path()
    subscriber_count = len(_registry.subscribers) if _registry is not None else 0
    uptime = time.monotonic() - _startup_monotonic if _startup_monotonic else 0.0
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "subscriber_count": subscriber_count,
        "uptime_seconds": round(uptime, 3),
        "consecutive_outer_errors": consecutive_outer_errors,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace via tempfile in the same directory.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".gateway-heartbeat-",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup of the tempfile; os.replace consumed it on success.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _emit_poll_loop_error(consecutive_errors: int) -> None:
    """Emit an AGENT_ERROR event so a crashing poll loop becomes visible.

    Swallows all exceptions — if even this fails, we log and give up.  Called
    only after the cooldown check passes.
    """
    if _bus is None:
        return
    try:
        from events.schema import EventType, Priority
        _bus.emit(
            event_type=EventType.AGENT_ERROR,
            source="event-bus",
            payload={
                "subscriber_id": "poll-loop",
                "error": "subscriber_poll_loop caught an unexpected exception",
                "consecutive_errors": consecutive_errors,
            },
            priority=Priority.HIGH,
        )
    except Exception:
        logger.exception("Failed to emit poll-loop error event")


def _subscriber_poll_loop() -> None:
    """Background thread that polls all subscribers at their configured intervals.

    Outer try/except is a safety net: every sub-operation below already has
    its own try/except, but a handful of state saves and iterations are not
    individually guarded, and an uncaught exception would kill this thread
    and produce exactly the silent-notification failure mode we fixed in
    the 2026-04-16 Post-Silence-Fix Addendum.  If the outer catch fires,
    we log, emit an agent_error event (rate-limited), and continue ticking.
    """
    last_poll_times: Dict[str, float] = {}
    last_mailbox_scan: float = 0
    last_health_check: float = 0
    last_lag_check: float = 0
    last_cleanup: float = 0
    last_checkpoint: float = 0
    last_heartbeat: float = 0
    _state = load_state(digest_state_path(), default={})
    last_digest_hour: int = _state.get("last_digest_hour", -1)
    _flush_state = load_state(whatsapp_flush_state_path(), default={})
    last_flush_date: str = _flush_state.get("last_flush_date", "")
    lag_alerts_sent: Dict[str, float] = {}  # subscriber_id -> monotonic timestamp
    consecutive_outer_errors: int = 0
    last_outer_error_emit: float = 0.0

    while not _stop_event.is_set():
        try:
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
                    _state["last_digest_hour"] = et_hour
                    try:
                        save_state(digest_state_path(), _state)
                    except Exception:
                        logger.exception("Failed to save digest state")
                elif et_hour not in DIGEST_SCHEDULE_HOURS:
                    if last_digest_hour != -1:
                        last_digest_hour = -1
                        _state["last_digest_hour"] = -1
                        try:
                            save_state(digest_state_path(), _state)
                        except Exception:
                            logger.exception("Failed to save digest state")

                # WhatsApp morning flush — one-per-day by ET date
                import zoneinfo
                from datetime import datetime as _dt
                try:
                    tz = zoneinfo.ZoneInfo("America/New_York")
                    today_et = _dt.now(tz).date().isoformat()
                except Exception:
                    today_et = _dt.utcnow().date().isoformat()

                if et_hour == 7 and last_flush_date != today_et:
                    for sub in _registry.subscribers:
                        if isinstance(sub, WhatsAppEscalator):
                            try:
                                count = sub.flush_queue()
                                if count:
                                    logger.info("WhatsApp morning flush: %d messages", count)
                            except Exception:
                                logger.exception("WhatsApp flush failed")
                    last_flush_date = today_et
                    try:
                        save_state(whatsapp_flush_state_path(), {"last_flush_date": today_et})
                    except Exception:
                        logger.exception("Failed to save whatsapp flush state")

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

            # WAL checkpoint every 60 seconds
            if _bus and now - last_checkpoint >= 60:
                try:
                    _bus.checkpoint()
                except Exception:
                    logger.exception("WAL checkpoint failed")
                last_checkpoint = now

            # Liveness heartbeat file — external watchers stat mtime and alert
            # on staleness, so we must always attempt to write even when other
            # blocks have partially failed.  _write_heartbeat itself is wrapped
            # because a stuck filesystem must not kill the loop.
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                try:
                    _write_heartbeat(consecutive_outer_errors)
                except Exception:
                    logger.exception("Heartbeat write failed")
                last_heartbeat = now

            # Reset consecutive counter after a fully successful tick
            consecutive_outer_errors = 0
        except Exception:
            consecutive_outer_errors += 1
            logger.exception(
                "Subscriber poll loop body raised unexpectedly (consecutive=%d)",
                consecutive_outer_errors,
            )
            if time.monotonic() - last_outer_error_emit >= POLL_LOOP_ERROR_COOLDOWN_SECONDS:
                _emit_poll_loop_error(consecutive_outer_errors)
                last_outer_error_emit = time.monotonic()

        _stop_event.wait(timeout=1)  # tick every 1 second
