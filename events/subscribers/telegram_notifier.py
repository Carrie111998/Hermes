"""TelegramNotifier subscriber — routes events to Telegram forum topics.

v3 (2026-07-18): ALL routing decisions live in events.routing_policy —
this subscriber owns delivery mechanics only (verbosity gate, noise
guards, batching, sends, reverse signals). One event → exactly one
Telegram message (cross-posting removed); noise guards enforce P4/P6
(no no-op messages, no verbatim repeats, flaps collapse to incidents).
Design: docs/superpowers/specs/2026-07-18-notification-routing-v3-design.md
(~/.hermes parent repo).

Reads topic registry from ~/.hermes/telegram/topics.json and verbosity
config from ~/.hermes/telegram/verbosity.json.  Delivers messages via
the gateway's Telegram adapter send() method.
"""

import dataclasses
import json
import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from events.bus import EventBus
from events.noise_guards import (
    FlapGuard,
    RepeatGuard,
    is_off_ladder_consecutive_failure,
    is_sustained_resource_repeat,
    is_noop_cron_output,
    normalize_for_fingerprint,
    strip_agent_iteration_json,
)
from events.paths import notifier_batch_path
from events.routing_policy import (
    Attention,
    Route,
    WA_IMMEDIATE,
    classify,
    resolve_topic_thread,
)
from events.schema import Event, EventType, Priority
from events.state import load_state, save_state
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

# v3: the event→topic table moved to events.routing_policy._POLICY (the
# single source of truth both delivery subscribers consult). The pre-v3
# TOPIC_ROUTING dict below was deleted 2026-07-18; the ScribeRealtime dual
# delivery path it documented is retired in the same change (typed events
# are canonical — one event, one message).
# Event types treated as "digest-class" — once-a-day aggregate summaries
# that carry no incident urgency on their own but whose absence is meaningful
# (a missing daily heartbeat = the producer might be dead). Used by the
# ``digest_only`` verbosity branch to let these pass at NORMAL priority
# alongside HIGH+ failure-fires, while still dropping routine NORMAL/LOW
# chatter. See WATCHDOG_DAILY's schema docstring (2026-04-30) and the
# verbosity gate inside handle().
DIGEST_EVENT_TYPES = frozenset({
    EventType.CURATOR_DAILY,
    EventType.WATCHDOG_DAILY,
    EventType.DIGEST_GENERATED,
})

# Cycle-prevention: this subscriber emits NOTIFICATION_DELIVERED /
# NOTIFICATION_FAILED from inside _deliver(); if it ALSO consumed those
# events, every send would feed a delivery event back through the same
# subscriber and recurse (LOW would batch the recursion; NORMAL+ would
# loop synchronously). Documented in the BaseSubscriber docstring rule:
# no subscriber that emits a delivery event may consume one. See
# docs/superpowers/specs/2026-04-30-notification-delivered-design.md
# §"Cycle prevention" for why this is an in-handle early-return rather
# than a class-level negative filter.
_NEVER_CONSUME = frozenset({
    EventType.NOTIFICATION_DELIVERED,
    EventType.NOTIFICATION_FAILED,
})

CRON_SUMMARY_MAX_LINES = 24
CRON_SUMMARY_MAX_CHARS = 1500
CRON_SUMMARY_TRUNCATION_NOTE = (
    "[Mission Control trimmed the rest; full run output remains in cron history/artifacts.]"
)

# Receiver-side LRU dedup for AGENT_FAILURE_CLUSTER (Option C in the
# 2026-04-29 watchdog-dedup proposal). Caches (source, 30-min bucket)
# pairs that have already been delivered to Telegram so a near-duplicate
# cluster event for the same agent in the same window is suppressed at
# the chat layer. The bus event itself flows through unchanged so
# downstream consumers (Critic substrate, audit logger) still see both.
CLUSTER_DEDUP_BUCKET_SECONDS = 30 * 60
CLUSTER_DEDUP_LRU_SIZE = 256

# P4: cron_started / cron_triggered carry zero operator information (the
# completion event covers the run). Bus-only — audit/critic consumers see
# them; chat never does. Pre-v3 they batched ~4,760 lines/week into the
# cron firehose. cron_skipped* stay deliverable (rare + meaningful).
_CRON_BUS_ONLY = frozenset({
    EventType.CRON_STARTED,
    EventType.CRON_TRIGGERED,
})

# TRACE batching: hourly digest per thread (observability cadence, not
# real-time), flushed early at 20 buffered messages so a busy hour still
# produces bounded, readable combos.
BATCH_FLUSH_SECONDS = 3600.0
BATCH_MAX_MESSAGES = 20
# Minimum gap between flush attempts for a key whose last send FAILED.
# _flush_stale_batches runs on every handled event, so without this a
# restored stale batch would hammer a dead Telegram once per event
# (2026-07-20: the transport sat in an httpx.ReadError reconnect loop
# for hours — retrying per event buys nothing and floods the log).
BATCH_RETRY_BACKOFF_SECONDS = 120.0


