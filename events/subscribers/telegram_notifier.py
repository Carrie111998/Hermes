"""TelegramNotifier subscriber — routes events to Telegram forum topics.

Reads topic registry from ~/.hermes/telegram/topics.json and verbosity
config from ~/.hermes/telegram/verbosity.json.  Delivers messages via
the gateway's Telegram adapter send() method.
"""

import json
import logging
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from events.bus import EventBus
from events.paths import notifier_batch_path
from events.schema import Event, EventType, Priority
from events.state import load_state, save_state
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

# Maps event_type string → topic key.
# The 7 high-stakes trigger event types (job_high_score, application_submitted,
# application_ready, interview_signal, offer_signal, critic_proposal,
# curator_daily) are routed by BOTH paths and that is intentional:
#   1. scribe-realtime renders each into a one-liner and emits a
#      mailbox_message NOTIFICATION with `to:` set to a Scribe-narrative
#      topic (e.g. hermes_milestones for interview/offer). resolve_target()
#      honors the explicit `to:` field below.
#   2. The original typed event is also routed via this TOPIC_ROUTING table
#      to a structured-decisions topic (e.g. jobflow_decisions) so the
#      action surface remains queryable independently of Scribe narration.
# This dual path was previously claimed to be "deliberately absent" in
# 2026-04-28 spec (2026-04-28-scribe-realtime-narration-design.md) but the
# implementation kept both — verified 2026-04-30. CROSS_POST_TO_ALERTS still
# fires for high-priority types (preserved).
TOPIC_ROUTING: Dict[str, str] = {
    # === Hermes Telegram v2 (cutover 20260424T233627Z) ===
    # -> jobflow_firehose
    'job_discovered': 'jobflow_firehose',
    'job_vip_discovered': 'jobflow_firehose',
    'job_scored': 'jobflow_firehose',
    'tailor_completed': 'jobflow_firehose',
    'tailor_iteration': 'jobflow_firehose',
    'application_submitted': 'jobflow_firehose',
    'stage_transition': 'jobflow_firehose',
    'followup_due': 'jobflow_firehose',
    # -> jobflow_decisions (human-action signals; per test_critical_events_route_to_alerts)
    'approval_request': 'jobflow_decisions',
    'apply_packet': 'jobflow_decisions',
    'application_ready': 'jobflow_decisions',
    'interview_signal': 'jobflow_decisions',
    'offer_signal': 'jobflow_decisions',
    # Mirror scribe_realtime.py:34-38 so narrated and structured copies
    # land on the same topic; CROSS_POST_TO_ALERTS still cross-posts at HIGH+.
    'job_high_score': 'jobflow_decisions',
    # -> devflow_firehose
    'run_started': 'devflow_firehose',
    'run_completed': 'devflow_firehose',
    'trace_snapshot': 'devflow_firehose',
    'devflow.run_started': 'devflow_firehose',
    'devflow.run_completed': 'devflow_firehose',
    'devflow.trace_snapshot': 'devflow_firehose',
    # PR + build telemetry -- added 2026-04-30 so SDLC activity surfaces
    # in the devflow_firehose stream alongside the run.* lifecycle events.
    # Spec: docs/superpowers/specs/2026-04-30-devflow-pr-build-events.md.
    'devflow.pr_opened': 'devflow_firehose',
    'devflow.pr_merged': 'devflow_firehose',
    'devflow.pr_closed': 'devflow_firehose',
    'devflow.build_started': 'devflow_firehose',
    'devflow.build_succeeded': 'devflow_firehose',
    # -> devflow_decisions
    'approval_requested': 'devflow_decisions',
    'devflow.approval_requested': 'devflow_decisions',
    # PR ready-for-review and build failures are decisions (someone needs
    # to act). Routes to devflow_decisions, NOT cross-posted to
    # watchdog_alerts (DevFlow has its own decision topic and we don't
    # want every red CI leaking into the operator alert stream).
    'devflow.pr_review_requested': 'devflow_decisions',
    'devflow.build_failed': 'devflow_decisions',
    # -> watchdog_alerts
    'gateway_health': 'watchdog_alerts',
    'agent_error': 'watchdog_alerts',
    'cron_started': 'watchdog_alerts',
    'cron_triggered': 'watchdog_alerts',
    'cron_completed': 'watchdog_alerts',
    'cron_failed': 'watchdog_alerts',
    'cron_failed_consecutive': 'watchdog_alerts',
    'cron_stale': 'watchdog_alerts',
    # cron_skipped_duplicate: same-job concurrency guard, low-priority
    # informational telemetry (LOW priority => batched / digest_only-gated
    # alongside cron_started). Routed to watchdog_alerts so a sudden burst
    # of duplicate-skips on one job is visible to the operator without
    # needing a dedicated topic. Added 2026-04-30 with EventType.
    'cron_skipped_duplicate': 'watchdog_alerts',
    'application_blocked': 'watchdog_alerts',
    'application_failed': 'watchdog_alerts',
    # iter5: proper watchdog event types (replacing AGENT_ERROR fallback)
    'watchdog_tick': 'watchdog_alerts',
    'watchdog_probe_transition': 'watchdog_alerts',
    'watchdog_silence_alert': 'watchdog_alerts',
    'watchdog_recovered': 'watchdog_alerts',
    'watchdog_burst': 'watchdog_alerts',
    'watchdog_self_degraded': 'watchdog_alerts',
    # Once-per-day aggregate health heartbeat (2026-04-30) — see
    # EventType.WATCHDOG_DAILY docstring for the visibility-restoration
    # backstory. Routes alongside the other watchdog signals so a topic
    # cutover only has to update one block.
    'watchdog_daily': 'watchdog_alerts',
    'agent_failure_cluster': 'watchdog_alerts',
    # Notification delivery reverse-signal (2026-04-30). These entries
    # exist so test_all_event_types_have_routing covers them, but the
    # primary defense is the cycle guard in handle() — both delivery
    # subscribers early-return for these types so they NEVER actually
    # route to a chat. If the guard is ever removed by mistake, the
    # routing here lands them in watchdog_alerts (sensible operator
    # signal for delivery failures); LOW-priority NOTIFICATION_DELIVERED
    # would be batched/filtered by verbosity. Spec at
    # docs/superpowers/specs/2026-04-30-notification-delivered-design.md.
    'notification_delivered': 'watchdog_alerts',
    'notification_failed': 'watchdog_alerts',
    # -> critic_proposals
    'critic_proposal': 'critic_proposals',
    'critic_auto_applied': 'critic_proposals',
    'critic_self_degraded': 'critic_proposals',
    # NOTE: 'agent_failure_cluster' is the Critic's TRIGGER, not its proposal.
    # It routes to watchdog_alerts (line ~60, with the other watchdog signals).
    # The Critic still consumes it via the bus and emits critic_proposal events
    # which DO route here.
    # -> curator_digest
    'memory_consolidated': 'curator_digest',
    'skill_evolved': 'curator_digest',
    'curator_daily': 'curator_digest',
    # -> scribe_daily
    # digest_generated is an observability event (Watchdog/Critic cadence
    # tracking) — NOT delivered to Telegram in normal flow. The actual digest
    # content arrives via the special-case mailbox_message + NOTIFICATION
    # path below (search "message_type") so Diego sees one digest per fire,
    # not two. Listed here for test coverage (test_all_event_types_have_routing)
    # but delivery is gated separately.
    'digest_generated': 'scribe_daily',
    'scribe_digest': 'scribe_daily',
    'mailbox_message': 'scribe_daily',
    'user_inbound_message': 'scribe_daily',
    # -> security_and_system
    'secret_detected': 'security_and_system',
    # agent_iteration: TOPIC_ROUTING entry is the FALLBACK only — the
    # actual primary route is per-agent via resolve_target() reading
    # payload.agent against AGENT_TOPIC_MAP below. resolve_target() short-
    # circuits before this lookup runs, so this entry is hit only when
    # the AGENT_ITERATION code path is bypassed (e.g. in test_all_event_
    # types_have_routing coverage). Default keeps agent activity in the
    # firehose where it does the least harm if AGENT_TOPIC_MAP misses.
    'agent_iteration': 'jobflow_firehose',
}

