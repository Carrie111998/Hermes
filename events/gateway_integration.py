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
from events.producers.resource_monitor import ResourcePressureMonitor
from events.producers.partial_backlog_monitor import PartialBacklogMonitor
from events.state import load_state, save_state
from events.subscribers.base import SubscriberRegistry
from events.subscribers.audit_logger import AuditLogger
from events.subscribers.cron_trigger_log import CronTriggerLog
from events.subscribers.telegram_notifier import TelegramNotifier
from events.subscribers.whatsapp_escalator import WhatsAppEscalator
from events.subscribers.digest_composer import DigestComposer, DIGEST_SCHEDULE_HOURS
from events.subscribers.memory_writer import MemoryWriter
from events.subscribers.telegram_mirror import TelegramMirror
from events.subscribers.mailbox_translator import MailboxTranslator
from events.subscribers.cron_stale_monitor import CronStaleMonitor
from events.subscribers.tracker_intent_applier import (
    TrackerIntentApplierSubscriber,
    tracker_partial_dir,
)
from events.subscribers.critic_trigger import CriticSubscriber
from events.subscribers.scribe_realtime import ScribeRealtime
from events.subscribers.scribe_action_telemetry import ScribeActionTelemetry
from events.subscribers.scribe_voice_tuning import ScribeVoiceTuning

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
# Hourly TRUNCATE-checkpoint attempt. The 60s PASSIVE checkpoint keeps the WAL
# backfilled but can never RESET it, and journal_size_limit only truncates on
# a reset - under the subscribers' 1-2s read cadence a reset almost never
# happens on its own, which is how event_bus.db-wal reached 1.44 GB on
# 2026-07-13. A TRUNCATE that loses to a reader returns busy=1 (no exception);
# we just try again next hour. The nightly event-bus-retention cron (04:52)
# is the backstop and the reporting surface.
WAL_TRUNCATE_INTERVAL_SECONDS = 3600
WAL_WARN_BYTES = 512 * 1024 * 1024
# WhatsApp morning-flush retry throttle. The flush delivers the overnight queue
# OVER WhatsApp, so when the WhatsApp bridge is itself down at 7am (2026-07-10:
# a 0/105 flush stranded the whole overnight queue) the flush fails and must
# RETRY later the same day rather than burning the single per-ET-date attempt.
# Retries are throttled to this interval so a persistently-down bridge can't
# hammer _deliver (which blocks ~5s per failed send) on every 1s poll tick.
FLUSH_RETRY_INTERVAL_SECONDS = 900  # 15 min
# Tick cadence for the tracker-intent-applier's DEDICATED poll thread. Mirrors
# TrackerIntentApplierSubscriber.poll_interval_seconds; the applier runs off
# the shared serial loop (2026-07-13 starvation fix) so this is its real,
# uncontended floor rather than a best-case the serial loop rarely hits.
APPLIER_POLL_INTERVAL_SECONDS = 1
# Auto-re-drive eligible partials at most once/min, on the dedicated applier
# thread (single-writer). Flag-gated at the subscriber (default off — the :4100
# hard gate). See docs/superpowers/specs/2026-07-14-tracker-applier-auto-redrive-design.md.
REDRIVE_INTERVAL_SECONDS = 60
# Read-only partial/ backlog count on the shared subscriber loop, once/min.
PARTIAL_BACKLOG_CHECK_INTERVAL_SECONDS = 60

_bus: Optional[EventBus] = None
_registry: Optional[SubscriberRegistry] = None
_health_monitor: Optional[GatewayHealthMonitor] = None
_resource_monitor: Optional[ResourcePressureMonitor] = None
_partial_backlog_monitor: Optional[PartialBacklogMonitor] = None
_mailbox_watcher: Optional[MailboxWatcher] = None
_subscriber_thread: Optional[threading.Thread] = None
# Dedicated poll thread + its subscriber for the tracker-intent-applier. The
# applier is filesystem-driven and latency-sensitive and MUST NOT share the
# serial _subscriber_poll_loop (where a slow subscriber ahead of it starves
# its ~1s scan). See _applier_poll_loop.
_applier_thread: Optional[threading.Thread] = None
_applier_subscriber: Optional[TrackerIntentApplierSubscriber] = None
_stop_event = threading.Event()
_startup_monotonic: float = 0.0