class TelegramNotifier(BaseSubscriber):
    subscriber_id = "telegram-notifier"
    poll_interval_seconds = 5

    def __init__(
        self,
        bus: EventBus,
        topics_path: Optional[Path] = None,
        verbosity_path: Optional[Path] = None,
        send_fn: Optional[Callable] = None,
    ):
        super().__init__(bus)
        if topics_path is None:
            from events.paths import telegram_topics_path
            topics_path = telegram_topics_path()
        if verbosity_path is None:
            from events.paths import telegram_verbosity_path
            verbosity_path = telegram_verbosity_path()

        self._topics_path = Path(topics_path)
        self._verbosity_path = Path(verbosity_path)
        self._send_fn = send_fn  # injected for testing; uses gateway adapter in prod

        self.group_chat_id: str = ""
        self.topics: Dict[str, Dict[str, Any]] = {}
        self._verbosity: Dict[str, Dict[str, str]] = {}
        self._batch_buffer: Dict[str, List[str]] = {}  # topic_key → messages
        # topic_key → wall-clock ISO of the first buffered message. Persisted
        # alongside the buffer so batch AGE survives restarts: seeding the
        # monotonic timestamps at plain now() on every __init__ meant a batch
        # only flushed if one gateway process survived the full 3600s window —
        # under 2026-07-20's ~20-30 min restart churn, messages starved 6.5h
        # undelivered (confirmed live 2026-07-21 00:44Z).
        self._batch_started_at: Dict[str, str] = {}
        saved = load_state(notifier_batch_path(), default={})
        if isinstance(saved.get("buffer"), dict):
            self._batch_buffer = {k: list(v) for k, v in saved["buffer"].items()}
        saved_started = saved.get("started_at")
        if not isinstance(saved_started, dict):
            saved_started = {}  # legacy buffer-only state file (pre-fix shape)
        now_mono = time.monotonic()
        now_wall = datetime.now(timezone.utc)
        # topic_key → first batch time. Monotonic drives in-process aging;
        # each restored key is seeded BACK by its persisted wall-clock age so
        # already-stale batches flush on the first _flush_stale_batches pass.
        self._batch_timestamps: Dict[str, float] = {}
        for k in self._batch_buffer:
            elapsed = 0.0
            iso = saved_started.get(k)
            if iso:
                try:
                    elapsed = max(
                        0.0,
                        (now_wall - datetime.fromisoformat(iso)).total_seconds(),
                    )
                    self._batch_started_at[k] = iso
                except (TypeError, ValueError):
                    pass  # unparseable/naive timestamp → treat as freshly buffered
            if k not in self._batch_started_at:
                self._batch_started_at[k] = now_wall.isoformat()
            self._batch_timestamps[k] = now_mono - elapsed
        # topic_key → monotonic time of the last FAILED flush attempt.
        # In-memory only: a restart grants one fresh attempt, which is the
        # desired behavior (the transport may well be back by then).
        self._batch_retry_at: Dict[str, float] = {}
        self._verbosity_mtime: float = 0.0  # mtime of verbosity.json for hot-reload

        # AGENT_FAILURE_CLUSTER dedup cache (source, 30-min bucket) -> True.
        # In-memory only; a notifier restart clears it (acceptable per the
        # proposal: the Option A producer-side canonicalisation is the
        # primary fix; this LRU is belt-and-braces for timing skew between
        # the two cluster producers within a single notifier lifetime).
        self._cluster_dedup_lru: "OrderedDict[Tuple[str, int], bool]" = OrderedDict()

        # v3 noise guards (P4/P6). In-memory: a restart re-arms them, which
        # at worst re-delivers one repeat/flap announcement per key.
        self._repeat_guard = RepeatGuard()
        # 2h flap window + 2h base mute (escalating to 24h): the WhatsApp
        # bridge flapped for DAYS at ~hourly cadence — a 15-min window
        # never saw 4 transitions, so every flip delivered. 4 flips in 2h
        # of the SAME signal is flapping; a real outage (down, later up)
        # is 2 flips and always delivers.
        self._flap_guard = FlapGuard(
            window_seconds=7200.0, mute_seconds=7200.0)
        # Cron novelty: a job whose (normalized) output hasn't changed in
        # 6h has nothing new to say — "devflow-pr-build-poll: repos=1"
        # every 5 minutes is one fact, not 288 messages. Sliding window:
        # steady identical output stays suppressed until it CHANGES;
        # breakage is covered by cron_failed / cron_stale, not repeats.
        self._cron_novelty_guard = RepeatGuard(window_seconds=6 * 3600.0)

        self._load_config()

    def _load_config(self) -> None:
        """Load topic registry and verbosity config from disk."""
        if self._topics_path.exists():
            data = json.loads(self._topics_path.read_text(encoding="utf-8"))
            self.group_chat_id = data.get("group_chat_id", "")
            self.topics = data.get("topics", {})
        self._reload_verbosity()

    def _reload_verbosity(self) -> None:
        """Hot-reload verbosity.json if it changed on disk (mtime-based)."""
        if not self._verbosity_path.exists():
            return
        try:
            mtime = self._verbosity_path.stat().st_mtime
            if mtime != self._verbosity_mtime:
                self._verbosity = json.loads(self._verbosity_path.read_text(encoding="utf-8"))
                self._verbosity_mtime = mtime
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("TelegramNotifier: failed to reload verbosity.json: %s", e)

    def handle(self, event: Event) -> None:
        # Cycle prevention (2026-04-30): this subscriber EMITS delivery
        # events from _deliver(); consuming them too would recurse. Must
        # short-circuit BEFORE batching so a buffered delivery message
        # doesn't eventually flush and re-trigger the loop.
        if event.event_type in _NEVER_CONSUME:
            return

        # Hot-reload verbosity config on each cycle (spec: hot-reloadable)
        self._reload_verbosity()

        # Synthesized AGENT_ITERATION placeholders (scheduler marker-missing
        # fallback, payload.synthesized=True) are telemetry for the Critic /
        # dashboards, not operator signal — the body is always the same
        # "<agent> completed (synthesized — agent did not emit
        # AGENT_ITERATION_JSON)" line. devflow-bridge alone produced ~918 of
        # these per day into devflow_firehose (2026-07-11 comms audit).
        # Bus-only: skip chat delivery, keep every other consumer unchanged.
        if (event.event_type == EventType.AGENT_ITERATION
                and isinstance(event.payload, dict)
                and event.payload.get("synthesized")):
            return

        # Daily Brief declutter (2026-08-06). DIGEST_GENERATED is generation
        # TELEMETRY, not the narrative: the human digest is delivered directly
        # by DigestComposer._deliver_telegram and by the Scribe cron's
        # NOTIFICATION mailbox_message (body in `summary`). The event carries
        # only metadata (mode/path/body_length/items_surfaced) and has no
        # formatter, so it rendered as a raw key:value dump in scribe_daily.
        # Bus-only: ScribeActionTelemetry still consumes it to open its
        # attribution window; chat never sees it.
        if event.event_type == EventType.DIGEST_GENERATED:
            return

        # Daily Brief declutter (2026-08-06). A raw MAILBOX_MESSAGE is a
        # transport envelope MailboxWatcher mirrors from the filesystem
        # mailbox. MailboxTranslator already converts every actionable
        # envelope into a typed domain event routed to its own topic
        # (SCORE_RESULT->JOB_SCORED->JobFlow, ERROR->AGENT_ERROR->Alerts,
        # PIPELINE_UPDATE->STAGE_TRANSITION, ...), so delivering the raw
        # envelope too duplicated that signal into scribe_daily. The lone
        # user-facing sub-type is NOTIFICATION (the Scribe narrative rides
        # here, and classify() honours an explicit `to:` topic) — keep it,
        # and keep USER_INBOUND_MESSAGE, which is a different event type.
        # Bus-only for the rest: translator/audit/recovery consumers unchanged.
        if (event.event_type == EventType.MAILBOX_MESSAGE
                and isinstance(event.payload, dict)
                and event.payload.get("message_type") != "NOTIFICATION"):
            return

        # Infrastructure noise: lag alerts about the bus itself become a feedback
        # loop (digest -> agent_error -> digest). Suppress them from chat; they
        # remain in the bus for audit-logger and the gateway log.
        if (event.event_type == EventType.AGENT_ERROR
                and event.source == "event-bus"):
            return

        # Receiver-side dedup for AGENT_FAILURE_CLUSTER. Belt-and-braces
        # over the producer-side canonical-source mapping (Option A): if
        # both the cron-emitter and the mailbox-translator paths fire a
        # cluster for the same agent within the same 30-minute window,
        # only the first delivery hits Telegram. The bus event continues
        # flowing so the Critic substrate and the audit logger still see
        # it. See profiles/critic/workspace/watchdog-dedup-proposal-2026-04-29.md.
        if (event.event_type == EventType.AGENT_FAILURE_CLUSTER
                and self._is_duplicate_cluster(event)):
            return

        # FIX 2026-04-25: watchdog feedback flood. The watchdog emits its own
        # signals (probe_transition, silence_alert, tick, agent_failure_cluster)
        # but they fall back to EventType.AGENT_ERROR because no explicit enum
        # member exists. They carry a `watchdog_type` field. They also get
        # detected by the cluster detector as part of its OWN cluster — fixed
        # in watchdog_sweep.py. Telegram-side gate: only HIGH+ watchdog
        # signals come through; routine LOW/NORMAL watchdog noise is bus-only.
        if (event.event_type == EventType.AGENT_ERROR
                and event.source == "watchdog"
                and isinstance(event.payload, dict)
                and event.payload.get("watchdog_type")
                and event.priority.level < Priority.HIGH.level):
            return

        # secret_detected used to be hard-suppressed during the SR-409 scanner
        # explosion (2026-04-19); the scanner is now disabled at the Scheduled
        # Task level and the producer's seen-set is reseeded. Route normally;
        # verbosity gating + per-topic mode in verbosity.json provides the
        # rate-limit hook if volume returns. Re-enabled 2026-04-30.

        if not self.group_chat_id or not self.topics:
            self._load_config()
            if not self.group_chat_id:
                logger.debug("TelegramNotifier: no topics.json configured, skipping")
                return

        route = classify(event, known_topic_keys=self.topics.keys())

        # P4: cron lifecycle noise never becomes a message. A no-op
        # completion ([SILENT]/empty/"no work") is bus-only; a WARN or ACT
        # upgrade from the content sniffers in routing_policy bypasses this
        # (a real error — or a question asked — inside a [SILENT]-prefixed
        # run must deliver).
        if event.event_type in _CRON_BUS_ONLY:
            return
        if is_sustained_resource_repeat(event):
            return
        # P4 for a MONOTONE signal: a job that has failed 4 times in a row
        # is not news 13 minutes after it had failed 3 times. Only ladder
        # rungs (3, 6, 12, 24 ...) reach chat; the rest stay on the bus.
        # This is what makes the producer effectively rising-edge, which is
        # the property the v3 design already assumed it had.
        if is_off_ladder_consecutive_failure(event):
            return
        if event.event_type == EventType.CRON_COMPLETED:
            output = (event.payload or {}).get("output_summary", "")
            if (route.attention not in (Attention.WARN, Attention.ACT)
                    and is_noop_cron_output(output)):
                return
            # Novelty gate (applies to sniffer-upgraded WARN repeats too):
            # same job + same normalized output within 6h = nothing new.
            if self._cron_novelty_guard.is_repeat(
                    f"cron:{event.source}", normalize_for_fingerprint(output)):
                return

        # P6: up/down style signals collapse to state transitions with
        # flap detection (898 alternating bridge-flap messages/week
        # pre-v3; the "watchdog sweep freshness" probe alternated states
        # 68 delivered times — A→B→A evades verbatim-repeat dedup).
        flap_note = None
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.event_type == EventType.GATEWAY_HEALTH:
            decision = self._flap_guard.observe(
                f"gw:{payload.get('platform', '?')}",
                str(payload.get("status", "?")),
            )
            if not decision.deliver:
                return
            flap_note = decision.note
        elif event.event_type == EventType.WATCHDOG_PROBE_TRANSITION:
            decision = self._flap_guard.observe(
                f"probe:{payload.get('probe', '?')}",
                str(payload.get("after", "?")),
            )
            if not decision.deliver:
                return
            flap_note = decision.note

        topic_key, thread_id = resolve_topic_thread(self.topics, route.topic_key)
        if not thread_id:
            logger.warning(
                "TelegramNotifier: no thread for topic %s (event %s) — dropping",
                route.topic_key, event.event_type.type_string,
            )
            return
        chat_id = self.group_chat_id

        if not self._passes_verbosity(topic_key, route, event):
            return

        message = self.format_message(event, route=route)
        if flap_note:
            message = f"{message}\n{flap_note}"

        # P4: repeats collapse (normalized: digits/telemetry ignored).
        # IMMEDIATE-tier ACT (interview/offer/secret/credential) is exempt
        # — those are precious and rare. Everything else, including URGENT
        # ACT, dedups on a 30-min window: the 2026-07-17 capped-partials
        # bug machine-gunned 79 near-identical tracker_partial_backlog
        # pings at the action surface; a broken producer must cost one
        # message per window, not one per fire.
        #
        # Two exceptions, both about signals that ESCALATE rather than flap:
        #   * a cron_failed_consecutive that reached here already passed the
        #     ladder gate above, which IS its rate limit. Running it through
        #     RepeatGuard too would re-collapse the rungs onto one another
        #     (3 and 6 normalize identically) and restore the 2026-08-25
        #     behaviour this ladder exists to remove.
        #   * every other WARN+CRITICAL route dedups on a NON-SLIDING window,
        #     so a fault that persists costs one message per 30 min rather
        #     than one message for the entire outage.
        sustained_critical = (route.attention is Attention.WARN
                              and route.priority is Priority.CRITICAL)
        ladder_rung = event.event_type == EventType.CRON_FAILED_CONSECUTIVE
        if (route.wa_tier != WA_IMMEDIATE
                and not ladder_rung
                and self._repeat_guard.is_repeat(
                    thread_id, message, sliding=not sustained_critical)):
            return

        if route.batch:
            # TRACE batches per thread; flushed hourly or at 20 buffered.
            key = f"{chat_id}:{thread_id}"
            if key not in self._batch_buffer:
                self._batch_buffer[key] = []
                self._batch_timestamps[key] = time.monotonic()
                self._batch_started_at[key] = datetime.now(timezone.utc).isoformat()
            self._batch_buffer[key].append(message)
            if len(self._batch_buffer[key]) >= BATCH_MAX_MESSAGES:
                self._flush_batch_key(key)
            self._persist_batch_buffer()
        else:
            # Model-rate-limit override buttons (2026-08-14 design). Scoped
            # to MODEL_RATE_LIMITED only — buttons_for() already gates on
            # detector/outcome internally, but calling it for every event
            # type on this hot path would be needless work and blur the
            # intent. Lazily imported (matching this module's existing
            # local-import convention) so a broken import in
            # events.override_buttons can't take the whole notifier down at
            # module load. Wrapped so a runtime failure degrades to
            # buttons=None (message still delivers, just without buttons)
            # rather than dropping the alert.
            buttons = None
            if event.event_type == EventType.MODEL_RATE_LIMITED:
                try:
                    from events.override_buttons import buttons_for
                    buttons = buttons_for(event)
                except Exception:
                    logger.exception(
                        "TelegramNotifier: buttons_for() failed for event %s "
                        "— delivering without buttons", event.event_id,
                    )
                    buttons = None
                if buttons:
                    # Record what the opaque callback token actually points
                    # at (Task 7 seam). buttons_for() is deliberately
                    # pure/stateless — see its docstring — so it never
                    # writes this itself; this is the one place downstream
                    # of it that has both the token (embedded identically
                    # in every button on this row) and the event payload
                    # (provider/model/fallback_provider/fallback_model)
                    # needed to populate events.override_callback_state.
                    # plugins/platforms/telegram/adapter.py's callback
                    # handler resolves a tap through THIS map, never by
                    # trusting anything Telegram echoes back in
                    # callback_data — a tap must never be able to name an
                    # arbitrary model. Wrapped so a failure here degrades
                    # to "buttons render but no-op on tap" rather than
                    # dropping the alert.
                    try:
                        from events import override_callback_state
                        token = buttons[0][0]["callback_data"].split(":", 2)[2]
                        payload = getattr(event, "payload", None) or {}
                        override_callback_state.record(
                            token,
                            provider=payload.get("provider", ""),
                            model=payload.get("model", ""),
                            replacement_provider=payload.get("fallback_provider", ""),
                            replacement_model=payload.get("fallback_model", ""),
                        )
                    except Exception:
                        logger.exception(
                            "TelegramNotifier: failed to record override "
                            "callback state for event %s — buttons will "
                            "no-op on tap", event.event_id,
                        )
            # Pass event + topic_key so _deliver can emit the
            # NOTIFICATION_DELIVERED / NOTIFICATION_FAILED reverse
            # signal carrying original_event_id + target metadata.
            # Batched flushes call _deliver() without an event so
            # they DO NOT emit per-event reverse signals (Phase 1
            # scope per the design doc — keeps LOW-priority firehose
            # volume bounded), and correspondingly never attach buttons —
            # a coalesced "Batched (N events)" message has no single event
            # to attach an override to. Spec at
            # docs/superpowers/specs/2026-04-30-notification-delivered-design.md.
            self._deliver(
                chat_id, thread_id, message,
                event=event, topic_key=topic_key, buttons=buttons,
            )

        # Flush any batches older than 5 minutes
        self._flush_stale_batches()

    def _passes_verbosity(self, topic_key: str, route: Route, event: Event) -> bool:
        """v2 verbosity gate: explicit ``min_priority`` per topic plus the
        ``off`` kill-switch. Legacy ``mode`` values (significant_only /
        digest_only) keep their pre-v3 meaning when no min_priority is set,
        so an old verbosity.json stays safe under new code and vice versa.
        Gates on the ROUTE's clamped priority, so class floors (ACT>=HIGH,
        WARN>=NORMAL) are what the threshold sees."""
        cfg = self._verbosity.get(topic_key) or {}
        mode = cfg.get("mode", "all")
        if mode == "off":
            return False
        min_priority = cfg.get("min_priority")
        if min_priority:
            try:
                threshold = Priority.from_string(str(min_priority))
            except Exception:
                threshold = Priority.LOW
            return route.priority.level >= threshold.level
        if mode == "significant_only":
            return route.priority.level >= Priority.HIGH.level
        if mode == "digest_only":
            return (route.priority.level >= Priority.HIGH.level
                    or event.event_type in DIGEST_EVENT_TYPES)
        return True

    def resolve_target(self, event: Event) -> Tuple[str, str, str]:
        """Resolve the single Telegram target for an event. v3: the
        decision is events.routing_policy.classify(); this just maps the
        policy topic_key onto topics.json (alias-aware)."""
        route = classify(event, known_topic_keys=self.topics.keys())
        _key, thread_id = resolve_topic_thread(self.topics, route.topic_key)
        return ("telegram", self.group_chat_id, thread_id)

    def resolve_all_targets(self, event: Event) -> List[Tuple[str, str, str]]:
        """v3: cross-posting removed (P2 — one event, one message). Kept
        for API compatibility; always returns exactly one target."""
        return [self.resolve_target(event)]

    def format_message(self, event: Event, route: Optional[Route] = None) -> str:
        """Format an event into a human-readable Telegram message.

        The priority dot reflects the ROUTE's clamped priority (P5: the
        dot must match the attention class), not the raw event priority.
        """
        from events.formatting import format_event_message

        if route is None:
            route = classify(event, known_topic_keys=self.topics.keys())
        if route.priority is not event.priority:
            event = dataclasses.replace(event, priority=route.priority)
        body = self._format_payload(event)
        return format_event_message(event, body, verdict=route.verdict)

    def _format_payload(self, event: Event) -> str:
        """Format event payload into readable text."""
        p = event.payload
        et = event.event_type

        if et == EventType.CRON_COMPLETED:
            summary = self._trim_cron_summary(p.get("output_summary", ""))
            duration = p.get("duration", "?")
            return f"Duration: {duration}s\n{summary}" if summary else f"Duration: {duration}s"

        if et == EventType.CRON_FAILED:
            return f"Error: {p.get('error', 'Unknown')}\nConsecutive failures: {p.get('consecutive_errors', 0)}"

        if et == EventType.CRON_STALE:
            # Four meanings on one event type, separated only by `scope`.
            # The generic fallback buried it mid-list and splatted the
            # correlation UUIDs — cf. the SECRET_DETECTED branch below.
            from events.formatting import cron_stale_body
            return cron_stale_body(p)

        if et in (EventType.CRON_PAUSED, EventType.CRON_RESUMED):
            # The generic fallback splatted the full `reason` field — for a
            # resume that is the multi-KB pause reason being LIFTED, and it
            # read as if the hold were still in force (2026-08-31, the
            # tracker-identity-repair resume). Lead with what happened; keep
            # the reason to one clamped line with the right tense, and point
            # at `hermes cron show` for the full text.
            verb = "paused" if et == EventType.CRON_PAUSED else "resumed"
            lines = [f"{p.get('job_name', '?')} {verb} (caller: {p.get('caller', '?')})"]
            if et == EventType.CRON_RESUMED and p.get("next_run_at"):
                lines.append(f"Next run: {p.get('next_run_at')}")
            reason = str(p.get("reason") or "").strip()
            if reason:
                label = "Reason" if et == EventType.CRON_PAUSED else "Lifted pause reason (no longer in force)"
                clamped = reason[:300] + ("…" if len(reason) > 300 else "")
                lines.append(f"{label}: {clamped}")
                if len(reason) > 300:
                    lines.append(f"Full text: hermes cron show {p.get('job_id', '?')}")
            return "\n".join(lines)

        if et == EventType.JOB_DISCOVERED:
            return f"Title: {p.get('title', '?')}\nCompany: {p.get('company', '?')}\nSource: {p.get('source', '?')}"

        if et in (EventType.JOB_SCORED, EventType.JOB_HIGH_SCORE):
            return f"Score: {p.get('score', '?')}\nTitle: {p.get('title', '?')}\nCompany: {p.get('company', '?')}"

        if et == EventType.APPLICATION_FAILED:
            return f"Error: {p.get('error', 'Unknown')}\nCompany: {p.get('company', '?')}"

        if et == EventType.APPLICATION_BLOCKED:
            # Had no branch of its own: the generic fallback below printed the
            # whole envelope as `key: value` lines, which for a list-valued
            # `options` is a Python repr of the very labels the answer must be
            # copied from verbatim. Lead with the question, then the choices as
            # a numbered list (events.formatting owns the list rendering so the
            # WhatsApp page and this cannot drift).
            #
            # 2026-08-23: opens with an explicit action line. 132 of these had
            # delivered to jobflow_firehose reading like pipeline progress —
            # Diego's complaint that "jobs are being input in ATS but nothing
            # shows in Applying" traced to exactly this ambiguity: a block is
            # not a submission attempt in flight, it is a question only a
            # human can answer.
            from events.formatting import (
                blocked_question_line,
                blocked_question_options_block,
            )
            lines = [
                "Action needed: answer this question to unblock the application.",
                "",
                f"Company: {p.get('company', '?')}",
                f"Title: {p.get('title', '?')}",
                f"Question: {blocked_question_line(p)}",
            ]
            options = blocked_question_options_block(p)
            if options:
                lines.append("")
                lines.append(options)
            return "\n".join(lines)

        if et == EventType.GATEWAY_HEALTH:
            # Lead with the plain-language diagnosis; keep the raw error
            # below it — Telegram is the diagnostic surface.
            from events.formatting import humanize_health_detail
            detail = p.get("detail", "")
            reason = humanize_health_detail(detail)
            lines = [f"Platform: {p.get('platform', '?')} → {p.get('status', '?')}"]
            if reason:
                lines.append(reason)
            if detail and detail != reason:
                lines.append(f"raw: {detail}")
            return "\n".join(lines)

        # Watchdog signals share plain-language bodies with the WhatsApp
        # escalator (2026-07-11) — the generic fallback used to splat the
        # raw `transitions` list of dicts into the topic.
        if et == EventType.WATCHDOG_BURST:
            from events.formatting import watchdog_burst_body
            return watchdog_burst_body(p, max_listed=15, aggregate_optional=False)

        if et == EventType.WATCHDOG_PROBE_TRANSITION:
            from events.formatting import probe_transition_body
            return probe_transition_body(p)

        if et == EventType.WATCHDOG_SILENCE_ALERT:
            from events.formatting import silence_alert_body
            return silence_alert_body(p)

        if et == EventType.WATCHDOG_SELF_DEGRADED:
            from events.formatting import watchdog_self_degraded_body
            return watchdog_self_degraded_body(p)

        if et == EventType.CONTAINER_CRASH_LOOP:
            from events.formatting import container_crash_loop_body
            return container_crash_loop_body(p)

        if et == EventType.AGENT_FAILURE_CLUSTER:
            from events.formatting import failure_cluster_body
            return failure_cluster_body(p)

        if et == EventType.RESOURCE_PRESSURE:
            # 2026-06-11 pagefile-burst remediation; shared with the WhatsApp
            # escalator since 2026-08-14 so both lanes render the severity band
            # identically (the band is the only part the repeat guard can see).
            from events.formatting import resource_pressure_body
            return resource_pressure_body(p)

        if et == EventType.TRACKER_PARTIAL_BACKLOG:
            # 2026-07-18 operator feedback: the generic fallback splatted
            # count/threshold/oldest_age_seconds/capped_count/sample_job_ids as
            # raw key:value lines — cryptic. Render the plain-language triage.
            from events.formatting import partial_backlog_body
            return partial_backlog_body(p)

        if et == EventType.CODE_DRIFT:
            # 2026-07-21: the generic fallback would splat missed_subjects
            # as a raw list — render the plain-language diagnosis instead.
            from events.formatting import code_drift_body
            return code_drift_body(p)

        if et == EventType.BOOT_SUMMARY:
            # 2026-07-27: failures/anomalies are lists, so the generic
            # fallback below would render Python list reprs into the topic.
            from events.formatting import boot_summary_body
            return boot_summary_body(p)

        if et == EventType.AGENT_NOTE:
            # 2026-08-19: the only type whose body is the caller's own prose.
            # Renders headline + detail VERBATIM — the same treatment
            # AGENT_ITERATION gives a structured `brief` below. Every other
            # branch in this method reads type-specific fields and would
            # discard free text; that discard is what silently collapsed two
            # distinct notes onto one RepeatGuard fingerprint.
            from events.formatting import agent_note_body
            return agent_note_body(p)

        if et == EventType.MAILBOX_MESSAGE:
            return f"{p.get('from', '?')} → {p.get('to', '?')}: {p.get('message_type', '?')}\n{p.get('summary', '')}"

        if et == EventType.STAGE_TRANSITION:
            # Dashboard ↔ comms wiring (2026-04-30 Phase 4). Render
            # transitions readable: "<title> at <company> → new_stage
            # (by actor via source)". Backwards-compatible: payload may
            # come from cron/legacy producers without metadata.
            title = p.get("title") or p.get("job_title") or ""
            company = p.get("company") or ""
            prior = p.get("prior_stage") or "?"
            new = p.get("new_stage") or p.get("stage") or "?"
            actor = p.get("actor") or "?"
            src = p.get("source_surface") or p.get("source") or ""
            head_parts = []
            if title:
                head_parts.append(title)
            if company:
                head_parts.append(f"at {company}")
            head = " ".join(head_parts) or p.get("job_id") or "?"
            via = f" (by {actor}" + (f" via {src})" if src else ")")
            return f"{head}\n{prior} → {new}{via}"

        if et == EventType.AGENT_ITERATION:
            # Per-agent run summary (2026-04-30). When the agent supplies a
            # structured multi-line ``brief`` (2026-08-06 Critic messaging),
            # render it verbatim and DO NOT prefix with the agent name or
            # append the compact ``k=v · k=v`` counter line — the brief is the
            # human-readable surface and counters remain on the event payload.
            anomalies = p.get("anomalies") or []
            brief = p.get("brief")
            if isinstance(brief, str) and brief.strip():
                lines = [brief.strip()]
            else:
                # Legacy path: agent name + summary, then compact counters.
                agent = (p.get("agent") or "?").strip()
                summary = (p.get("summary") or "").strip()
                counters = p.get("counters") or {}
                lines = [f"{agent}: {summary}" if summary else f"{agent}: (no summary)"]
                if isinstance(counters, dict) and counters:
                    compact = " · ".join(f"{k}={v}" for k, v in counters.items())
                    lines.append(compact)
            if isinstance(anomalies, list) and anomalies:
                # Anomalies are short (we expect 0-3). Take first 3 to
                # keep the message bounded.
                anom_strs = []
                for a in anomalies[:3]:
                    if isinstance(a, dict):
                        anom_strs.append(a.get("kind") or a.get("note") or str(a)[:40])
                    else:
                        anom_strs.append(str(a)[:40])
                lines.append("⚠ " + " · ".join(anom_strs))
            return "\n".join(lines)

        if et == EventType.SECRET_DETECTED:
            # SR-408 post-flood fix (2026-04-19). Before this branch the
            # generic fallback dumped all 6 payload fields — including the
            # multi-kilobyte `match_preview` asterisk walls from LevelDB
            # binary chunks and internal `finding_hash` / `gitleaks_version`
            # operators cannot act on. Keep the body to the 3 operator-
            # actionable fields: rule, location, masked preview. The
            # finding_hash still flows to the event bus as correlation_id
            # (see scanner.py::_publish_to_hermes) — not lost, just out of
            # the user's face.
            return (
                f"Rule: {p.get('rule_id', '?')}\n"
                f"File: {p.get('file_path', '?')}:{p.get('line_no', '?')}\n"
                f"Preview: {p.get('match_preview', '?')}"
            )

        if et == EventType.CREDENTIAL_LOSS:
            # Named credential/infra loss from the watchdog sweep (2026-07-10).
            # Lead with the probe + its state edge, then the actionable detail;
            # the generic fallback would splat watchdog_type/tier/category noise.
            return (
                f"{p.get('probe', '?')}: {p.get('before', '?')} → {p.get('after', '?')}\n"
                f"{p.get('detail', '')}"
            ).strip()

        # Generic fallback
        lines = [f"{k}: {v}" for k, v in p.items() if v]
        return "\n".join(lines[:10])

    def _trim_cron_summary(self, summary: str) -> str:
        """Keep Mission Control cron updates readable inside a chat topic."""
        # Machine-telemetry blocks are for the Critic, never for humans.
        summary = strip_agent_iteration_json(str(summary or "")).strip()
        if not summary:
            return ""

        lines = summary.splitlines()
        if len(lines) <= CRON_SUMMARY_MAX_LINES and len(summary) <= CRON_SUMMARY_MAX_CHARS:
            return summary

        budget = max(80, CRON_SUMMARY_MAX_CHARS - len(CRON_SUMMARY_TRUNCATION_NOTE) - 2)
        kept: List[str] = []
        used = 0

        for line in lines:
            normalized = line.rstrip()
            addition = normalized if not kept else f"\n{normalized}"
            if len(kept) >= CRON_SUMMARY_MAX_LINES or used + len(addition) > budget:
                break
            kept.append(normalized)
            used += len(addition)

        if not kept:
            clipped = summary[: max(0, budget - 3)].rstrip()
            if len(clipped) < len(summary):
                clipped += "..."
            kept_text = clipped
        else:
            kept_text = "\n".join(kept).rstrip()

        return f"{kept_text}\n\n{CRON_SUMMARY_TRUNCATION_NOTE}"

    def _deliver(
        self,
        chat_id: str,
        thread_id: str,
        message: str,
        *,
        event: Optional[Event] = None,
        topic_key: Optional[str] = None,
        batch_count: Optional[int] = None,
        buttons: Optional[List[List[Dict[str, str]]]] = None,
    ) -> bool:
        """Send a message to a Telegram chat/thread. Returns True when
        the send succeeded, False when it raised (exceptions are swallowed
        here; callers that must not lose the message — the batch flush —
        requeue on False, mirroring WhatsAppEscalator's quiet-queue requeue).

        When ``event`` is provided (non-batched delivery from handle()),
        emits NOTIFICATION_DELIVERED on success and NOTIFICATION_FAILED
        on exception, carrying original_event_id + target metadata.
        Both reverse-signal emits are wrapped in helpers that swallow
        their own exceptions, so a bus-side failure (transient SQLite
        lock, schema mismatch) NEVER breaks the upstream delivery path.

        Batched flushes (_flush_batch_key) call this without an event
        so per-event reverse signals don't double the LOW-priority
        firehose volume — Phase 1 scoping per the design doc at
        docs/superpowers/specs/2026-04-30-notification-delivered-design.md.
        They pass ``batch_count`` instead, and a successful flush emits
        ONE synthetic NOTIFICATION_DELIVERED with
        original_event_type="batch_flush" (routing-v3 observability gap,
        2026-07-20: without it a "Batched (N events)" chat message left
        zero ledger rows, so per-topic delivery audits undercounted).

        ``buttons``, when provided, is the serializable spec from
        ``events.override_buttons.buttons_for`` and is forwarded to
        ``_deliver_result`` (the production path here, since ``_send_fn``
        is unset outside tests). It is never passed to ``_send_fn``, so a
        test double's call signature is unaffected. Defaults to ``None``,
        in which case this method's behavior is unchanged from before this
        parameter existed.
        """
        # Formatter observability (2026-08-19). The rendered message
        # existed NOWHERE on disk: audit.jsonl carries the event PAYLOAD,
        # RepeatGuard keeps only a sha of the normalized text and dies with
        # the process, no message_id is persisted, and TelegramMirror was
        # retired 2026-04-28. So the UNKNOWN AGENT_NOTE header (fixed in
        # bdc736bd37) rendered wrong on EVERY delivery while 759 tests, an
        # 83/83 coverage gate and three clean delivery receipts all passed.
        # This is the single choke point for both the _send_fn (test) and
        # _deliver_result (production) paths.
        #
        # HEADER LINE ONLY, deliberately. The header carries everything the
        # formatter decides -- priority dot, verdict label, icon, type,
        # source, timestamp -- which is the whole surface this exists to
        # watch. The body is CALLER CONTENT and may be sensitive, so it
        # must not be written to a log file. body_chars is a length, never
        # content, and is enough to catch truncation. Do not widen this
        # back to the full message.
        header = message.splitlines()[0] if message else ""
        logger.info(
            "TelegramNotifier sending to %s/%s: %r (+%d body chars)",
            chat_id, thread_id, header, len(message) - len(header),
        )
        t0 = time.monotonic()
        try:
            if self._send_fn:
                self._send_fn(chat_id, thread_id, message)
            else:
                # Production: use gateway delivery
                from cron.scheduler import _deliver_result
                target_str = (
                    f"telegram:{chat_id}:{thread_id}" if thread_id
                    else f"telegram:{chat_id}"
                )
                _deliver_result(
                    {"deliver": target_str, "id": "event-bus", "name": "event-bus"},
                    message,
                    skip_cron_framing=True,
                    buttons=buttons,
                )
            latency_ms = int((time.monotonic() - t0) * 1000)
            if event is not None:
                self._safe_emit_delivered(
                    event, chat_id, thread_id, topic_key, latency_ms,
                )
            elif batch_count is not None:
                self._safe_emit_batch_delivered(
                    chat_id, thread_id, topic_key, latency_ms, batch_count,
                )
            return True
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.error("TelegramNotifier delivery failed: %s", exc)
            if event is not None:
                self._safe_emit_failed(
                    event, chat_id, thread_id, topic_key, latency_ms, exc,
                )
            elif batch_count is not None:
                self._safe_emit_batch_failed(
                    chat_id, thread_id, topic_key, latency_ms, batch_count, exc,
                )
            return False

    def _safe_emit_delivered(
        self,
        event: Event,
        chat_id: str,
        thread_id: str,
        topic_key: Optional[str],
        latency_ms: int,
    ) -> None:
        """Emit NOTIFICATION_DELIVERED. Swallows all exceptions — a
        bus failure here MUST NOT break the upstream delivery path."""
        try:
            self.bus.emit(
                event_type=EventType.NOTIFICATION_DELIVERED,
                source="telegram-notifier",
                payload={
                    "original_event_id": event.event_id,
                    "original_event_type": event.event_type.type_string,
                    "platform": "telegram",
                    "target": {
                        "chat_id": chat_id,
                        "thread_id": thread_id,
                        "topic_key": topic_key or "",
                    },
                    "latency_ms": latency_ms,
                },
                priority=Priority.LOW,
                correlation_id=event.event_id,
                tags=["delivery", "telegram"],
            )
        except Exception:
            logger.exception(
                "TelegramNotifier: failed to emit NOTIFICATION_DELIVERED "
                "for event %s", event.event_id,
            )

    def _safe_emit_batch_delivered(
        self,
        chat_id: str,
        thread_id: str,
        topic_key: Optional[str],
        latency_ms: int,
        batch_count: int,
    ) -> None:
        """Emit ONE synthetic NOTIFICATION_DELIVERED for a successful
        batch flush (original_event_type="batch_flush", no
        original_event_id — the send aggregates many events). Same
        swallow contract as _safe_emit_delivered. Loop-safe: both this
        subscriber and WhatsAppEscalator skip NOTIFICATION_DELIVERED via
        their _NEVER_CONSUME early-return, so the synthetic event is
        ledger-only."""
        try:
            self.bus.emit(
                event_type=EventType.NOTIFICATION_DELIVERED,
                source="telegram-notifier",
                payload={
                    "original_event_type": "batch_flush",
                    "batch_count": batch_count,
                    "platform": "telegram",
                    "target": {
                        "chat_id": chat_id,
                        "thread_id": thread_id,
                        "topic_key": topic_key or "",
                    },
                    "latency_ms": latency_ms,
                },
                priority=Priority.LOW,
                tags=["delivery", "telegram", "batch"],
            )
        except Exception:
            logger.exception(
                "TelegramNotifier: failed to emit batch-flush "
                "NOTIFICATION_DELIVERED for %s:%s", chat_id, thread_id,
            )

    def _safe_emit_failed(
        self,
        event: Event,
        chat_id: str,
        thread_id: str,
        topic_key: Optional[str],
        latency_ms: int,
        exc: Exception,
    ) -> None:
        """Emit NOTIFICATION_FAILED. Same swallow contract as
        _safe_emit_delivered."""
        try:
            self.bus.emit(
                event_type=EventType.NOTIFICATION_FAILED,
                source="telegram-notifier",
                payload={
                    "original_event_id": event.event_id,
                    "original_event_type": event.event_type.type_string,
                    "platform": "telegram",
                    "target": {
                        "chat_id": chat_id,
                        "thread_id": thread_id,
                        "topic_key": topic_key or "",
                    },
                    "latency_ms": latency_ms,
                    "error": {
                        "kind": type(exc).__name__,
                        # Cap so a multi-KB stacktrace doesn't bloat the
                        # bus DB. The full exception stays in the gateway
                        # log via logger.error above.
                        "message": str(exc)[:500],
                    },
                },
                priority=Priority.NORMAL,
                correlation_id=event.event_id,
                tags=["delivery", "telegram", "failure"],
            )
        except Exception:
            logger.exception(
                "TelegramNotifier: failed to emit NOTIFICATION_FAILED "
                "for event %s", event.event_id,
            )

    def _safe_emit_batch_failed(
        self,
        chat_id: str,
        thread_id: str,
        topic_key: Optional[str],
        latency_ms: int,
        batch_count: int,
        exc: Exception,
    ) -> None:
        """Emit ONE synthetic NOTIFICATION_FAILED for a failed batch flush
        — the failure twin of _safe_emit_batch_delivered (same
        original_event_type="batch_flush" payload shape, no
        original_event_id). Before this (2026-07-20/21 live loss) a flush
        into a dead transport left ZERO ledger rows, indistinguishable
        from a quiet hour. Same swallow contract; loop-safe via
        _NEVER_CONSUME."""
        try:
            self.bus.emit(
                event_type=EventType.NOTIFICATION_FAILED,
                source="telegram-notifier",
                payload={
                    "original_event_type": "batch_flush",
                    "batch_count": batch_count,
                    "platform": "telegram",
                    "target": {
                        "chat_id": chat_id,
                        "thread_id": thread_id,
                        "topic_key": topic_key or "",
                    },
                    "latency_ms": latency_ms,
                    "error": {
                        "kind": type(exc).__name__,
                        # Cap so a multi-KB stacktrace doesn't bloat the
                        # bus DB. The full exception stays in the gateway
                        # log via logger.error in _deliver.
                        "message": str(exc)[:500],
                    },
                },
                priority=Priority.NORMAL,
                tags=["delivery", "telegram", "batch", "failure"],
            )
        except Exception:
            logger.exception(
                "TelegramNotifier: failed to emit batch-flush "
                "NOTIFICATION_FAILED for %s:%s", chat_id, thread_id,
            )

    def _flush_stale_batches(self, max_age: float = BATCH_FLUSH_SECONDS) -> None:
        """Flush batched TRACE messages older than max_age seconds."""
        now = time.monotonic()
        keys_to_flush = [
            k for k, ts in self._batch_timestamps.items()
            if now - ts >= max_age
        ]
        for key in keys_to_flush:
            self._flush_batch_key(key)
        if keys_to_flush:
            self._persist_batch_buffer()

    def _flush_batch_key(self, key: str) -> None:
        """Deliver and clear one thread's batch buffer.

        Failed-send requeue (2026-07-21): the messages are popped BEFORE
        the send, and _deliver() swallows exceptions — pre-fix, a flush
        into a dead transport silently LOST the whole batch (live
        2026-07-20/21: Telegram stuck in an httpx.ReadError reconnect
        loop; the batch buffered since 18:07Z left zero chat messages and
        zero ledger rows). On failure the messages go back to the buffer
        (front, order preserved, capped oldest-out at BATCH_MAX_MESSAGES)
        with the key's original age, the restored state is persisted, and
        the key backs off BATCH_RETRY_BACKOFF_SECONDS before the next
        attempt. The ledger contract stands: NOTIFICATION_DELIVERED only
        on success; failure emits the batch_flush NOTIFICATION_FAILED
        twin (via _deliver).
        """
        last_failed = self._batch_retry_at.get(key)
        if (last_failed is not None
                and time.monotonic() - last_failed < BATCH_RETRY_BACKOFF_SECONDS):
            # Recent failed attempt: don't hammer a dead transport once
            # per handled event; keep the still-growing buffer bounded
            # while we wait the backoff out.
            self._cap_batch_key(key)
            return
        messages = self._batch_buffer.pop(key, [])
        prior_ts = self._batch_timestamps.pop(key, None)
        prior_started_at = self._batch_started_at.pop(key, None)
        if not messages:
            return
        parts = key.split(":", 1)
        chat_id, thread_id = parts[0], parts[1] if len(parts) > 1 else ""
        combined = f"Batched ({len(messages)} events):\n\n" + "\n---\n".join(messages)
        if self._deliver(
            chat_id, thread_id, combined,
            topic_key=self._thread_id_to_key(thread_id),
            batch_count=len(messages),
        ):
            self._batch_retry_at.pop(key, None)
            return
        # Restore to the FRONT (nothing can have appended mid-flush —
        # handle() is synchronous — but stay order-safe regardless) and
        # keep the key's original age so the retry isn't treated as a
        # fresh batch.
        self._batch_buffer[key] = messages + self._batch_buffer.get(key, [])
        self._batch_timestamps[key] = (
            prior_ts if prior_ts is not None else time.monotonic()
        )
        self._batch_started_at[key] = (
            prior_started_at or datetime.now(timezone.utc).isoformat()
        )
        self._cap_batch_key(key)
        self._batch_retry_at[key] = time.monotonic()
        self._persist_batch_buffer()

    def _cap_batch_key(self, key: str) -> None:
        """Drop oldest messages beyond BATCH_MAX_MESSAGES for one key —
        bounds the buffer while failed flushes are being backed off."""
        buf = self._batch_buffer.get(key)
        if not buf or len(buf) <= BATCH_MAX_MESSAGES:
            return
        dropped = len(buf) - BATCH_MAX_MESSAGES
        del buf[:dropped]
        logger.warning(
            "TelegramNotifier: batch %s over cap while sends fail — "
            "dropped %d oldest message(s)", key, dropped,
        )

    def _persist_batch_buffer(self) -> None:
        """Write current batch state to disk so it survives restart."""
        try:
            save_state(notifier_batch_path(), {
                "buffer": {k: list(v) for k, v in self._batch_buffer.items()},
                # Wall-clock first-buffered stamps: what lets the NEXT process
                # resume aging instead of restarting the 3600s window. Only
                # keys still buffered are persisted (flushes pop both dicts).
                "started_at": {
                    k: v for k, v in self._batch_started_at.items()
                    if k in self._batch_buffer
                },
            })
        except Exception:
            logger.exception("TelegramNotifier: failed to persist batch buffer")

    def shutdown(self) -> None:
        """Flush all pending batches on shutdown."""
        self._flush_stale_batches(max_age=0)

    def _thread_id_to_key(self, thread_id: str) -> str:
        """Reverse lookup: thread_id → topic key."""
        for key, topic in self.topics.items():
            if str(topic.get("thread_id", "")) == thread_id:
                return key
        return "system"

    def _is_duplicate_cluster(self, event: Event) -> bool:
        """Return True iff this AGENT_FAILURE_CLUSTER event has already
        been delivered for the same (source, 30-min bucket) within the
        notifier's LRU window. Records the key on miss before returning
        False, so subsequent calls with the same key suppress.

        Bucket: integer floor of event.timestamp / CLUSTER_DEDUP_BUCKET_SECONDS.
        Falls back to wall clock if event.timestamp is unparseable so the
        dedup never silently passes through on bad input.
        """
        source = event.source or "unknown"
        try:
            ts = datetime.fromisoformat(event.timestamp).timestamp()
        except (TypeError, ValueError):
            ts = time.time()
        bucket = int(ts // CLUSTER_DEDUP_BUCKET_SECONDS)
        key = (source, bucket)

        if key in self._cluster_dedup_lru:
            self._cluster_dedup_lru.move_to_end(key)
            return True

        self._cluster_dedup_lru[key] = True
        while len(self._cluster_dedup_lru) > CLUSTER_DEDUP_LRU_SIZE:
            self._cluster_dedup_lru.popitem(last=False)
        return False