# Per-agent routing for AGENT_ITERATION events — added 2026-04-30 to
# distribute generic iteration summaries to the right Telegram topic
# without needing one EventType per agent. Lookup uses the canonical
# agent name from event.payload.agent (lowercase, hyphens only). Unknown
# agents fall back to the default topic.
AGENT_TOPIC_MAP: Dict[str, str] = {
    # JobFlow agents
    'scout': 'jobflow_firehose',
    'matcher': 'jobflow_firehose',
    'matcher-shadow': 'jobflow_firehose',
    'tailor': 'jobflow_firehose',
    'applier': 'jobflow_firehose',
    'tracker': 'jobflow_firehose',
    'sentinel': 'jobflow_firehose',
    'cv-handler': 'jobflow_firehose',
    'notifier': 'jobflow_firehose',
    'main': 'jobflow_firehose',
    # DevFlow agents
    'devflow': 'devflow_firehose',
    'devflow-standup': 'devflow_firehose',
    'devflow-bridge': 'devflow_firehose',
    # Platform / learning agents
    'critic': 'critic_proposals',
    'curator': 'curator_digest',
    'watchdog': 'watchdog_alerts',
    'scribe': 'scribe_daily',
}
AGENT_ITERATION_DEFAULT_TOPIC = 'jobflow_firehose'

