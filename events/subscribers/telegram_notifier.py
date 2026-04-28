"""TelegramNotifier subscriber — routes events to Telegram forum topics.

Reads topic registry from ~/.hermes/telegram/topics.json and verbosity
config from ~/.hermes/telegram/verbosity.json.  Delivers messages via
the gateway's Telegram adapter send() method.
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from events.bus import EventBus
from events.paths import notifier_batch_path
from events.schema import Event, EventType, Priority
from events.state import load_state, save_state
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

# Maps event_type string → topic key
TOPIC_ROUTING: Dict[str, str] = {
    # === Hermes Telegram v2 (cutover 20260424T233627Z) ===
    # -> jobflow_firehose
    'job_discovered': 'jobflow_firehose',
    'job_vip_discovered': 'jobflow_firehose',
    'job_scored': 'jobflow_firehose',
    'tailor_completed': 'jobflow_firehose',
    'application_submitted': 'jobflow_firehose',
    'stage_transition': 'jobflow_firehose',
    'followup_due': 'jobflow_firehose',
    # -> jobflow_decisions
    'job_high_score': 'jobflow_decisions',
    'application_ready': 'jobflow_decisions',
    'interview_signal': 'jobflow_decisions',
    'offer_signal': 'jobflow_decisions',
    'approval_request': 'jobflow_decisions',
    'apply_packet': 'jobflow_decisions',
    # -> devflow_firehose
    'run_started': 'devflow_firehose',
    'run_completed': 'devflow_firehose',
    'trace_snapshot': 'devflow_firehose',
    'devflow.run_started': 'devflow_firehose',
    'devflow.run_completed': 'devflow_firehose',
    'devflow.trace_snapshot': 'devflow_firehose',
    # -> devflow_decisions
    'approval_requested': 'devflow_decisions',
    'devflow.approval_requested': 'devflow_decisions',
    # -> watchdog_alerts
    'gateway_health': 'watchdog_alerts',
    'agent_error': 'watchdog_alerts',
    'cron_started': 'watchdog_alerts',
    'cron_completed': 'watchdog_alerts',
    'cron_failed': 'watchdog_alerts',
    'cron_failed_consecutive': 'watchdog_alerts',
    'cron_stale': 'watchdog_alerts',
    'application_blocked': 'watchdog_alerts',
    'application_failed': 'watchdog_alerts',
    # iter5: proper watchdog event types (replacing AGENT_ERROR fallback)
    'watchdog_tick': 'watchdog_alerts',
    'watchdog_probe_transition': 'watchdog_alerts',
    'watchdog_silence_alert': 'watchdog_alerts',
    'watchdog_recovered': 'watchdog_alerts',
    'agent_failure_cluster': 'watchdog_alerts',
    # -> critic_proposals
    'critic_proposal': 'critic_proposals',
    'critic_auto_applied': 'critic_proposals',
    'critic_self_degraded': 'critic_proposals',
    # NOTE: 'agent_failure_cluster' is the Critic's TRIGGER, not its proposal.
    # It routes to watchdog_alerts (line ~60, with the other watchdog signals).
    # The Critic still consumes it via the bus and emits critic_proposal events
    # which DO route here.
    # -> curator_digest
    'curator_daily': 'curator_digest',
    'memory_consolidated': 'curator_digest',
    'skill_evolved': 'curator_digest',
    # -> scribe_daily
    # digest_generated is an observability event (Watchdog/Critic cadence
    # tracking) — NOT delivered to Telegram. The actual digest content
    # arrives via the special-case mailbox_message + NOTIFICATION path
    # below (search "message_type") so Diego sees one digest per fire,
    # not two.
    'scribe_digest': 'scribe_daily',
    'mailbox_message': 'scribe_daily',
    # -> security_and_system
    'secret_detected': 'security_and_system',
}

# Events that cross-post to alerts when high/critical
CROSS_POST_TO_ALERTS = {
    'application_ready',
    'followup_due',
    'interview_signal',
    'job_high_score',
    'offer_signal',
}

CRON_SUMMARY_MAX_LINES = 24
CRON_SUMMARY_MAX_CHARS = 1500
CRON_SUMMARY_TRUNCATION_NOTE = (
    "[Mission Control trimmed the rest; full run output remains in cron history/artifacts.]"
)


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
        # Hot-reload verbosity config on each cycle (spec: hot-reloadable)
        self._reload_verbosity()

        # Infrastructure noise: lag alerts about the bus itself become a feedback
        # loop (digest -> agent_error -> digest). Suppress them from chat; they
        # remain in the bus for audit-logger and the gateway log.
        if (event.event_type == EventType.AGENT_ERROR
                and event.source == "event-bus"):
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

        # secret_detected volume is unbounded when the scanner runs; route via
        # audit-logger only. A daily rollup is handled by the digest.
        if event.event_type.type_string == "secret_detected":
            return

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
            if verbosity == "digest_only" and event.priority.level < Priority.HIGH.level:
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
                self._deliver(chat_id, thread_id, message)

        # Flush any batches older than 5 minutes
        self._flush_stale_batches()

    def resolve_target(self, event: Event) -> Tuple[str, str, str]:
        """Resolve the primary Telegram target for an event."""
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

    def _deliver(self, chat_id: str, thread_id: str, message: str) -> None:
        """Send a message to a Telegram chat/thread."""
        if self._send_fn:
            self._send_fn(chat_id, thread_id, message)
            return

        # Production: use gateway delivery
        try:
            from cron.scheduler import _deliver_result
            target_str = f"telegram:{chat_id}:{thread_id}" if thread_id else f"telegram:{chat_id}"
            _deliver_result(
                {"deliver": target_str, "id": "event-bus", "name": "event-bus"},
                message,
                skip_cron_framing=True,
            )
        except Exception as e:
            logger.error("TelegramNotifier delivery failed: %s", e)

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
