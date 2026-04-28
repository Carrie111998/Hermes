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
from typing import Any, Dict, List, Optional, Set

from events.bus import EventBus
from events.paths import (
    cron_stale_thresholds_path,
    digest_state_path,
    gateway_heartbeat_path,
    whatsapp_flush_state_path,
)
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
from events.subscribers.tracker_intent_applier import TrackerIntentApplierSubscriber
from events.subscribers.critic_trigger import CriticSubscriber
from events.subscribers.scribe_realtime import ScribeRealtime

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
    # TelegramMirror retired 2026-04-28: it was a v1-era shadow-copy of
    # mailbox_message events to the Agent Comms topic. The v2 cutover
    # (20260424T233627Z) collapsed Agent Comms and Digests into a single
    # scribe_daily topic, which TelegramNotifier already routes mailbox_message
    # events to via TOPIC_ROUTING + the NOTIFICATION special case. Keeping
    # TelegramMirror registered duplicated every mailbox_message delivery.
    # _registry.register(TelegramMirror(_bus))
    _registry.register(MailboxTranslator(_bus))
    _registry.register(TrackerIntentApplierSubscriber(_bus))
    _registry.register(CriticSubscriber(_bus))
    _registry.register(ScribeRealtime(_bus))

    # CronStaleMonitor: load optional per-job threshold overrides.  Missing
    # file = built-in defaults.  Malformed file = log + fall back to defaults
    # (never crash the gateway over a config typo).
    _stale_default: Optional[int] = None
    _stale_overrides: Dict[str, int] = {}
    try:
        _stale_cfg_path = cron_stale_thresholds_path()
        if _stale_cfg_path.exists():
            with open(_stale_cfg_path, "r", encoding="utf-8") as f:
                _stale_cfg = json.load(f)
            if isinstance(_stale_cfg.get("default_seconds"), int):
                _stale_default = _stale_cfg["default_seconds"]
            if isinstance(_stale_cfg.get("per_job"), dict):
                _stale_overrides = {
                    str(k): int(v) for k, v in _stale_cfg["per_job"].items()
                    if isinstance(v, int) or (isinstance(v, str) and v.isdigit())
                }
    except Exception:
        logger.exception("Failed to load cron_stale_thresholds.json — using defaults")
    _registry.register(CronStaleMonitor(
        _bus,
        default_threshold_seconds=_stale_default,
        per_job_thresholds=_stale_overrides,
    ))

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