# Events that cross-post to alerts when high/critical
CROSS_POST_TO_ALERTS = {
    'application_ready',
    'followup_due',
    'interview_signal',
    'job_high_score',
    'offer_signal',
}

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
        saved = load_state(notifier_batch_path(), default={})
        if isinstance(saved.get("buffer"), dict):
            self._batch_buffer = {k: list(v) for k, v in saved["buffer"].items()}
        import time
        now = time.monotonic()
        self._batch_timestamps: Dict[str, float] = {k: now for k in self._batch_buffer}  # topic_key → first batch time
        self._verbosity_mtime: float = 0.0  # mtime of verbosity.json for hot-reload

        # AGENT_FAILURE_CLUSTER dedup cache (source, 30-min bucket) -> True.
        # In-memory only; a notifier restart clears it (acceptable per the
        # proposal: the Option A producer-side canonicalisation is the
        # primary fix; this LRU is belt-and-braces for timing skew between
        # the two cluster producers within a single notifier lifetime).
        self._cluster_dedup_lru: "OrderedDict[Tuple[str, int], bool]" = OrderedDict()

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

        targets = self.resolve_all_targets(event)
        message = self.format_message(event)

        for platform, chat_id, thread_id in targets:
            topic_key = self._thread_id_to_key(thread_id)
            verbosity = self._verbosity.get(topic_key, {}).get("mode", "all")

            if verbosity == "off":
                continue
            if verbosity == "significant_only" and event.priority.level < Priority.HIGH.level:
                continue
            if verbosity == "digest_only":
                # 2026-04-30: previously this branch was a code-level
                # duplicate of ``significant_only`` (both gated on HIGH+).
                # That made ``digest_only`` indistinguishable from
                # ``significant_only`` for any topic, so an operator who
                # set their watchdog_alerts to ``digest_only`` got the
                # same incident-only stream and never saw the new
                # WATCHDOG_DAILY heartbeat. Distinct semantics now: the
                # mode passes HIGH+ failure-fires AND digest-class events
                # (CURATOR_DAILY, WATCHDOG_DAILY, DIGEST_GENERATED) at any
                # priority, but still drops routine NORMAL/LOW chatter.
                is_high = event.priority.level >= Priority.HIGH.level
                is_digest = event.event_type in DIGEST_EVENT_TYPES
                if not (is_high or is_digest):
                    continue

            # Low-priority events are batched for up to 5 minutes
            if event.priority == Priority.LOW:
                key = f"{chat_id}:{thread_id}"
                if key not in self._batch_buffer:
                    self._batch_buffer[key] = []
                    self._batch_timestamps[key] = time.monotonic()
                self._batch_buffer[key].append(message)
                self._persist_batch_buffer()
            else:
                # Pass event + topic_key so _deliver can emit the
                # NOTIFICATION_DELIVERED / NOTIFICATION_FAILED reverse
                # signal carrying original_event_id + target metadata.
                # Batched flushes call _deliver() without an event so
                # they DO NOT emit per-event reverse signals (Phase 1
                # scope per the design doc — keeps LOW-priority firehose
                # volume bounded). Spec at
                # docs/superpowers/specs/2026-04-30-notification-delivered-design.md.
                self._deliver(
                    chat_id, thread_id, message,
                    event=event, topic_key=topic_key,
                )

        # Flush any batches older than 5 minutes
        self._flush_stale_batches()

    def resolve_target(self, event: Event) -> Tuple[str, str, str]:
        """Resolve the primary Telegram target for an event."""
        # AGENT_ITERATION routes per-agent (2026-04-30): the canonical
        # agent name in payload.agent picks the topic. Falls through to
        # TOPIC_ROUTING['agent_iteration'] only if payload.agent is
        # missing or unknown.
        if event.event_type == EventType.AGENT_ITERATION:
            agent_name = ""
            payload = event.payload or {}
            if isinstance(payload, dict):
                agent_name = (payload.get('agent') or '').strip().lower()
            topic_key = AGENT_TOPIC_MAP.get(agent_name, AGENT_ITERATION_DEFAULT_TOPIC)
            topic = self.topics.get(topic_key, {})
            thread_id = str(topic.get("thread_id", ""))
            return ("telegram", self.group_chat_id, thread_id)

        topic_key = TOPIC_ROUTING.get(event.event_type.type_string, "system")

        # User-facing NOTIFICATION messages (morning digest, follow-up alerts,
        # etc.) belong in the ``digests`` topic where verbosity defaults to
        # ``all`` — NOT in ``agent_comms`` (the default for mailbox_message)
        # where the ``significant_only`` filter drops the default LOW priority
        # and the user never sees them.
        #
        # Regression: 2026-04-19 — the Sunday morning digest sat in the bus
        # (rowid 219957, priority=low) but telegram-notifier silently skipped
        # it because agent_comms verbosity=significant_only requires HIGH+.
        if (event.event_type == EventType.MAILBOX_MESSAGE
                and event.payload.get("message_type") == "NOTIFICATION"):
            requested_to = event.payload.get("to", "")
            # Honor explicit `to:` when it's a known v2 topic_key (e.g.
            # scribe-realtime emits to: jobflow_decisions / hermes_milestones / etc).
            # Legacy batch-digest payloads use `to: "telegram_digests"` (v1
            # label) which is NOT a v2 key — they fall through to scribe_daily,
            # preserving 2B behavior.
            if requested_to in self.topics:
                topic_key = requested_to
            else:
                topic_key = "scribe_daily"

        topic = self.topics.get(topic_key, {})
        thread_id = str(topic.get("thread_id", ""))
        return ("telegram", self.group_chat_id, thread_id)

    def resolve_all_targets(self, event: Event) -> List[Tuple[str, str, str]]:
        """Resolve all targets including cross-posts."""
        targets = [self.resolve_target(event)]

        # Cross-post action-required high/critical events to watchdog_alerts.
        # v2 cutover (20260424T233627Z) renamed the catch-all ``alerts`` topic
        # to ``watchdog_alerts``; the constant name CROSS_POST_TO_ALERTS is
        # kept as a generic descriptor of intent.
        if (event.event_type.type_string in CROSS_POST_TO_ALERTS
                and event.priority.level >= Priority.HIGH.level):
            alerts_topic = self.topics.get("watchdog_alerts", {})
            alerts_thread = str(alerts_topic.get("thread_id", ""))
            primary_thread = targets[0][2]
            if alerts_thread and alerts_thread != primary_thread:
                targets.append(("telegram", self.group_chat_id, alerts_thread))

        return targets

    def format_message(self, event: Event) -> str:
        """Format an event into a human-readable Telegram message."""
        from events.formatting import format_event_message

        body = self._format_payload(event)
        return format_event_message(event, body)

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

        if et == EventType.JOB_DISCOVERED:
            return f"Title: {p.get('title', '?')}\nCompany: {p.get('company', '?')}\nSource: {p.get('source', '?')}"

        if et in (EventType.JOB_SCORED, EventType.JOB_HIGH_SCORE):
            return f"Score: {p.get('score', '?')}\nTitle: {p.get('title', '?')}\nCompany: {p.get('company', '?')}"

        if et == EventType.APPLICATION_FAILED:
            return f"Error: {p.get('error', 'Unknown')}\nCompany: {p.get('company', '?')}"

        if et == EventType.GATEWAY_HEALTH:
            return f"Platform: {p.get('platform', '?')} → {p.get('status', '?')}\n{p.get('detail', '')}"

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
            # Per-agent run summary (2026-04-30). Lead with agent name +
            # the human-readable summary line. Counters are compactly
            # rendered as "k=v · k=v" so the message stays a single
            # readable line in Telegram even with 5-6 counters.
            agent = (p.get("agent") or "?").strip()
            summary = (p.get("summary") or "").strip()
            counters = p.get("counters") or {}
            anomalies = p.get("anomalies") or []
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

        # Generic fallback
        lines = [f"{k}: {v}" for k, v in p.items() if v]
        return "\n".join(lines[:10])

    def _trim_cron_summary(self, summary: str) -> str:
        """Keep Mission Control cron updates readable inside a chat topic."""
        summary = str(summary or "").strip()
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
    ) -> None:
        """Send a message to a Telegram chat/thread.

        When ``event`` is provided (non-batched delivery from handle()),
        emits NOTIFICATION_DELIVERED on success and NOTIFICATION_FAILED
        on exception, carrying original_event_id + target metadata.
        Both reverse-signal emits are wrapped in helpers that swallow
        their own exceptions, so a bus-side failure (transient SQLite
        lock, schema mismatch) NEVER breaks the upstream delivery path.

        Batched flushes (_flush_stale_batches) call this without an
        event so per-event reverse signals don't double the LOW-priority
        firehose volume — Phase 1 scoping per the design doc at
        docs/superpowers/specs/2026-04-30-notification-delivered-design.md.
        """
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
                )
            latency_ms = int((time.monotonic() - t0) * 1000)
            if event is not None:
                self._safe_emit_delivered(
                    event, chat_id, thread_id, topic_key, latency_ms,
                )
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.error("TelegramNotifier delivery failed: %s", exc)
            if event is not None:
                self._safe_emit_failed(
                    event, chat_id, thread_id, topic_key, latency_ms, exc,
                )

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

    def _flush_stale_batches(self, max_age: float = 300.0) -> None:
        """Flush batched low-priority messages older than max_age seconds."""
        now = time.monotonic()
        keys_to_flush = [
            k for k, ts in self._batch_timestamps.items()
            if now - ts >= max_age
        ]
        for key in keys_to_flush:
            messages = self._batch_buffer.pop(key, [])
            self._batch_timestamps.pop(key, None)
            if not messages:
                continue
            parts = key.split(":", 1)
            chat_id, thread_id = parts[0], parts[1] if len(parts) > 1 else ""
            combined = f"Batched ({len(messages)} events):\n\n" + "\n---\n".join(messages)
            self._deliver(chat_id, thread_id, combined)
        if keys_to_flush:
            self._persist_batch_buffer()

    def _persist_batch_buffer(self) -> None:
        """Write current batch state to disk so it survives restart."""
        try:
            save_state(notifier_batch_path(), {
                "buffer": {k: list(v) for k, v in self._batch_buffer.items()},
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
