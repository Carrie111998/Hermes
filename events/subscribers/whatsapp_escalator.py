"""WhatsAppEscalator — sends escalated notifications to WhatsApp.

Filters events by escalation criteria, respects quiet hours (11pm-7am ET),
and queues non-breakthrough events for morning flush.
"""

import json
import logging
import os
import time
from collections import deque
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)


class EscalationTier(Enum):
    """4 WhatsApp escalation tiers per design spec Section 2.2.

    - IMMEDIATE: breaks through quiet hours, delivered without throttle
    - URGENT: queued during quiet hours, flushed at 7:01am
    - IMPORTANT: queued during quiet hours, throttled to 15-min windows
    - DIGEST: morning digest, sent at 7:01am by DigestComposer
    """

    IMMEDIATE = ("immediate", 40)
    URGENT = ("urgent", 30)
    IMPORTANT = ("important", 20)
    DIGEST = ("digest", 10)

    def __init__(self, label: str, priority: int):
        self.label = label
        self.priority = priority


# Tier assignment per event type (static mapping)
_TIER_BY_EVENT: Dict[EventType, EscalationTier] = {
    # Immediate
    EventType.INTERVIEW_SIGNAL: EscalationTier.IMMEDIATE,
    EventType.OFFER_SIGNAL: EscalationTier.IMMEDIATE,
    # Urgent
    EventType.APPLICATION_BLOCKED: EscalationTier.URGENT,
    EventType.APPLICATION_FAILED: EscalationTier.URGENT,
    EventType.CRON_FAILED_CONSECUTIVE: EscalationTier.URGENT,
    EventType.AGENT_ERROR: EscalationTier.URGENT,
    # Phase B Stage-3 iter2: approval requests interrupt the LangGraph until
    # Diego replies. Treat as URGENT so they queue during quiet hours and
    # flush at 7:01am rather than wake him up at 3am.
    EventType.APPROVAL_REQUEST: EscalationTier.URGENT,
    # GATEWAY_HEALTH is conditional (only down) — handled in classify_tier
    # AGENT_ERROR escalation is gated by cluster threshold — see should_escalate()
    # Important
    EventType.APPLICATION_READY: EscalationTier.IMPORTANT,
    EventType.FOLLOWUP_DUE: EscalationTier.IMPORTANT,
    # Phase B Stage-3 iter2: APPLY_PACKET hands materials to Diego for the
    # final manual click. Important but not urgent.
    EventType.APPLY_PACKET: EscalationTier.IMPORTANT,
    # Phase C iter2: Critic proposals are advisory; daily flush is enough.
    EventType.CRITIC_PROPOSAL: EscalationTier.IMPORTANT,
    # iter5: proper watchdog signals (replacing AGENT_ERROR-fallback hack).
    # WATCHDOG_PROBE_TRANSITION at HIGH = real probe state flip (gateway down,
    # API server :8642 down, etc.). Treat as URGENT so it queues during quiet
    # hours and flushes at 7:01am. Routine probe ticks aren't routed here.
    EventType.WATCHDOG_PROBE_TRANSITION: EscalationTier.URGENT,
    # WATCHDOG_SILENCE_ALERT at HIGH = an agent missed its expected cadence
    # (e.g. jaum-inbox-sweeper silent for 1042s vs expected 600s). Important,
    # not urgent — operator can act in next digest window.
    EventType.WATCHDOG_SILENCE_ALERT: EscalationTier.IMPORTANT,
    # AGENT_FAILURE_CLUSTER at HIGH = >=3 consecutive failures from the same
    # source. Real ones (NOT watchdog self-emissions thanks to the
    # source=watchdog skip in watchdog_sweep.py) escalate as URGENT.
    EventType.AGENT_FAILURE_CLUSTER: EscalationTier.URGENT,
    # WATCHDOG_TICK + WATCHDOG_RECOVERED intentionally NOT in tier map ->
    # bus-only, no WhatsApp escalation.
    # JOB_HIGH_SCORE is conditional (>= 9.0) — handled in classify_tier
}

HIGH_SCORE_WA_THRESHOLD = 9.0

# Sustained-failure window for agent_error events. Isolated agent_errors stay
# digest-only (avoids flooding WhatsApp); only a cluster (>= threshold in window)
# escalates as URGENT so the user sees a real outage.
AGENT_ERROR_WINDOW_SECONDS = 900
AGENT_ERROR_CLUSTER_THRESHOLD = 3


