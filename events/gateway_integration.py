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
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from events.bus import EventBus
from events.paths import (
    cron_stale_thresholds_path,
    digest_state_path,
    gateway_heartbeat_path,
    whatsapp_flush_state_path,
)
from events.producers.ai_usage_monitor import AIUsageCollectorMonitor
from events.producers.health_monitor import GatewayHealthMonitor
from events.producers.mailbox_watcher import MailboxWatcher
from events.producers.resource_monitor import ResourcePressureMonitor
from events.producers.code_drift_monitor import CodeDriftMonitor, watched_repos
from events.producers.partial_backlog_monitor import PartialBacklogMonitor
from events import roster as _roster
from events.roster import RosterError, load_roster
from events.state import load_state, save_state
from events.subscribers.base import SubscriberRegistry
from events.subscribers.audit_logger import AuditLogger
from events.subscribers.cron_trigger_log import CronTriggerLog
from events.subscribers.telegram_notifier import TelegramNotifier
from events.subscribers.whatsapp_escalator import WhatsAppEscalator
from events.subscribers.digest_composer import DigestComposer, DIGEST_SCHEDULE_HOURS
from events.subscribers.memory_writer import MemoryWriter
from events.subscribers.jobflow_dispatcher import JobFlowDispatcher
from events.subscribers.mailbox_translator import MailboxTranslator
from events.subscribers.cron_stale_monitor import CronStaleMonitor
from events.subscribers.tracker_intent_applier import (
    TrackerIntentApplierSubscriber,
    tracker_partial_dir,
)
from events.subscribers.critic_trigger import CriticSubscriber
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
# Budget for the BEST-EFFORT tail of shutdown()'s drain.  GATEWAY_STOPPED
# consumers are drained first and never skipped; everything else yields to this
# deadline.  Bounded because teardown creeping toward gateway/status.py's
# _TASKKILL_TIMEOUT_S is what leaves the gateway DOWN on this box: a stop that
# outruns the cap gets force-killed mid-teardown.
SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 10.0
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
_ai_usage_monitor: Optional[AIUsageCollectorMonitor] = None
_code_drift_monitors: List[CodeDriftMonitor] = []
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


def _verify_subscriber_roster() -> None:
    """Announce any drift between what startup() REGISTERED and the roster.

    The roster (``events/subscriber_roster.json``) is the single source of
    truth shared with ``scripts/event_bus_retention.py`` and
    ``hermes_cli/events_doctor.py``.  Keeping those in sync by hand failed
    twice with operator-visible consequences (see events/roster.py), so the
    check runs here, at the only place that holds the ANSWER rather than a
    guess: real subscriber objects, whose ``subscriber_id`` needs no parsing
    and whose conditional registrations (jobflow-dispatcher's try/except)
    have already resolved for this boot.

    Report-only by design.  A roster typo must never take down the WhatsApp
    gateway — so drift logs at ERROR and emits ONE HIGH-priority AGENT_ERROR,
    which telegram-notifier delivers within a poll cycle.  Emitting after
    construction is safe: every subscriber seeded its cursor at the bus head
    during __init__, so this event's rowid is past every cursor and will be
    delivered rather than skipped.
    """
    if _registry is None:
        return
    registered = {s.subscriber_id for s in _registry.subscribers if s.subscriber_id}
    # Read the module attribute rather than a name bound at import: this must
    # report the path actually in effect, and it is only the FALLBACK anyway --
    # a successful load overwrites it with the file that was really read.
    roster_path = _roster.ROSTER_PATH
    payload: Dict[str, Any] = {
        "subscriber_id": "roster-check",
        "registered": sorted(registered),
    }
    problems: List[str] = []

    try:
        roster = load_roster()
    except RosterError as exc:
        # Fail LOUD, not closed: the gateway keeps running, but nobody gets to
        # believe the roster was checked.
        problems.append(f"roster could not be loaded ({exc})")
    else:
        roster_path = roster.source or roster_path
        retired = roster.retired
        unregistered = sorted(roster.live - registered)
        unlisted = sorted(registered - roster.live)
        if unregistered:
            problems.append(
                "roster says live but startup() registered nothing for: "
                + ", ".join(unregistered)
            )
            payload["live_but_not_registered"] = unregistered
        if unlisted:
            problems.append(
                "registered but not live in the roster: "
                + ", ".join(
                    f"{sid} (roster: retired {retired[sid].since})"
                    if sid in retired else f"{sid} (absent from roster)"
                    for sid in unlisted
                )
            )
            payload["registered_but_not_live"] = unlisted

    if not problems:
        logger.info(
            "EventBus: subscriber roster verified — %d registered subscribers match",
            len(registered),
        )
        return

    detail = "; ".join(problems)
    payload["error"] = "subscriber roster drift"
    payload["detail"] = detail
    payload["roster_path"] = str(roster_path)
    logger.error(
        "EventBus: SUBSCRIBER ROSTER DRIFT — %s. Fix %s (and note that "
        "scripts/event_bus_retention.py classifies subscriber_cursors rows from it).",
        detail, roster_path,
    )
    if _bus is None:
        return
    try:
        from events.schema import EventType, Priority
        _bus.emit(
            event_type=EventType.AGENT_ERROR,
            source="event-bus",
            payload=payload,
            priority=Priority.HIGH,
        )
    except Exception:
        logger.exception("Failed to emit subscriber-roster drift event")


