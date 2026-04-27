"""DigestComposer — produces 3x/day structured notification digests.

Timer-based subscriber that fires at 8am, 1pm, and 6pm.  Queries the
event bus for events since the last digest and formats a structured summary.
Posts to the Digests & Summaries Telegram topic and WhatsApp (morning only).
"""

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from events.bus import EventBus
from events.paths import digest_state_path
from events.schema import Event, EventType, Priority
from events.state import load_state, save_state
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

DIGEST_SCHEDULE_HOURS = [8, 13, 18]  # ET


class DigestComposer(BaseSubscriber):
    subscriber_id = "digest-composer"
    poll_interval_seconds = 60  # check every minute if digest is due

    # The jobflow-notifier cron job writes pipeline snapshot data here
    # on a 7am cron.  DigestComposer reads it to incorporate pipeline
    # counts into the digest body (spec §7).
    DEFAULT_SNAPSHOT_PATH = Path.home() / ".hermes" / "profiles" / "notifier" / "workspace" / "digest-data.json"
    SNAPSHOT_STALE_AFTER_HOURS = 24

    def __init__(
        self,
        bus: EventBus,
        send_telegram_fn: Optional[Callable] = None,
        send_whatsapp_fn: Optional[Callable] = None,
        notifier_snapshot_path: Optional[Path] = None,
    ):
        super().__init__(bus)
        self._send_telegram_fn = send_telegram_fn
        self._send_whatsapp_fn = send_whatsapp_fn
        self._last_digest_at: Optional[str] = None
        state = load_state(digest_state_path(), default={})
        self._last_digest_at = state.get("last_digest_at")
        self._snapshot_path = Path(notifier_snapshot_path) if notifier_snapshot_path else self.DEFAULT_SNAPSHOT_PATH

    def handle(self, event: Event) -> None:
        # DigestComposer doesn't process individual events via handle().
        # It uses compose() triggered by the timer.  This is a no-op so the
        # base subscriber can still poll and ack to advance the cursor.
        pass

    def compose(self, since: Optional[str] = None) -> str:
        """Compose a digest from events since the given timestamp (or last digest).

        After formatting, delivers to Telegram (always) and WhatsApp (morning only).
        """
        query_since = since or self._last_digest_at
        events = self.bus.query(since=query_since) if query_since else self.bus.query()
        self._last_digest_at = datetime.now(timezone.utc).isoformat()
        save_state(digest_state_path(), {"last_digest_at": self._last_digest_at})

        snapshot = self._load_notifier_snapshot()

        if not events:
            digest = self._format_empty_digest(snapshot)
        else:
            digest = self._format_digest(events, snapshot)

        # Deliver to Telegram Digests topic
        self._deliver_telegram(digest)

        # Morning digest also goes to WhatsApp (condensed)
        if self._is_morning():
            self._deliver_whatsapp(digest, events)

        # Emit digest_generated event for audit trail
        from events.schema import EventType
        self.bus.emit(
            event_type=EventType.DIGEST_GENERATED,
            source="digest-composer",
            payload={"period": self._get_period_label(), "event_count": len(events)},
        )

        return digest

    def _load_notifier_snapshot(self) -> Optional[Dict[str, Any]]:
        """Load the notifier's latest pipeline snapshot, if fresh.

        Expected format (written by the jobflow-notifier cron job):
            {
                "generated_at": "<ISO8601 UTC>",
                "pipeline_snapshot": {
                    "total_active": int,
                    "by_stage": {<stage>: int, ...}
                },
                "action_items": [...],         # optional, used by composer
                "system_health": {...}         # optional
            }

        Returns None when the file is missing, unreadable, malformed,
        or older than SNAPSHOT_STALE_AFTER_HOURS — so compose() always
        falls back to event-bus-only digests.
        """
        if not self._snapshot_path.exists():
            return None
        try:
            data = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("DigestComposer: ignoring malformed notifier snapshot: %s", e)
            return None
        if not isinstance(data, dict):
            return None

        # Reject stale snapshots so we don't report yesterday's pipeline as current
        generated_at = data.get("generated_at")
        if generated_at:
            try:
                # Strip trailing Z which fromisoformat pre-3.11 rejects
                ts = generated_at.rstrip("Z")
                gen = datetime.fromisoformat(ts)
                if gen.tzinfo is None:
                    gen = gen.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - gen
                if age > timedelta(hours=self.SNAPSHOT_STALE_AFTER_HOURS):
                    logger.info(
                        "DigestComposer: notifier snapshot is stale (%.1fh old), skipping",
                        age.total_seconds() / 3600,
                    )
                    return None
            except ValueError:
                # Bad timestamp — treat as unknown freshness, still skip
                return None

        return data

    def _format_pipeline_block(self, snapshot: Dict[str, Any]) -> List[str]:
        """Format the pipeline snapshot block from notifier data."""
        ps = snapshot.get("pipeline_snapshot", {})
        if not isinstance(ps, dict):
            return []
        lines: List[str] = []
        total = ps.get("total_active")
        by_stage = ps.get("by_stage", {}) if isinstance(ps.get("by_stage"), dict) else {}
        if total is None and not by_stage:
            return []
        lines.append("")
        lines.append("PIPELINE SNAPSHOT")
        if total is not None:
            lines.append(f"  Total active: {total}")
        for stage, count in by_stage.items():
            lines.append(f"  {stage}: {count}")
        return lines

    def _is_morning(self) -> bool:
        """Check if current ET hour is in the morning window."""
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo("America/New_York")
            return datetime.now(tz).hour < 12
        except Exception:
            return datetime.now().hour < 12

    def _deliver_telegram(self, digest: str) -> None:
        """Post digest to the Digests & Summaries Telegram topic."""
        if self._send_telegram_fn:
            try:
                self._send_telegram_fn(digest)
            except Exception as e:
                logger.error("DigestComposer: Telegram delivery failed: %s", e)
            return
        # Production: use gateway delivery
        try:
            from cron.scheduler import _deliver_result
            import json as _json
            from events.paths import telegram_topics_path
            topics_path = telegram_topics_path()
            if topics_path.exists():
                data = _json.loads(topics_path.read_text(encoding="utf-8"))
                chat_id = data.get("group_chat_id", "")
                # v2 cutover (20260424T233627Z) renamed v1 'digests' to
                # 'scribe_daily' (the destination for digest_generated /
                # morning JobFlow digests per telegram_notifier.TOPIC_ROUTING).
                thread_id = data.get("topics", {}).get("scribe_daily", {}).get("thread_id", "")
                if chat_id and thread_id:
                    target = f"telegram:{chat_id}:{thread_id}"
                    _deliver_result(
                        {"deliver": target, "id": "digest-composer", "name": "digest-composer"},
                        digest,
                        skip_cron_framing=True,
                    )
        except Exception as e:
            logger.error("DigestComposer: Telegram delivery failed: %s", e)

    def _deliver_whatsapp(self, digest: str, events: List[Event]) -> None:
        """Deliver condensed morning digest to WhatsApp using the shared
        event protocol (priority dot + 📝 icon + source + timestamp header).
        """
        # Build condensed version: highlights + action items only
        action_items = []
        highlights = []
        for e in events:
            if e.event_type == EventType.APPLICATION_READY:
                action_items.append(f"Approve: {e.payload.get('company', '?')}")
            elif e.event_type == EventType.FOLLOWUP_DUE:
                action_items.append(f"Follow up: {e.payload.get('company', '?')}")
            elif e.event_type == EventType.JOB_HIGH_SCORE:
                highlights.append(f"{e.payload.get('title', '?')} @ {e.payload.get('company', '?')} ({e.payload.get('score', '?')})")

        body_lines = [f"{len(events)} events overnight"]
        if highlights:
            body_lines.append("High scores: " + "; ".join(highlights[:3]))
        if action_items:
            body_lines.append("Action needed: " + "; ".join(action_items[:3]))
        body_lines.append("Full details in Telegram")
        body = "\n".join(body_lines)

        # Wrap with the shared protocol header so WhatsApp matches Telegram.
        # Build a lightweight DIGEST_GENERATED pseudo-event for formatting.
        from events.formatting import format_whatsapp_message
        from datetime import datetime, timezone
        pseudo_event = Event(
            event_id="digest",
            event_type=EventType.DIGEST_GENERATED,
            source="digest-composer",
            timestamp=datetime.now(timezone.utc).isoformat(),
            priority=Priority.LOW,
            payload={"period": self._get_period_label(), "event_count": len(events)},
        )
        condensed = format_whatsapp_message(pseudo_event, body)

        if self._send_whatsapp_fn:
            try:
                self._send_whatsapp_fn(condensed)
            except Exception as e:
                logger.error("DigestComposer: WhatsApp delivery failed: %s", e)
            return
        try:
            from cron.scheduler import _deliver_result
            _deliver_result(
                {"deliver": "whatsapp", "id": "digest-composer", "name": "digest-composer"},
                condensed,
                skip_cron_framing=True,
            )
        except Exception as e:
            logger.error("DigestComposer: WhatsApp delivery failed: %s", e)

    def _format_digest(self, events: List[Event], snapshot: Optional[Dict[str, Any]] = None) -> str:
        """Format a list of events into a structured digest."""
        now_str = datetime.now(timezone.utc).strftime("%b %d %Y %H:%M UTC")
        period = self._get_period_label()

        # Count events by type
        type_counts: Counter = Counter()
        source_counts: Counter = Counter()
        action_items: List[str] = []
        highlights: List[str] = []
        error_groups: Counter = Counter()
        eventbus_worst_lag: Dict[str, int] = {}

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
                # Dedupe event-bus lag alerts: track max lag per subscriber,
                # drop the row-by-row "Subscriber X is N behind" spam.
                if e.source == "event-bus" and e.event_type == EventType.AGENT_ERROR:
                    sub_id = e.payload.get("subscriber_id", "?")
                    lag = e.payload.get("lag", 0)
                    if lag > eventbus_worst_lag.get(sub_id, 0):
                        eventbus_worst_lag[sub_id] = lag
                    continue
                # Payload key varies across emitters: base.py / gateway_integration
                # use 'error', but mailbox-* AGENT_ERRORs use 'message', and some
                # cron_failed payloads use 'reason' or 'summary'.  Falling back in
                # priority order stops the "unknown" rendering we saw for
                # mailbox:sentinel and similar sources.
                error_text = (
                    e.payload.get("error")
                    or e.payload.get("message")
                    or e.payload.get("reason")
                    or e.payload.get("summary")
                    or "unknown"
                )
                key = f"{e.source}: {str(error_text)[:100]}"
                error_groups[key] += 1

        # Build digest
        lines = [f"HERMES DIGEST — {period} / {now_str}", ""]

        # Pipeline snapshot from notifier (if fresh)
        if snapshot:
            lines.extend(self._format_pipeline_block(snapshot))
            if lines[-1] != "":
                lines.append("")

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
        lines.append("")
        lines.append("SYSTEM HEALTH")
        if eventbus_worst_lag:
            worst_sub = max(eventbus_worst_lag, key=eventbus_worst_lag.get)
            worst_lag = eventbus_worst_lag[worst_sub]
            n_subs = len(eventbus_worst_lag)
            lines.append(
                f"  ! event-bus: {n_subs} subscribers fell behind "
                f"(worst: {worst_sub} {worst_lag} events)"
            )
        if error_groups:
            # Show at most 5 distinct errors, collapsed by (source, error-prefix)
            for (key, count) in error_groups.most_common(5):
                suffix = f" (x{count})" if count > 1 else ""
                lines.append(f"  ! {key}{suffix}")
            extra = len(error_groups) - 5
            if extra > 0:
                lines.append(f"  ! (+{extra} other distinct errors)")
        if not eventbus_worst_lag and not error_groups:
            cron_ok = type_counts.get(EventType.CRON_COMPLETED, 0)
            lines.append(f"  {cron_ok} cron jobs completed OK")

        return "\n".join(lines)

    def _format_empty_digest(self, snapshot: Optional[Dict[str, Any]] = None) -> str:
        now_str = datetime.now(timezone.utc).strftime("%b %d %Y %H:%M UTC")
        period = self._get_period_label()
        lines = [f"HERMES DIGEST — {period} / {now_str}"]
        if snapshot:
            lines.extend(self._format_pipeline_block(snapshot))
        lines.append("")
        lines.append("No activity since last digest.")
        return "\n".join(lines)

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