def classify_tier(event: Event) -> Optional[EscalationTier]:
    """Classify an event into its WhatsApp escalation tier.

    Returns None if the event should not be escalated to WhatsApp at all
    (e.g. GATEWAY_HEALTH when status=up, or JOB_HIGH_SCORE below threshold).
    """
    # Conditional: GATEWAY_HEALTH only escalates on "down"
    if event.event_type == EventType.GATEWAY_HEALTH:
        if event.payload.get("status") == "down":
            return EscalationTier.URGENT
        return None

    # Conditional: JOB_HIGH_SCORE only escalates above WhatsApp threshold
    if event.event_type == EventType.JOB_HIGH_SCORE:
        score = event.payload.get("score", 0)
        if score >= HIGH_SCORE_WA_THRESHOLD:
            return EscalationTier.IMPORTANT
        return None

    return _TIER_BY_EVENT.get(event.event_type)


class WhatsAppEscalator(BaseSubscriber):
    subscriber_id = "whatsapp-escalator"
    poll_interval_seconds = 5

    # Throttle: combine events within a 15-minute window into one message
    THROTTLE_WINDOW_SECONDS = 900  # 15 minutes

    def __init__(
        self,
        bus: EventBus,
        quiet_config_path: Optional[Path] = None,
        queue_path: Optional[Path] = None,
        send_fn: Optional[Callable] = None,
    ):
        super().__init__(bus)
        if quiet_config_path is None:
            from events.paths import quiet_hours_path
            quiet_config_path = quiet_hours_path()

        self._quiet_config_path = Path(quiet_config_path)
        self._send_fn = send_fn
        self._quiet_config = self._load_quiet_config()

        # Queue path precedence: explicit argument > quiet_hours.json queue_file > default
        if queue_path is not None:
            self._queue_path = Path(queue_path)
        elif self._quiet_config.get("queue_file"):
            self._queue_path = Path(os.path.expanduser(self._quiet_config["queue_file"]))
        else:
            from events.paths import quiet_queue_path
            self._queue_path = quiet_queue_path()

        # Throttle state
        self._throttle_buffer: List[str] = []
        self._throttle_start: Optional[float] = None
        self._daily_send_count: int = 0
        self._daily_reset_date: Optional[str] = None

        # Sustained-failure tracking for agent_error clustering
        self._agent_error_times: Deque[float] = deque()

    # Sensible defaults used whenever the config file is missing or
    # malformed.  Matching the spec: 23:00-07:00 ET, interview/offer
    # signals break through.
    _DEFAULT_QUIET_CONFIG: Dict[str, Any] = {
        "enabled": True,
        "start": "23:00",
        "end": "07:00",
        "timezone": "America/New_York",
        "breakthrough_events": ["interview_signal", "offer_signal"],
    }

    def _load_quiet_config(self) -> Dict[str, Any]:
        """Load quiet_hours.json, falling back to defaults on any failure."""
        if not self._quiet_config_path.exists():
            return dict(self._DEFAULT_QUIET_CONFIG)
        try:
            data = json.loads(self._quiet_config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"expected dict, got {type(data).__name__}")
            return data
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning(
                "WhatsAppEscalator: malformed quiet_hours.json at %s: %s — using defaults",
                self._quiet_config_path, e,
            )
            return dict(self._DEFAULT_QUIET_CONFIG)

    def should_escalate(self, event: Event) -> bool:
        """Check if this event meets WhatsApp escalation criteria.

        Delegates to classify_tier() — an event escalates iff it maps to
        a non-None tier (Immediate / Urgent / Important).

        agent_error has cluster-aware logic: a single isolated error is
        noise (stays digest-only), but a burst (>= threshold within the
        window) is a real outage and escalates as URGENT.
        """
        if event.event_type == EventType.AGENT_ERROR:
            return self._agent_error_cluster_triggered()
        return classify_tier(event) is not None

    def _agent_error_cluster_triggered(self) -> bool:
        """Track agent_error timestamps and decide whether a cluster
        threshold has been crossed. Called on each agent_error arrival.
        """
        now = time.monotonic()
        self._agent_error_times.append(now)
        # Drop entries outside the window
        cutoff = now - AGENT_ERROR_WINDOW_SECONDS
        while self._agent_error_times and self._agent_error_times[0] < cutoff:
            self._agent_error_times.popleft()
        return len(self._agent_error_times) >= AGENT_ERROR_CLUSTER_THRESHOLD

    def should_deliver_now(self, event: Event) -> bool:
        """Check if event should be delivered now vs queued for morning.

        Breakthrough events (IMMEDIATE tier) deliver even during quiet
        hours; everything else gets queued for the 7:01am flush.
        """
        if not self._is_quiet_hours():
            return True
        return classify_tier(event) == EscalationTier.IMMEDIATE

    def _is_quiet_hours(self) -> bool:
        """Check if current time is within quiet hours.

        Fail-safe: on any config parsing error (bad timezone, malformed
        time, etc.), logs once and treats the time as quiet.  This is
        the conservative fallback — a misconfigured system should err
        on the side of NOT sending WhatsApp at 3am, rather than let
        everything through.
        """
        if not self._quiet_config.get("enabled", True):
            return False

        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(self._quiet_config.get("timezone", "America/New_York"))
        except Exception as e:
            self._log_config_error_once(
                f"invalid timezone {self._quiet_config.get('timezone')!r}: {e}"
            )
            return True  # fail-safe: treat as quiet

        try:
            start_h, start_m = map(int, self._quiet_config["start"].split(":"))
            end_h, end_m = map(int, self._quiet_config["end"].split(":"))
        except (KeyError, ValueError, AttributeError) as e:
            self._log_config_error_once(f"invalid start/end time in quiet_hours: {e}")
            return True  # fail-safe: treat as quiet

        now = datetime.now(tz)
        current_minutes = now.hour * 60 + now.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        if start_minutes > end_minutes:  # crosses midnight (23:00-07:00)
            return current_minutes >= start_minutes or current_minutes < end_minutes
        return start_minutes <= current_minutes < end_minutes

    def _log_config_error_once(self, msg: str) -> None:
        """Log a config parse error the first time we encounter it per instance.

        Avoids spamming the audit log with the same error every 5 seconds.
        """
        if getattr(self, "_config_error_logged", False):
            return
        logger.error("WhatsAppEscalator: quiet_hours config error — %s "
                     "(failing safe: treating time as quiet)", msg)
        self._config_error_logged = True

    def handle(self, event: Event) -> None:
        if not self.should_escalate(event):
            return

        # Reset daily counter at midnight
        today = datetime.now().strftime("%Y-%m-%d")
        if self._daily_reset_date != today:
            self._daily_send_count = 0
            self._daily_reset_date = today

        message = self.format_message(event)

        if not self.should_deliver_now(event):
            self._queue_message(message)
            return

        # Breakthrough events always deliver immediately (bypass throttle)
        if classify_tier(event) == EscalationTier.IMMEDIATE:
            self._deliver(message)
            return

        # Throttle: buffer events within 15-minute windows
        now = time.monotonic()
        if self._throttle_start is None:
            self._throttle_start = now

        self._throttle_buffer.append(message.split("\n\nDetails in Telegram")[0])

        if now - self._throttle_start >= self.THROTTLE_WINDOW_SECONDS:
            self._flush_throttle_buffer()

    def format_message(self, event: Event) -> str:
        """Format event as plain-text WhatsApp message."""
        from events.formatting import format_whatsapp_message

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
        elif et == EventType.AGENT_ERROR:
            count = len(self._agent_error_times)
            source_agent = p.get("source_agent") or p.get("source", "?")
            msg = p.get("message") or p.get("error", "agent error")
            text = (
                f"{count} agent errors in last 15 min. Latest: {source_agent}: "
                f"{str(msg)[:140]}"
            )
        else:
            text = f"{et.type_string}: {json.dumps(p)[:200]}"

        formatted = format_whatsapp_message(event, text.strip())
        return f"{formatted}\n\nDetails in Telegram"

    def _flush_throttle_buffer(self) -> None:
        """Flush accumulated throttle buffer into a single WhatsApp message."""
        if not self._throttle_buffer:
            self._throttle_start = None
            return
        if len(self._throttle_buffer) == 1:
            text = self._throttle_buffer[0] + "\n\nDetails in Telegram"
        else:
            text = f"{len(self._throttle_buffer)} updates:\n\n"
            text += "\n\n".join(f"- {m}" for m in self._throttle_buffer)
            text += "\n\nDetails in Telegram"
        self._deliver(text)
        self._throttle_buffer.clear()
        self._throttle_start = None

    def shutdown(self) -> None:
        """Flush pending throttle buffer and queue on shutdown."""
        self._flush_throttle_buffer()

    def _deliver(self, message: str) -> bool:
        """Send message via WhatsApp.  Returns True on success, False on failure."""
        if self._send_fn:
            try:
                self._send_fn(message)
                return True
            except Exception as e:
                logger.error("WhatsApp delivery failed (send_fn): %s", e)
                return False
        try:
            from cron.scheduler import _deliver_result
            err = _deliver_result(
                {"deliver": "whatsapp", "id": "event-bus", "name": "event-bus"},
                message,
                skip_cron_framing=True,
            )
            if err:
                logger.warning("WhatsApp delivery failed: %s", err)
                return False
            return True
        except Exception as e:
            logger.error("WhatsApp delivery failed: %s", e)
            return False

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

    # Maximum body characters per chunk. Sized to keep the full summary
    # (header + body + footer) under WhatsApp's 4096-char per-message text
    # limit, with margin for the "(N/M)" header and the "Details in Telegram"
    # footer. The bridge's express.json() body limit (~100KB by default) is
    # also satisfied by chunks of this size.
    _FLUSH_CHUNK_BODY_LIMIT = 3500

    def flush_queue(self) -> int:
        """Flush queued messages as one or more chunked summaries.

        Returns the count of messages successfully delivered.

        Each delivered chunk fits under WhatsApp's 4096-char text limit and
        the bridge's express.json() body limit. On partial failure (some
        chunks succeed, a later one fails), the unsent remainder is
        preserved for retry on the next flush — preventing both duplicate
        sends of already-delivered chunks and silent loss of queued alerts.

        If the bridge is unreachable or the target cannot be resolved (e.g.
        missing WHATSAPP_HOME_CHANNEL), the entire queue is preserved.
        """
        if not self._queue_path.exists():
            return 0
        try:
            queue = json.loads(self._queue_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0
        if not queue:
            return 0

        messages = [item["message"].split("\n\nDetails in Telegram")[0] for item in queue]
        total = len(messages)

        # Group consecutive messages into chunks while combined body length
        # stays under the limit. Each chunk records the slice [start, end)
        # of `queue` it represents, so partial failure can preserve the
        # exact unsent remainder.
        chunks: List[tuple] = []  # (start_idx, end_idx, body_text)
        cur_start = 0
        cur_lines: List[str] = []
        cur_chars = 0
        for i, m in enumerate(messages):
            line = f"- {m}"
            # +2 accounts for the "\n\n" separator before this line.
            if cur_chars + len(line) + 2 > self._FLUSH_CHUNK_BODY_LIMIT and cur_lines:
                chunks.append((cur_start, i, "\n\n".join(cur_lines)))
                cur_start = i
                cur_lines = []
                cur_chars = 0
            cur_lines.append(line)
            cur_chars += len(line) + 2
        if cur_lines:
            chunks.append((cur_start, total, "\n\n".join(cur_lines)))

        n_chunks = len(chunks)

        for idx, (start, _end, body) in enumerate(chunks):
            if n_chunks == 1:
                header = f"Overnight Summary — {total} events while you were away:"
            else:
                header = (
                    f"Overnight Summary ({idx + 1}/{n_chunks}) — "
                    f"{total} events while you were away:"
                )
            summary = f"{header}\n\n{body}\n\nDetails in Telegram"

            if not self._deliver(summary):
                # Stop on first failure; preserve everything from this chunk
                # forward so the next flush can retry the unsent remainder.
                unsent = queue[start:]
                self._queue_path.write_text(
                    json.dumps(unsent, indent=2), encoding="utf-8"
                )
                logger.warning(
                    "WhatsApp flush_queue: delivered %d/%d chunks (%d/%d msgs); "
                    "preserving %d remaining queued messages",
                    idx, n_chunks, start, total, len(unsent),
                )
                return start

        # All chunks delivered.
        self._queue_path.write_text("[]", encoding="utf-8")
        return total