def startup(adapters: Optional[Dict] = None) -> None:
    """Initialize EventBus, register all subscribers, start polling thread."""
    global _bus, _registry, _health_monitor, _resource_monitor, _ai_usage_monitor, _code_drift_monitors, _partial_backlog_monitor, _mailbox_watcher, _subscriber_thread, _applier_thread, _applier_subscriber, _startup_monotonic

    if _bus is not None:
        shutdown()

    logger.info("EventBus: initializing communication layer...")

    _startup_monotonic = time.monotonic()
    _bus = EventBus()
    _registry = SubscriberRegistry()
    _health_monitor = GatewayHealthMonitor(_bus)
    _resource_monitor = ResourcePressureMonitor(_bus)
    # Resident AI-usage collection. Default mode is 'shadow': it writes to
    # ai-tokens-resident.json while the AIUsageCollector scheduled task keeps
    # owning the real snapshot, so the two can be diffed before any cutover.
    # Construction is side-effect-free and imports nothing heavy -- the
    # collector's own imports are paid inside the worker on first run.
    _ai_usage_monitor = AIUsageCollectorMonitor()
    # One monitor per repo whose WORKING TREE is deployed code, each with
    # its own trunk ref and its own episode-state file (2026-07-28: added
    # ~/.hermes on `master` alongside agent-src on `main`).
    _code_drift_monitors = [CodeDriftMonitor(_bus, repo=r) for r in watched_repos()]
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
    # Event-driven JobFlow activation. Registered unconditionally so its
    # cursor advances and lag_report() covers it, but INERT unless
    # HERMES_JOBFLOW_EVENT_DISPATCH is set (default 'off' -> handle() returns
    # immediately). 'shadow' records would-wake decisions without acting.
    # Activation goes through cron.wake_channel, never jobs.json.
    try:
        from jobflow_dispatch.store import ActivationStore, default_ledger_path

        _registry.register(
            JobFlowDispatcher(_bus, ActivationStore(default_ledger_path()))
        )
    except Exception:
        logger.exception("JobFlowDispatcher registration failed; continuing without it")
    # Registered like any other subscriber (so startup_all() builds its
    # IntentApplier and shutdown_all()/lag_report() still cover it), but it is
    # driven by a DEDICATED thread below and SKIPPED in _subscriber_poll_loop's
    # serial iteration — never polled from both, since IntentApplier is
    # single-threaded by design.
    _applier_subscriber = TrackerIntentApplierSubscriber(_bus)
    _registry.register(_applier_subscriber)
    _registry.register(CriticSubscriber(_bus))
    # ScribeRealtime retired 2026-07-18 (routing v3, P2 one-event-one-
    # message): its narrated mailbox_message NOTIFICATION copies duplicated
    # the typed delivery of the same 7 event types (interview/offer landed
    # in THREE topics per event). Typed events are canonical; plain-language
    # bodies live in events/formatting.py. Class + tests kept for history.
    # _registry.register(ScribeRealtime(_bus))
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

    # Registration is complete — assert it against the canonical roster before
    # anything starts polling.  This is the mechanism that makes roster drift
    # self-announcing instead of surfacing as a nightly retention warning that
    # tells the operator to prune a live cursor (2026-08-23, af05110a).
    _verify_subscriber_roster()

    # Start subscriber polling thread
    _stop_event.clear()
    _subscriber_thread = threading.Thread(
        target=_subscriber_poll_loop,
        # Capture the env-derived write targets NOW — at startup, where their
        # meaning is fixed — and carry them. shutdown()'s join has a 5s
        # timeout, so on a loaded box this thread can outlive the test that
        # started it.
        args=(gateway_heartbeat_path(), whatsapp_flush_state_path()),
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


def _consumes_gateway_stopped(subscriber) -> bool:
    """Whether ``subscriber`` would be handed a GATEWAY_STOPPED event.

    ``event_types is None`` means "no filter" — the subscriber receives every
    event — so those count too. AuditLogger is the consumer that matters here:
    it is how the shutdown event reaches audit.jsonl before the bus closes.
    """
    from events.schema import EventType

    event_types = getattr(subscriber, "event_types", None)
    if event_types is None:
        return True
    try:
        return EventType.GATEWAY_STOPPED in event_types
    except TypeError:
        return False


def _drain_subscribers_for_shutdown(
    registry,
    skip=(),
    timeout_seconds: Optional[float] = None,
) -> Dict[str, int]:
    """Poll subscribers one last time so teardown-time events are delivered.

    ``gateway/run.py`` emits GATEWAY_STOPPED early in its stop path and calls
    :func:`shutdown` late in ``main()``. Without this drain, delivery inside
    that window depends on the poll loop happening to tick before the bus
    closes. What that buys is a RECORD: AuditLogger writing the shutdown event
    into audit.jsonl, and the other subscribers seeing whatever else landed
    during teardown.

    ⚠ **It is NOT what makes the cron shutdown-attribution work, despite what
    bc07363000's commit message claims.** That commit justified this drain by
    ``CronStaleMonitor`` needing to see GATEWAY_STOPPED before the process
    died. The premise was falsified on 2026-08-17: PID 10168 was force-killed
    INSIDE ``_drain_active_agents`` — it logged ``notify_active_sessions done
    at +1.76s`` and never ``drain done`` — so this function never ran, and two
    genuinely-killed crons went unreported. The general form is that the
    gateway's drain budget and ``gateway/status.py``'s ``_TASKKILL_TIMEOUT_S``
    are both ~30s, so any hook at or after the drain is reachable only when the
    drain ended early, i.e. only when nothing was killed and there is nothing
    to report. Attribution therefore moved to the SUCCESSOR, rebuilt from the
    bus in ``CronStaleMonitor.startup()`` (517cc56c97), where it needs nothing
    from this path at all. Do not re-derive a shutdown-time reporting hook from
    that commit message; it has been tried and measured.

    GATEWAY_STOPPED consumers go FIRST and are never skipped; the rest are
    best-effort under ``timeout_seconds``. The deadline gates whether a
    subscriber is STARTED, not how long it may take — a single wedged
    subscriber can still overrun it, exactly as it can in the poll loop.
    """
    if registry is None:
        return {}
    if timeout_seconds is None:
        timeout_seconds = SHUTDOWN_DRAIN_TIMEOUT_SECONDS
    excluded = {id(sub) for sub in skip if sub is not None}

    guaranteed: List[Any] = []
    best_effort: List[Any] = []
    for sub in registry.subscribers:
        if id(sub) in excluded:
            continue
        if _consumes_gateway_stopped(sub):
            guaranteed.append(sub)
        else:
            best_effort.append(sub)

    results: Dict[str, int] = {}

    def _poll_once(sub) -> None:
        try:
            results[sub.subscriber_id] = sub.poll()
        except Exception:
            logger.exception(
                "Shutdown drain: %s poll failed", sub.subscriber_id,
            )
            results[sub.subscriber_id] = 0

    for sub in guaranteed:
        _poll_once(sub)

    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    for index, sub in enumerate(best_effort):
        if time.monotonic() >= deadline:
            # Never silent: a bounded sweep that does not say what it dropped
            # reads as full coverage.
            logger.warning(
                "Shutdown drain: %.1fs budget exhausted, %d subscriber(s) not "
                "drained: %s",
                timeout_seconds,
                len(best_effort) - index,
                ", ".join(s.subscriber_id for s in best_effort[index:]),
            )
            break
        _poll_once(sub)

    return results


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
    # Deliver whatever landed on the bus during teardown — chiefly getting the
    # GATEWAY_STOPPED this process emitted into audit.jsonl before the bus
    # closes. Best-effort by nature: a teardown force-killed before this point
    # skips it entirely, which is why nothing CORRECTNESS-critical may depend
    # on it (see _drain_subscribers_for_shutdown). AFTER the joins so no
    # subscriber is polled from two threads at once, and BEFORE close() so the
    # bus is still open. The applier is excluded for the same reason
    # _subscriber_poll_loop excludes it: its join above is bounded at 5s, so it
    # may still be running, and IntentApplier is single-threaded by design.
    if _registry:
        _drain_subscribers_for_shutdown(_registry, skip=(_applier_subscriber,))
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


def get_code_drift_monitors() -> List[CodeDriftMonitor]:
    """All code-drift monitors (one per watched repo, checkout-vs-trunk)."""
    return _code_drift_monitors


def get_code_drift_monitor() -> Optional[CodeDriftMonitor]:
    """The agent-src code-drift monitor, or None before startup().

    Back-compat accessor from when agent-src was the only watched repo;
    prefer get_code_drift_monitors().
    """
    return _code_drift_monitors[0] if _code_drift_monitors else None


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


def _write_heartbeat(
    consecutive_outer_errors: int, *, path: Optional[Path] = None
) -> None:
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

    ``path`` lets a caller CARRY a target captured when its meaning was fixed
    rather than re-resolving ``HERMES_HOME`` on every tick. The early-boot
    heartbeat thread needs this: it can outlive the scope that started it, and
    a per-tick resolve follows the env to whatever a test's teardown restores.
    Direct callers pass nothing and resolve live — correct, since the process
    still holds the home it was launched with.
    """
    if path is None:
        path = gateway_heartbeat_path()
    elif not path.parent.exists():
        # The captured home is gone (e.g. a torn-down pytest tmp_path).
        # Leave no litter — do not recreate someone else's deleted directory.
        return
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
    ``DigestComposer.compose()`` advances its rowid watermark
    (``self._last_digest_rowid``) after every call and uses it as the
    window's lower bound.

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


def _resolve_whatsapp_state_path(path: Optional[Path]) -> Path:
    """Return the carried whatsapp flush-state path, or resolve one live.

    Direct (non-deferred) callers pass ``None`` and resolve against the current
    ``HERMES_HOME`` — correct, because the process still holds the home it was
    launched with. A deferred caller binds the path at the moment its meaning is
    fixed and passes it in, so a later env change cannot move the write.
    """
    return path if path is not None else whatsapp_flush_state_path()


def _resolve_digest_state_path(path: Optional[Path]) -> Path:
    """Return the carried digest-state path, or resolve one live.

    Same contract as :func:`_resolve_whatsapp_state_path`.
    """
    return path if path is not None else digest_state_path()


def _subscriber_poll_loop(
    heartbeat_path: Optional[Path] = None,
    whatsapp_state_path: Optional[Path] = None,
    digest_path: Optional[Path] = None,
) -> None:
    """Background thread that polls all subscribers at their configured intervals.

    ``heartbeat_path`` and ``whatsapp_state_path`` are captured by
    :func:`startup` and carried in, so a tick that lands after ``shutdown()``'s
    bounded ``join(timeout=5)`` gives up still writes to the home this loop was
    started with. Re-resolving either per tick would follow ``HERMES_HOME`` into
    whatever a test's teardown restored — and ``save_state`` mkdirs its parent
    before writing, so it would create the directory too.

    Outer try/except is a safety net: every sub-operation below already has
    its own try/except, but a handful of state saves and iterations are not
    individually guarded, and an uncaught exception would kill this thread
    and produce exactly the silent-notification failure mode we fixed in
    the 2026-04-16 Post-Silence-Fix Addendum.  If the outer catch fires,
    we log, emit an agent_error event (rate-limited), and continue ticking.
    """
    whatsapp_state_target = _resolve_whatsapp_state_path(whatsapp_state_path)
    digest_state_target = _resolve_digest_state_path(digest_path)
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
    _state = load_state(digest_state_target, default={})
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
    _flush_state = load_state(whatsapp_state_target, default={})
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
                        _state = load_state(digest_state_target, default={})
                    except Exception:
                        logger.exception("Failed to reload digest state before merge")
                    _state["fired_digest_keys"] = fired_digest_keys
                    # Retire the legacy single-key field once we've committed
                    # to the new multi-key format.  Safe: the migration on
                    # startup already copied it into fired_digest_keys.
                    _state.pop("last_digest_key", None)
                    try:
                        save_state(digest_state_target, _state)
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
                            save_state(whatsapp_state_target, {"last_flush_date": today_et})
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

            # Code-drift probe — each deployed checkout vs its own trunk ref
            # (2026-07-20/21 agent-src stale-restart incident; 2026-07-28
            # ~/.hermes 62-commit stale-checkout incident). Every monitor
            # self-gates to one read-only git sample per 15 min, so the
            # per-tick call is a clock comparison.
            for _drift_monitor in _code_drift_monitors:
                try:
                    _drift_monitor.check()
                except Exception:
                    logger.exception("Code drift check failed (%s)",
                                     _drift_monitor.repo_name)

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

            # Resident AI-usage collection (2026-08-26). Self-gating like the
            # drift monitors above, so the per-tick cost is a clock comparison.
            # check() NEVER does the work on this thread: a collection is 40-60s
            # of network plus a CDP browser probe, which would stall the bus for
            # a minute every interval. It hands off to a daemon worker and reaps
            # on a later tick, at most one run in flight.
            if _ai_usage_monitor:
                try:
                    _ai_usage_monitor.check()
                except Exception:
                    logger.exception("AI usage collection check failed")

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
                # Refresh planner statistics AFTER the prune, so they describe
                # the table that callers will actually query. Separate try so a
                # failed cleanup still gets fresh stats and vice versa. Cheap
                # by construction (~0.010s via analysis_limit) — see
                # EventBus.analyze; nothing else re-analyzes this DB, so
                # without this the stats taken on 2026-07-23 would drift
                # further out of date the more the bus grows.
                try:
                    _bus.analyze()
                except Exception:
                    logger.exception("Event bus ANALYZE failed")
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
                    _write_heartbeat(consecutive_outer_errors, path=heartbeat_path)
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
            # Reap capped partials PG+canonical both show converged (own flag,
            # default off). Runs after redrive so it mops up exactly what redrive
            # just classified capped. Single-writer thread; own try/except so a
            # reap failure never stalls the loop.
            try:
                _applier_subscriber.reap_converged_partials()
            except Exception:
                logger.exception("tracker-intent-applier reap failed")
            last_redrive = now
        _stop_event.wait(timeout=interval)
