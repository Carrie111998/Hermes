"""ScribeRealtime — per-event-driven Scribe narration to Telegram topics.

Subscribes to 7 high-stakes Hermes event types and renders each via a
per-event-type template into an emoji-prefixed one-liner ≤160 chars.
Emits a `mailbox_message` NOTIFICATION event whose `to:` field directs
telegram-notifier to the correct v2 topic. Per the 2D-1 design spec:

    docs/superpowers/specs/2026-04-28-scribe-realtime-narration-design.md

The 7 trigger event types are also pruned from telegram-notifier's
TOPIC_ROUTING (separate change) so this subscriber is the canonical
primary delivery path. Cross-post-to-alerts behavior preserved for
high-priority events.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, Tuple

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)


# Per-event-type templates: map from event_type.type_string to
# (target_topic_key, render_fn(payload) -> str).
#
# render_fn: takes the event's payload dict, returns a one-line string.
# Defensive: every field access uses .get(..., '?') so missing fields
# render as a placeholder rather than crashing.
_TEMPLATES: Dict[str, Tuple[str, Callable[[dict], str]]] = {
    "job_high_score": (
        "jobflow_decisions",
        lambda p: f"🎯 Matcher: {p.get('title','?')} at {p.get('company','?')} "
                  f"scored {p.get('score','?')} → Tailor",
    ),
    "application_submitted": (
        "jobflow_firehose",
        lambda p: f"📤 Submitted: {p.get('title','?')} at {p.get('company','?')}",
    ),
    "application_ready": (
        "jobflow_decisions",
        lambda p: f"📝 Ready: {p.get('title','?')} at {p.get('company','?')} "
                  f"— reply YES to submit, NO to skip",
    ),
    "interview_signal": (
        "hermes_milestones",
        lambda p: (f"🎉 Interview: {p.get('company','?')} — {p.get('title','?')}. "
                   f"{(p.get('detail') or '')[:80]}").rstrip(". "),
    ),
    "offer_signal": (
        "hermes_milestones",
        lambda p: f"🎉 OFFER: {p.get('company','?')} — {p.get('title','?')}!",
    ),
    "critic_proposal": (
        "critic_proposals",
        lambda p: (f"🧠 Critic ({p.get('kind','proposal')}): "
                   f"{(p.get('summary') or '?')[:120]}"),
    ),
    "curator_daily": (
        "curator_digest",
        lambda p: (f"🌙 Curator: {p.get('agents_updated','?')} agents, "
                   f"{p.get('patterns_seeded','?')} patterns, "
                   f"{p.get('skills_observed','?')} skills"),
    ),
}


class ScribeRealtime(BaseSubscriber):
    """Renders 7 high-stakes Hermes events into Scribe-styled one-liners.

    Each handle() invocation looks up a per-event-type template, renders
    the line, and emits a mailbox_message NOTIFICATION event tagged with
    the destination topic_key. The existing telegram-notifier subscriber
    handles actual Telegram delivery via its modified resolve_target.
    """

    subscriber_id = "scribe-realtime"
    poll_interval_seconds = 30
    event_types = [
        EventType.JOB_HIGH_SCORE,
        EventType.APPLICATION_SUBMITTED,
        EventType.APPLICATION_READY,
        EventType.INTERVIEW_SIGNAL,
        EventType.OFFER_SIGNAL,
        EventType.CRITIC_PROPOSAL,
        EventType.CURATOR_DAILY,
    ]

    def handle(self, event: Event) -> None:
        try:
            template = _TEMPLATES.get(event.event_type.type_string)
            if template is None:
                # Filter is event_types-bounded; this branch is defensive
                # in case the filter is widened or BaseSubscriber semantics change.
                return
            topic_key, render_fn = template
            payload = event.payload or {}
            line = render_fn(payload)[:160]  # final hard-truncate safety net
            self._emit_notification(line, topic_key, event.event_type.type_string)
        except Exception as exc:
            logger.warning(
                "scribe-realtime: handle failed for %s (%s): %s",
                event.event_id, event.event_type.type_string, exc,
            )

    def _emit_notification(
        self, summary: str, topic_key: str, scribe_realtime_type: str
    ) -> None:
        """Emit a mailbox_message NOTIFICATION that telegram-notifier will
        route to `topic_key` via its modified resolve_target.
        """
        self.bus.emit(
            event_type=EventType.MAILBOX_MESSAGE,
            source="scribe-realtime",
            payload={
                "from": "scribe-realtime",
                "to": topic_key,
                "message_type": "NOTIFICATION",
                "summary": summary,
                "scribe_realtime_type": scribe_realtime_type,
            },
            priority=Priority.NORMAL,
        )
