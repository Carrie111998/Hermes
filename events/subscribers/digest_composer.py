"""DigestComposer — produces 3x/day structured notification digests.

Timer-based subscriber that fires at 8am, 1pm, and 6pm.  Queries the
event bus for events since the last digest and formats a structured summary.
Posts to the Digests & Summaries Telegram topic and WhatsApp (morning only).
"""

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

DIGEST_SCHEDULE_HOURS = [8, 13, 18]  # ET


class DigestComposer(BaseSubscriber):
    subscriber_id = "digest-composer"
    poll_interval_seconds = 60  # check every minute if digest is due

    def __init__(
        self,
        bus: EventBus,
        send_telegram_fn: Optional[Callable] = None,
        send_whatsapp_fn: Optional[Callable] = None,
    ):
        super().__init__(bus)
        self._send_telegram_fn = send_telegram_fn
        self._send_whatsapp_fn = send_whatsapp_fn
        self._last_digest_at: Optional[str] = None

    def handle(self, event: Event) -> None:
        # DigestComposer doesn't process individual events via handle().
        # It uses compose() triggered by the timer.  This is a no-op so the
        # base subscriber can still poll and ack to advance the cursor.
        pass

    def compose(self, since: Optional[str] = None) -> str:
        """Compose a digest from events since the given timestamp (or last digest)."""
        query_since = since or self._last_digest_at
        events = self.bus.query(since=query_since) if query_since else self.bus.query()
        self._last_digest_at = datetime.now(timezone.utc).isoformat()

        if not events:
            return self._format_empty_digest()

        return self._format_digest(events)

    def _format_digest(self, events: List[Event]) -> str:
        """Format a list of events into a structured digest."""
        now_str = datetime.now(timezone.utc).strftime("%b %d %Y %H:%M UTC")
        period = self._get_period_label()

        # Count events by type
        type_counts: Counter = Counter()
        source_counts: Counter = Counter()
        action_items: List[str] = []
        highlights: List[str] = []
        errors: List[str] = []

        for e in events:
            type_counts[e.event_type] += 1
            source_counts[e.source] += 1

            if e.event_type == EventType.APPLICATION_READY:
                action_items.append(
                    f"Approve dry-run for {e.payload.get('company', '?')} "
                    f"{e.payload.get('title', '')}".strip()
                )
            elif e.event_type == EventType.FOLLOWUP_DUE:
                action_items.append(
                    f"Follow up with {e.payload.get('company', '?')} "
                    f"({e.payload.get('days', 14)}+ days)"
                )
            elif e.event_type == EventType.APPLICATION_BLOCKED:
                action_items.append(
                    f"Unblock application at {e.payload.get('company', '?')}: "
                    f"{e.payload.get('question', 'needs input')}"
                )

            if e.event_type == EventType.JOB_HIGH_SCORE:
                highlights.append(
                    f"{e.payload.get('title', '?')} at {e.payload.get('company', '?')} "
                    f"scored {e.payload.get('score', '?')}"
                )
            elif e.event_type in (EventType.INTERVIEW_SIGNAL, EventType.OFFER_SIGNAL):
                highlights.append(
                    f"{e.event_type.type_string.upper()}: {e.payload.get('company', '?')}"
                )

            if e.event_type in (EventType.CRON_FAILED, EventType.CRON_FAILED_CONSECUTIVE, EventType.AGENT_ERROR):
                errors.append(f"{e.source}: {e.payload.get('error', 'unknown')[:100]}")

        # Build digest
        lines = [f"HERMES DIGEST — {period} / {now_str}", ""]

        # Event summary by source
        lines.append("SINCE LAST DIGEST")
        discovered = type_counts.get(EventType.JOB_DISCOVERED, 0)
        scored = type_counts.get(EventType.JOB_SCORED, 0) + type_counts.get(EventType.JOB_HIGH_SCORE, 0)
        tailored = type_counts.get(EventType.TAILOR_COMPLETED, 0)
        submitted = type_counts.get(EventType.APPLICATION_SUBMITTED, 0)
        transitions = type_counts.get(EventType.STAGE_TRANSITION, 0)

        if discovered:
            lines.append(f"  Scout: {discovered} new jobs found")
        if scored:
            high = type_counts.get(EventType.JOB_HIGH_SCORE, 0)
            lines.append(f"  Matcher: {scored} scored — {high} HIGH (>=8.75)")
        if tailored:
            lines.append(f"  Tailor: {tailored} resumes generated")
        if submitted:
            lines.append(f"  Applier: {submitted} submitted")
        if transitions:
            lines.append(f"  Tracker: {transitions} stage transitions")
        if not any([discovered, scored, tailored, submitted, transitions]):
            lines.append("  No activity since last digest")

        # Highlights
        if highlights:
            lines.append("")
            lines.append("HIGHLIGHTS")
            for h in highlights:
                lines.append(f"  {h}")

        # Action items
        if action_items:
            lines.append("")
            lines.append("ACTION ITEMS")
            for item in action_items:
                lines.append(f"  -> {item}")

        # Errors
        if errors:
            lines.append("")
            lines.append("SYSTEM HEALTH")
            for err in errors:
                lines.append(f"  ! {err}")
        else:
            lines.append("")
            lines.append("SYSTEM HEALTH")
            cron_ok = type_counts.get(EventType.CRON_COMPLETED, 0)
            lines.append(f"  {cron_ok} cron jobs completed OK")

        return "\n".join(lines)

    def _format_empty_digest(self) -> str:
        now_str = datetime.now(timezone.utc).strftime("%b %d %Y %H:%M UTC")
        period = self._get_period_label()
        return f"HERMES DIGEST — {period} / {now_str}\n\nNo activity since last digest."

    def _get_period_label(self) -> str:
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo("America/New_York")
            hour = datetime.now(tz).hour
        except Exception:
            hour = datetime.now().hour

        if hour < 12:
            return "Morning"
        if hour < 17:
            return "Midday"
        return "Evening"