# Gateway lifecycle dedupe + timing — added 2026-04-30 (M1 in
# profiles/sentinel/workspace/gateway-restart-cluster-2026-04-30.md).
# emit_gateway_stopped() is wired from three paths (graceful _stop_impl +
# atexit + signal handler) to maximize coverage on Windows where SIGTERM
# semantics differ. _gateway_stopped_emitted dedupes so the triple doesn't
# triple-emit. _gateway_started_at_monotonic is captured at started-emit
# time so stopped-emit can include runtime_seconds without a global wallclock.
_gateway_stopped_emitted: bool = False
_gateway_started_at_monotonic: Optional[float] = None


def startup(adapters: Optional[Dict] = None) -> None:
    """Initialize EventBus, register all subscribers, start polling thread."""
    global _bus, _registry, _health_monitor, _resource_monitor, _partial_backlog_monitor, _mailbox_watcher, _subscriber_thread, _applier_thread, _applier_subscriber, _startup_monotonic

    if _bus is not None:
        shutdown()

    logger.info("EventBus: initializing communication layer...")

    _startup_monotonic = time.monotonic()
    _bus = EventBus()
    _registry = SubscriberRegistry()
    _health_monitor = GatewayHealthMonitor(_bus)
    _resource_monitor = ResourcePressureMonitor(_bus)
    # Always-on tracker partial/ backlog alert (independent of the re-drive flag).
    # Construction is side-effect-free (stores the path; counts only on check()).
    _partial_backlog_monitor = PartialBacklogMonitor(
        _bus, partial_dir=tracker_partial_dir(),
    )
    _mailbox_watcher = MailboxWatcher(_bus)

    # Register subscribers
    _registry.register(AuditLogger(_bus))
    _registry.register(CronTriggerLog(_bus))
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
    # Registered like any other subscriber (so startup_all() builds its
    # IntentApplier and shutdown_all()/lag_report() still cover it), but it is
    # driven by a DEDICATED thread below and SKIPPED in _subscriber_poll_loop's
    # serial iteration — never polled from both, since IntentApplier is
    # single-threaded by design.
    _applier_subscriber = TrackerIntentApplierSubscriber(_bus)
    _registry.register(_applier_subscriber)
    _registry.register(CriticSubscriber(_bus))
    _registry.register(ScribeRealtime(_bus))
    _registry.register(ScribeActionTelemetry(_bus))
    _registry.register(ScribeVoiceTuning(_bus))

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

    # Start the dedicated tracker-intent-applier thread. Separate from the
    # serial loop so the WhatsApp/Telegram reconnect blocks during a gateway
    # restart can't starve operator-approval application (2026-07-13 fix).
    _applier_thread = threading.Thread(
        target=_applier_poll_loop,
        daemon=True,
        name="tracker-intent-applier",
    )
    _applier_thread.start()

    logger.info("EventBus: %d subscribers registered, polling started",
                len(_registry.subscribers))


def shutdown() -> None:
    """Stop polling and clean up."""
    global _subscriber_thread, _applier_thread, _applier_subscriber, _bus
    _stop_event.set()
    if _subscriber_thread:
        _subscriber_thread.join(timeout=5)
        _subscriber_thread = None
    # Join+clear the dedicated applier thread too, or a gateway restart
    # (shutdown() then startup()) leaks one applier thread per cycle.
    if _applier_thread:
        _applier_thread.join(timeout=5)
        _applier_thread = None
    _applier_subscriber = None
    if _registry:
        _registry.shutdown_all()
    if _bus:
        _bus.close()
        _bus = None
    logger.info("EventBus: shutdown complete")


def get_bus() -> Optional[EventBus]:
    """Get the global EventBus instance (for use by CronEventEmitter)."""
    return _bus


