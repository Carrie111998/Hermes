"""WhatsAppEscalator — sends escalated notifications to WhatsApp.

Filters events by escalation criteria, respects quiet hours (11pm-7am ET),
and queues non-breakthrough events for morning flush.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

# Events that escalate to WhatsApp
ESCALATION_EVENTS = {
    # Immediate (breakthrough during quiet hours)
    EventType.INTERVIEW_SIGNAL,
    EventType.OFFER_SIGNAL,
    # Urgent
    EventType.APPLICATION_BLOCKED,
    EventType.APPLICATION_FAILED,
    EventType.CRON_FAILED_CONSECUTIVE,
    EventType.GATEWAY_HEALTH,
    # Important
    EventType.JOB_HIGH_SCORE,  # only if score >= 9.0
    EventType.APPLICATION_READY,
    EventType.FOLLOWUP_DUE,
}

BREAKTHROUGH_EVENTS = {EventType.INTERVIEW_SIGNAL, EventType.OFFER_SIGNAL}

HIGH_SCORE_WA_THRESHOLD = 9.0


class WhatsAppEscalator(BaseSubscriber):
    subscriber_id = "whatsapp-escalator"
    poll_interval_seconds = 10

    def __init__(
        self,
        bus: EventBus,
        quiet_config_path: Optional[Path] = None,
        queue_path: Optional[Path] = None,
        send_fn: Optional[Callable] = None,
    ):
        super().__init__(bus)
        if quiet_config_path is None:
            from hermes_constants import get_hermes_home
            quiet_config_path = get_hermes_home() / "notifications" / "quiet_hours.json"
        if queue_path is None:
            from hermes_constants import get_hermes_home
            queue_path = get_hermes_home() / "notifications" / "quiet_queue.json"

        self._quiet_config_path = Path(quiet_config_path)
        self._queue_path = Path(queue_path)
        self._send_fn = send_fn
        self._quiet_config = self._load_quiet_config()

    def _load_quiet_config(self) -> Dict[str, Any]:
        if self._quiet_config_path.exists():
            return json.loads(self._quiet_config_path.read_text(encoding="utf-8"))
        return {
            "enabled": True,
            "start": "23:00",
            "end": "07:00",
            "timezone": "America/New_York",
            "breakthrough_events": ["interview_signal", "offer_signal"],
        }

    def should_escalate(self, event: Event) -> bool:
        """Check if this event meets WhatsApp escalation criteria."""
        if event.event_type not in ESCALATION_EVENTS:
            return False

        # JOB_HIGH_SCORE only escalates if score >= 9.0
        if event.event_type == EventType.JOB_HIGH_SCORE:
            score = event.payload.get("score", 0)
            return score >= HIGH_SCORE_WA_THRESHOLD

        # GATEWAY_HEALTH only escalates on "down"
        if event.event_type == EventType.GATEWAY_HEALTH:
            return event.payload.get("status") == "down"

        return True

    def should_deliver_now(self, event: Event) -> bool:
        """Check if event should be delivered now vs queued for morning."""
        if not self._is_quiet_hours():
            return True
        return event.event_type in BREAKTHROUGH_EVENTS

    def _is_quiet_hours(self) -> bool:
        """Check if current time is within quiet hours."""
        if not self._quiet_config.get("enabled", True):
            return False
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(self._quiet_config.get("timezone", "America/New_York"))
            now = datetime.now(tz)
            start_h, start_m = map(int, self._quiet_config["start"].split(":"))
            end_h, end_m = map(int, self._quiet_config["end"].split(":"))

            current_minutes = now.hour * 60 + now.minute
            start_minutes = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m

            if start_minutes > end_minutes:  # crosses midnight (23:00-07:00)
                return current_minutes >= start_minutes or current_minutes < end_minutes
            return start_minutes <= current_minutes < end_minutes
        except Exception:
            return False

    def handle(self, event: Event) -> None:
        if not self.should_escalate(event):
            return

        message = self.format_message(event)

        if self.should_deliver_now(event):
            self._deliver(message)
        else:
            self._queue_message(message)

    def format_message(self, event: Event) -> str:
        """Format event as plain-text WhatsApp message."""
        p = event.payload
        et = event.event_type

        if et == EventType.INTERVIEW_SIGNAL:
            text = f"Interview signal from {p.get('company', '?')}. {p.get('detail', '')}"
        elif et == EventType.OFFER_SIGNAL:
            text = f"Offer received from {p.get('company', '?')}! {p.get('detail', '')}"
        elif et == EventType.APPLICATION_BLOCKED:
            text = f"Application blocked at {p.get('company', '?')}: {p.get('question', 'needs your input')}"
        elif et == EventType.APPLICATION_FAILED:
            text = f"Application failed for {p.get('company', '?')}: {p.get('error', 'unknown error')}"
        elif et == EventType.APPLICATION_READY:
            text = f"Dry-run complete for {p.get('company', '?')} {p.get('title', '')}. Approve submission? Reply YES or NO."
        elif et == EventType.JOB_HIGH_SCORE:
            text = f"High-score job: {p.get('title', '?')} at {p.get('company', '?')} scored {p.get('score', '?')}"
        elif et == EventType.CRON_FAILED_CONSECUTIVE:
            text = f"Cron job '{p.get('job_name', '?')}' has failed {p.get('consecutive_errors', '?')} times in a row: {p.get('error', '')}"
        elif et == EventType.GATEWAY_HEALTH:
            text = f"Gateway {p.get('platform', '?')} is DOWN. {p.get('detail', '')}"
        elif et == EventType.FOLLOWUP_DUE:
            text = f"Follow-up due for {p.get('company', '?')} — {p.get('days', 14)}+ days no response"
        else:
            text = f"{et.type_string}: {json.dumps(p)[:200]}"

        return f"{text.strip()}\n\nDetails in Telegram"

    def _deliver(self, message: str) -> None:
        """Send message via WhatsApp."""
        if self._send_fn:
            self._send_fn(message)
            return
        try:
            from cron.scheduler import _deliver_result
            _deliver_result(
                {"deliver": "whatsapp", "id": "event-bus", "name": "event-bus"},
                message,
            )
        except Exception as e:
            logger.error("WhatsApp delivery failed: %s", e)

    def _queue_message(self, message: str) -> None:
        """Queue message for morning flush."""
        self._queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue = []
        if self._queue_path.exists():
            try:
                queue = json.loads(self._queue_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                queue = []
        queue.append({
            "message": message,
            "queued_at": datetime.now().isoformat(),
        })
        self._queue_path.write_text(json.dumps(queue, indent=2), encoding="utf-8")

    def flush_queue(self) -> int:
        """Flush queued messages as overnight summary.  Returns count flushed."""
        if not self._queue_path.exists():
            return 0
        try:
            queue = json.loads(self._queue_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0
        if not queue:
            return 0

        messages = [item["message"].split("\n\nDetails in Telegram")[0] for item in queue]
        summary = "Overnight Summary — {} events while you were away:\n\n".format(len(messages))
        summary += "\n\n".join(f"- {m}" for m in messages)
        summary += "\n\nDetails in Telegram"

        self._deliver(summary)
        self._queue_path.write_text("[]", encoding="utf-8")
        return len(queue)
