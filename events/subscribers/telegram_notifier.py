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
    # Alerts & Actions
    "application_blocked": "alerts",
    "application_failed": "alerts",
    "interview_signal": "alerts",
    "offer_signal": "alerts",
    "cron_failed_consecutive": "alerts",
    "gateway_health": "alerts",
    # Scout
    "job_discovered": "scout",
    "job_vip_discovered": "scout",
    # Matcher
    "job_scored": "matcher",
    "job_high_score": "matcher",
    # Tailor & Applier
    "tailor_completed": "tailor_applier",
    "application_ready": "tailor_applier",
    "application_submitted": "tailor_applier",
    # Tracker
    "stage_transition": "tracker",
    "followup_due": "tracker",
    # Digests
    "digest_generated": "digests",
    # System Health
    "cron_started": "system",
    "cron_completed": "system",
    "cron_failed": "system",
    "agent_error": "system",
    "memory_consolidated": "system",
    "skill_evolved": "system",
    # Agent Comms
    "mailbox_message": "agent_comms",
}

# Events that cross-post to alerts when high/critical
CROSS_POST_TO_ALERTS = {
    "job_high_score", "application_ready", "followup_due",
}


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
        topic = self.topics.get(topic_key, {})
        thread_id = str(topic.get("thread_id", ""))
        return ("telegram", self.group_chat_id, thread_id)

    def resolve_all_targets(self, event: Event) -> List[Tuple[str, str, str]]:
        """Resolve all targets including cross-posts."""
        targets = [self.resolve_target(event)]

        # Cross-post action-required high/critical events to alerts
        if (event.event_type.type_string in CROSS_POST_TO_ALERTS
                and event.priority.level >= Priority.HIGH.level):
            alerts_topic = self.topics.get("alerts", {})
            alerts_thread = str(alerts_topic.get("thread_id", ""))
            primary_thread = targets[0][2]
            if alerts_thread and alerts_thread != primary_thread:
                targets.append(("telegram", self.group_chat_id, alerts_thread))

        return targets

    def format_message(self, event: Event) -> str:
        """Format an event into a human-readable Telegram message."""
        ts = event.timestamp[:19].replace("T", " ")
        priority_label = event.priority.label.upper()
        header = f"[{priority_label}] {event.event_type.type_string} from {event.source} @ {ts} UTC"

        body = self._format_payload(event)
        return f"{header}\n{body}" if body else header

    def _format_payload(self, event: Event) -> str:
        """Format event payload into readable text."""
        p = event.payload
        et = event.event_type

        if et == EventType.CRON_COMPLETED:
            summary = p.get("output_summary", "")
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

        # Generic fallback
        lines = [f"{k}: {v}" for k, v in p.items() if v]
        return "\n".join(lines[:10])

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