def emit_gateway_started(boot_payload: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Emit a single GATEWAY_STARTED event. Best-effort; never raises.

    Caller (gateway/run.py) supplies a ``boot_payload`` carrying the
    operator-relevant context: parent_pid + parent_cmdline + argv +
    boot_reason + previous_pid (when discoverable). ``pid`` is auto-filled
    from os.getpid().

    Records ``_gateway_started_at_monotonic`` so a subsequent
    emit_gateway_stopped() call can include runtime_seconds without a
    global wallclock comparison.

    Returns the emitted event_id, or None if the bus is unavailable.
    """
    global _gateway_started_at_monotonic
    if _bus is None:
        return None
    _gateway_started_at_monotonic = time.monotonic()
    payload: Dict[str, Any] = {"pid": os.getpid()}
    if boot_payload:
        payload.update(boot_payload)
    try:
        from events.schema import EventType
        return _bus.emit(
            event_type=EventType.GATEWAY_STARTED,
            source="gateway",
            payload=payload,
        )
    except Exception:
        logger.exception("Failed to emit GATEWAY_STARTED")
        return None


def emit_gateway_stopped(stop_payload: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Emit a GATEWAY_STOPPED event exactly once per gateway process.

    Idempotent: subsequent calls are no-ops. This is the contract that
    justifies wiring three callers in gateway/run.py main():
      1. graceful _stop_impl path (knows the most context — exit_reason,
         restart vs shutdown, drain timeout)
      2. atexit.register hook (catches sys.exit / unhandled exception
         fall-through after _stop_impl was bypassed)
      3. signal handlers (SIGINT / SIGTERM / SIGBREAK on Windows) — fire
         before the asyncio loop notices the signal

    The first caller wins — the graceful path holds the most-informed
    exit_reason, so we preserve it even if atexit fires later.

    WMI Terminate (Windows TerminateProcess) bypasses all three. For that
    case, the next gateway boot's M2 stale-lock detection synthesizes a
    GATEWAY_STOPPED(reason=detected_dead, previous_pid=X) on behalf of the
    dead process.

    If ``_bus`` is None we return without flipping the dedupe flag, so
    a later call after the bus comes up (e.g. mid-startup crash where
    only atexit fires before bus init) can still emit the canonical event.

    Returns the emitted event_id, or None if the bus is unavailable or
    the event was already emitted.
    """
    global _gateway_stopped_emitted
    if _gateway_stopped_emitted:
        return None
    if _bus is None:
        return None
    payload: Dict[str, Any] = {"pid": os.getpid()}
    if _gateway_started_at_monotonic is not None:
        payload["runtime_seconds"] = round(
            time.monotonic() - _gateway_started_at_monotonic, 3
        )
    if stop_payload:
        payload.update(stop_payload)
    try:
        from events.schema import EventType
        event_id = _bus.emit(
            event_type=EventType.GATEWAY_STOPPED,
            source="gateway",
            payload=payload,
        )
        _gateway_stopped_emitted = True
        return event_id
    except Exception:
        logger.exception("Failed to emit GATEWAY_STOPPED")
        return None


def get_health_monitor() -> Optional[GatewayHealthMonitor]:
    """Get the health monitor (for gateway adapter health checks)."""
    return _health_monitor


def get_resource_monitor() -> Optional[ResourcePressureMonitor]:
    """Get the resource-pressure monitor (commit/pagefile/disk sampling)."""
    return _resource_monitor


def get_partial_backlog_monitor() -> Optional[PartialBacklogMonitor]:
    """Get the tracker partial-backlog monitor (counts mailbox/tracker/partial/)."""
    return _partial_backlog_monitor


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


def _should_attempt_whatsapp_flush(
    et_hour: int,
    last_flush_date: str,
    today_et: str,
    now: float,
    last_flush_attempt: float,
    retry_interval: float = FLUSH_RETRY_INTERVAL_SECONDS,
) -> bool:
    """Whether the WhatsApp morning flush should be ATTEMPTED on this tick.

    True iff it is >= 7am ET, today's queue has not yet been *successfully*
    drained (``last_flush_date`` advances only on a clean drain — see the caller),
    and we have not attempted within ``retry_interval``.

    The throttle is what lets a FAILED flush retry safely: on 2026-07-10 the
    WhatsApp bridge was down at 7am, the flush delivered 0/105, and the old gate
    burned the day's single attempt (advancing the date unconditionally) so the
    overnight queue stranded until the next ET date. Retrying is now allowed, but
    throttled so a persistently-down bridge cannot re-attempt on every 1s poll
    tick (each failed ``_deliver`` blocks ~5s).
    """
    if et_hour < 7:
        return False
    if last_flush_date == today_et:
        return False
    return (now - last_flush_attempt) >= retry_interval


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
    # Deliberately NOT 0: resource pressure is a continuous condition, so the
    # first sample can wait a full interval. Sampling on tick zero would
    # re-fire the rising edge on EVERY gateway restart while an episode
    # persists (the monitor's edge state is in-process), so a crash-loop
    # under sustained pressure would alert once per restart, bypassing the
    # re-alert cooldown. It also keeps the first tick light: the sampler
    # reads the real host (kernel32 + disk stat) and fires real emits, which
    # gi.startup()-based tests would otherwise pay on every startup.
    last_resource_check: float = time.monotonic()
    # Skip the boot tick like last_resource_check: reconnect storms make the
    # first sample noisy, and the edge state is in-process so a crash-loop under
    # a sustained backlog would re-fire the rising edge on every restart.
    last_partial_backlog_check: float = time.monotonic()
    last_lag_check: float = 0
    last_cleanup: float = 0
    last_checkpoint: float = 0
    # NOT 0: skip the boot tick (reconnect storms make TRUNCATE lose anyway);
    # first attempt comes one interval in, mirroring last_resource_check.
    last_wal_truncate: float = time.monotonic()
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
    # Monotonic timestamp of the last morning-flush ATTEMPT (success or fail),
    # throttling failed-flush retries. In-memory only (resets on restart, which
    # is fine — a restart should re-attempt promptly). -inf => first eligible
    # tick attempts immediately.
    last_flush_attempt: float = float("-inf")
    lag_alerts_sent: Dict[str, float] = {}  # subscriber_id -> monotonic timestamp
    consecutive_outer_errors: int = 0
    last_outer_error_emit: float = 0.0

    while not _stop_event.is_set():
        try:
            now = time.monotonic()

            # Poll each subscriber at its own interval
            if _registry:
                for sub in _registry.subscribers:
                    # The tracker-intent-applier is driven by its OWN dedicated
                    # thread (_applier_poll_loop) so a slow subscriber ahead of
                    # it here can't starve its latency-sensitive ~1s filesystem
                    # scan (2026-07-13 restart-starvation fix). It stays
                    # registered for startup/shutdown/lag, but must NOT be
                    # polled here too — the single-threaded IntentApplier
                    # (is_applied/mark_applied, _move_to) is not race-free
                    # against a second concurrent caller.
                    if isinstance(sub, TrackerIntentApplierSubscriber):
                        continue
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

                # WhatsApp morning flush — one SUCCESSFUL drain per ET date, any
                # time >= 7am. Catches up if the gateway was offline during the
                # 7am tick. Also RETRIES (throttled) later the same day if the
                # flush itself FAILED — e.g. the WhatsApp bridge down at 7am on
                # 2026-07-10, whose 0/105 flush previously burned the day's only
                # attempt (last_flush_date was advanced unconditionally) and
                # stranded the overnight queue until the next ET date. Now the
                # date advances ONLY once the queue actually drained.
                _flush_now = _should_attempt_whatsapp_flush(
                    et_hour, last_flush_date, today_et, now, last_flush_attempt
                )
                # Stranded-queue retry (2026-07-11 comms audit): the
                # escalator now REQUEUES failed immediate/throttled sends
                # into the quiet queue. Once today's morning drain has
                # already consumed last_flush_date, the morning gate above
                # never re-fires — so a message stranded by a mid-day
                # bridge outage would wait for tomorrow 7am. Re-attempt a
                # non-empty queue outside quiet hours (07-23 ET), on the
                # same failed-flush throttle interval.
                if (not _flush_now
                        and _registry is not None
                        and 7 <= et_hour < 23
                        and now - last_flush_attempt >= FLUSH_RETRY_INTERVAL_SECONDS):
                    _flush_now = any(
                        isinstance(sub, WhatsAppEscalator) and sub.has_queued_messages()
                        for sub in _registry.subscribers
                    )
                if _flush_now:
                    last_flush_attempt = now
                    all_drained = True
                    for sub in _registry.subscribers:
                        if isinstance(sub, WhatsAppEscalator):
                            try:
                                count = sub.flush_queue()
                                if count:
                                    logger.info("WhatsApp morning flush: %d messages", count)
                                # A preserved (non-empty) queue means delivery
                                # failed or was partial — do NOT consume today's
                                # slot; a later throttled tick retries.
                                if sub.has_queued_messages():
                                    all_drained = False
                            except Exception:
                                logger.exception("WhatsApp flush failed")
                                all_drained = False
                    if all_drained:
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

            # Resource-pressure sampling every 60 seconds — commit charge,
            # physical RAM, pagefile allocation, and C: free. Emits
            # RESOURCE_PRESSURE on the rising edge of any trigger (2026-06-11
            # pagefile-burst + 2026-07-16 paging-storm remediation).
            if _resource_monitor and now - last_resource_check >= 60:
                try:
                    _resource_monitor.check()
                except Exception:
                    logger.exception("Resource pressure check failed")
                last_resource_check = now

            # Tracker partial-backlog check every 60s — counts
            # mailbox/tracker/partial/ and emits TRACKER_PARTIAL_BACKLOG on the
            # rising edge of count > threshold (2026-07-14; the 07-13 pileup sat
            # ~a day unnoticed). Read-only, so it runs here in the shared loop
            # rather than the latency-sensitive applier thread.
            if _partial_backlog_monitor and now - last_partial_backlog_check >= PARTIAL_BACKLOG_CHECK_INTERVAL_SECONDS:
                try:
                    _partial_backlog_monitor.check()
                except Exception:
                    logger.exception("Partial backlog check failed")
                last_partial_backlog_check = now

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

            # Hourly WAL TRUNCATE attempt (see WAL_TRUNCATE_INTERVAL_SECONDS)
            if _bus and now - last_wal_truncate >= WAL_TRUNCATE_INTERVAL_SECONDS:
                try:
                    result = _bus.checkpoint("TRUNCATE")
                    wal_path = _bus.db_path.parent / (_bus.db_path.name + "-wal")
                    wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
                    if result is not None and result[0]:
                        logger.info(
                            "event_bus WAL TRUNCATE lost to readers (wal %.0f MB); retrying in 1h",
                            wal_bytes / 1e6,
                        )
                    if wal_bytes > WAL_WARN_BYTES:
                        logger.warning(
                            "event_bus WAL at %.0f MB despite hourly TRUNCATE attempts",
                            wal_bytes / 1e6,
                        )
                except Exception:
                    logger.exception("event_bus WAL truncate attempt failed")
                last_wal_truncate = now

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


def _applier_poll_loop() -> None:
    """Dedicated poll thread for the tracker-intent-applier subscriber.

    The applier is filesystem-driven and latency-sensitive: an operator
    approval lands as an intent file that must be applied within the
    dashboard's ~90s fast-revert window, or postgres-sync reverts the stage
    and the old-stage/revert bug reappears. Running it inside the shared
    ``_subscriber_poll_loop`` starved it for MINUTES during gateway restarts
    (proven live 2026-07-13): WhatsApp connect blocks ~30s and Telegram
    reconnects with ConnectionReset storms, and both subscribers are
    registered AHEAD of the applier, so they monopolise the single serial
    thread while intents sit unapplied. This dedicated thread guarantees the
    applier's ~1s scan cadence independent of the other 12 subscribers and the
    periodic maintenance blocks (health check, mailbox scan, WhatsApp flush).

    ``IntentApplier`` is single-threaded by design (``is_applied`` /
    ``mark_applied`` and ``_move_to`` are not race-free against concurrent
    callers). This thread is the SOLE driver of the applier —
    ``_subscriber_poll_loop`` skips the applier subscriber precisely so the
    two never both poll it. The direct ``_applier_subscriber`` reference (not
    a ``_registry`` scan) also keeps this loop ticking even if the serial loop
    is wedged iterating a slow subscriber.

    Mirrors ``_subscriber_poll_loop``'s outer-try safety net: any exception
    from a single scan is logged and swallowed so the thread keeps ticking
    rather than dying silently (the 2026-04-16 silent-failure mode).
    """
    interval = APPLIER_POLL_INTERVAL_SECONDS
    if _applier_subscriber is not None:
        interval = getattr(
            _applier_subscriber, "poll_interval_seconds", interval
        ) or interval

    # Skip the boot tick (reconnect-storm window); first re-drive one interval in.
    last_redrive = time.monotonic()

    while not _stop_event.is_set():
        try:
            if _applier_subscriber is not None:
                _applier_subscriber.poll()
        except Exception:
            logger.exception("tracker-intent-applier dedicated poll failed")
        # Auto-re-drive eligible partials at most once/min, on THIS single-writer
        # thread (never the shared loop) so it can't race scan_inbox's
        # is_applied/mark_applied/_move_to. Flag-gated inside the subscriber
        # (TRACKER_APPLIER_REDRIVE_ENABLED, default off — the :4100 hard gate).
        now = time.monotonic()
        if _applier_subscriber is not None and now - last_redrive >= REDRIVE_INTERVAL_SECONDS:
            try:
                _applier_subscriber.redrive_partials()
            except Exception:
                logger.exception("tracker-intent-applier redrive failed")
            last_redrive = now
        _stop_event.wait(timeout=interval)