def _pick_digest_target(
    et_hour: int,
    today_et: str,
    fired_keys: Set[str],
    schedule_hours=None,
) -> Optional[str]:
    """Return the next digest key to fire, or None.

    **First-and-latest semantics (2026-04-19 fix):** return the *first*
    missed scheduled hour of today if it hasn't fired yet, otherwise the
    *latest* missed scheduled hour.  Middle hours are skipped — the
    latest digest's pipeline snapshot already covers current state, and
    the first digest preserves the unique overnight-summary content.

    One key per call — the poll loop's sub-second cadence naturally
    sequences first-then-latest across successive ticks.  Firing both in
    the same tick would produce an empty second digest because
    ``DigestComposer.compose()`` advances ``self._last_digest_at`` after
    every call and uses it as the window's lower bound.

    Regression shield against the 2026-04-19 morning-digest loss: the
    previous "latest-only" rule silently skipped the 7:00 ET overnight
    summary when the gateway happened to be offline during that hour.
    """
    hours = sorted(schedule_hours if schedule_hours is not None else DIGEST_SCHEDULE_HOURS)
    applicable = [h for h in hours if h <= et_hour]
    if not applicable:
        return None

    first_hour = applicable[0]
    latest_hour = applicable[-1]

    first_key = f"{today_et}-{first_hour:02d}"
    latest_key = f"{today_et}-{latest_hour:02d}"

    if first_key not in fired_keys:
        return first_key
    if latest_key != first_key and latest_key not in fired_keys:
        return latest_key
    return None


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
    last_batch_flush: float = 0
    _state = load_state(digest_state_path(), default={})
    # ``fired_digest_keys`` is a list of YYYY-MM-DD-HH keys fired today.
    # Migrated from the legacy scalar ``last_digest_key`` on first post-
    # upgrade start: the legacy value is seeded into the set so we don't
    # re-fire the most recent digest.  Worst case if legacy_key was from a
    # prior day: one duplicate digest, auto-pruned on the next tick when
    # we filter to today's date prefix.
    _legacy_digest_key = _state.get("last_digest_key", "")
    fired_digest_keys: List[str] = list(_state.get("fired_digest_keys", []))
    if _legacy_digest_key and _legacy_digest_key not in fired_digest_keys:
        fired_digest_keys.append(_legacy_digest_key)
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

            # Timed triggers for DigestComposer and WhatsAppEscalator.
            # Uses catch-up semantics (>= rather than ==) so a gateway that
            # was offline during a scheduled hour still fires on startup.
            if _registry:
                import zoneinfo
                from datetime import datetime as _dt
                try:
                    tz = zoneinfo.ZoneInfo("America/New_York")
                    _now_et = _dt.now(tz)
                except Exception:
                    _now_et = _dt.utcnow()
                et_hour = _now_et.hour
                today_et = _now_et.date().isoformat()

                # Digest catch-up: fire one digest per tick using first-and-
                # latest semantics — if the first scheduled hour of today
                # hasn't fired yet, fire it now (preserves the morning
                # overnight summary); otherwise fire the latest scheduled
                # hour that hasn't fired (current state).  Middle hours are
                # skipped.  See ``_pick_digest_target`` for why this returns
                # one key per call rather than a list.
                # Prune fired_digest_keys to today-only so yesterday's keys
                # cannot block today's first/latest fires.
                fired_digest_keys = [k for k in fired_digest_keys if k.startswith(today_et)]
                target_key = _pick_digest_target(et_hour, today_et, set(fired_digest_keys))
                if target_key is not None:
                    for sub in _registry.subscribers:
                        if isinstance(sub, DigestComposer):
                            try:
                                digest = sub.compose()
                                logger.info(
                                    "Digest composed (key=%s, %d chars)",
                                    target_key, len(digest),
                                )
                            except Exception:
                                logger.exception("Digest compose failed")
                    fired_digest_keys.append(target_key)
                    # Reload state here to merge with whatever ``compose()``
                    # wrote during this tick — notably ``last_digest_at``.
                    # Without this re-read, our save below would clobber the
                    # time-window lower bound DigestComposer needs to keep
                    # successive digests non-overlapping.  Regression guard
                    # against a 2026-04-19 bug I introduced in the initial
                    # first-and-latest implementation.
                    try:
                        _state = load_state(digest_state_path(), default={})
                    except Exception:
                        logger.exception("Failed to reload digest state before merge")
                    _state["fired_digest_keys"] = fired_digest_keys
                    # Retire the legacy single-key field once we've committed
                    # to the new multi-key format.  Safe: the migration on
                    # startup already copied it into fired_digest_keys.
                    _state.pop("last_digest_key", None)
                    try:
                        save_state(digest_state_path(), _state)
                    except Exception:
                        logger.exception("Failed to save digest state")

                # WhatsApp morning flush — one per ET date, any time >= 7am.
                # Catches up if gateway was offline during the 7am tick.
                if et_hour >= 7 and last_flush_date != today_et:
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

            # TelegramNotifier LOW-priority batch flush every 60 s.  The
            # subscriber's internal flush is normally triggered from inside
            # ``handle()`` on the next incoming event — which means on a mostly-
            # quiet bus, a batched LOW message can sit well past its 300 s
            # threshold waiting for any other event to arrive.  Driving the
            # flush from the poll loop here bounds delivery latency at roughly
            # ``max_age + 60 s`` regardless of bus traffic.  Safe to call
            # repeatedly: ``_flush_stale_batches`` is a no-op when every
            # buffered key is younger than ``max_age``.
            if _registry and now - last_batch_flush >= 60:
                for sub in _registry.subscribers:
                    if isinstance(sub, TelegramNotifier):
                        try:
                            sub._flush_stale_batches()
                        except Exception:
                            logger.exception(
                                "Batch flush failed for %s", sub.subscriber_id,
                            )
                last_batch_flush = now

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
