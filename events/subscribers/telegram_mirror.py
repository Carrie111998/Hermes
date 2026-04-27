"""TelegramMirror — shadow-copies inter-agent mailbox messages to Telegram.

Subscribes only to mailbox_message events (emitted by MailboxWatcher)
and posts formatted summaries to the Agent Comms topic.
"""

import json
import logging
from pathlib import Path
from typing import Callable, Optional

from events.bus import EventBus
from events.schema import Event, EventType
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)


class TelegramMirror(BaseSubscriber):
    subscriber_id = "telegram-mirror"
    poll_interval_seconds = 60
    event_types = [EventType.MAILBOX_MESSAGE]

    def __init__(
        self,
        bus: EventBus,
        topics_path: Optional[Path] = None,
        send_fn: Optional[Callable] = None,
    ):
        super().__init__(bus)
        if topics_path is None:
            from events.paths import telegram_topics_path
            topics_path = telegram_topics_path()
        self._topics_path = Path(topics_path)
        self._send_fn = send_fn

    def handle(self, event: Event) -> None:
        message = self.format_mirror_message(event)
        self._deliver_to_agent_comms(message)

    def format_mirror_message(self, event: Event) -> str:
        """Format a mailbox message event for the Agent Comms topic."""
        from events.formatting import format_event_message
        p = event.payload or {}
        summary = p.get("summary", "")
        body = summary or p.get("message_type", "")
        return format_event_message(event, body)

    def _deliver_to_agent_comms(self, message: str) -> None:
        """Send to the Agent Comms topic."""
        if self._send_fn:
            self._send_fn(message)
            return

        try:
            config = json.loads(self._topics_path.read_text(encoding="utf-8"))
            chat_id = config.get("group_chat_id", "")
            # v2 cutover (20260424T233627Z) collapsed the v1 'agent_comms'
            # vs 'digests' split into a single 'scribe_daily' topic — the
            # destination for mailbox_message per telegram_notifier.TOPIC_ROUTING.
            thread_id = str(config.get("topics", {}).get("scribe_daily", {}).get("thread_id", ""))
            if not chat_id or not thread_id:
                return

            from cron.scheduler import _deliver_result
            _deliver_result(
                {"deliver": f"telegram:{chat_id}:{thread_id}", "id": "event-bus", "name": "event-bus"},
                message,
                skip_cron_framing=True,
            )
        except Exception as e:
            logger.error("TelegramMirror delivery failed: %s", e)
