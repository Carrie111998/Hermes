"""WhatsAppEscalator — sends escalated notifications to WhatsApp.

Filters events by escalation criteria, respects quiet hours (11pm-7am ET),
and queues non-breakthrough events for morning flush.
"""

import dataclasses
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
from events.failure_eligibility import failure_cluster_eligible
from events.noise_guards import RepeatGuard, is_sustained_resource_repeat
from events.routing_policy import (
    WA_IMMEDIATE,
    WA_IMPORTANT,
    WA_URGENT,
    classify as policy_classify,
    resolve_topic_thread,
)
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


# v3 (2026-07-18): the per-event tier dict (_TIER_BY_EVENT) is GONE —
# escalation derives from events.routing_policy.classify() (P3: WhatsApp is
# a contract, not a list). ACT always pages (IMMEDIATE at CRITICAL, else
# URGENT); WARN pages at CRITICAL or via explicit per-type pins;
# job_high_score >= 9.0 is phone-worthy IMPORTANT. Conditional logic that
# lived here (gateway_health down-only, probe actionability, high-score
# threshold) lives in routing_policy's hooks now.
_TIER_BY_LABEL: Dict[str, EscalationTier] = {
    WA_IMMEDIATE: EscalationTier.IMMEDIATE,
    WA_URGENT: EscalationTier.URGENT,
    WA_IMPORTANT: EscalationTier.IMPORTANT,
}


def classify_tier(event: Event) -> Optional[EscalationTier]:
    """Classify an event into its WhatsApp escalation tier.

    Returns None if the event should not be escalated to WhatsApp at all.
    v3: thin adapter over events.routing_policy — the policy's wa_tier
    string maps onto this module's EscalationTier enum.
    """
    route = policy_classify(event)
    if route.wa_tier is None:
        return None
    return _TIER_BY_LABEL.get(route.wa_tier)


# Sustained-failure window for agent_error events. Isolated agent_errors stay
# digest-only (avoids flooding WhatsApp); only a cluster (>= threshold in window)
# escalates as URGENT so the user sees a real outage.
AGENT_ERROR_WINDOW_SECONDS = 900
AGENT_ERROR_CLUSTER_THRESHOLD = 3

# Cycle-prevention: this subscriber emits NOTIFICATION_DELIVERED /
# NOTIFICATION_FAILED from inside _deliver(); if it ALSO consumed those
# events the loop is identical to telegram-notifier's. Today
# classify_tier returns None for both types so should_escalate naturally
# drops them, but the explicit guard is defense-in-depth against future
# tier-mapping changes (e.g. someone routes NOTIFICATION_FAILED to URGENT
# so Diego gets a WhatsApp ping when Telegram fails — that change MUST
# also keep this guard intact). Spec at
# docs/superpowers/specs/2026-04-30-notification-delivered-design.md.
_NEVER_CONSUME = frozenset({
    EventType.NOTIFICATION_DELIVERED,
    EventType.NOTIFICATION_FAILED,
})


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

        # Throttle state — persisted across restarts (2026-07-11): a buffered
        # escalation used to be in-memory only, so a gateway restart inside
        # the 15-min window silently dropped it. Monotonic timestamps don't
        # survive restarts, so a restored buffer restarts its window "now"
        # (worst case: one extra window of delay, never a loss).
        from events.state import load_state as _load_state
        from events.paths import whatsapp_throttle_path as _throttle_path
        self._throttle_buffer: List[str] = []
        self._throttle_start: Optional[float] = None
        _saved_throttle = _load_state(_throttle_path(), default={})
        if isinstance(_saved_throttle.get("buffer"), list):
            self._throttle_buffer = [str(m) for m in _saved_throttle["buffer"]]
        if self._throttle_buffer:
            self._throttle_start = time.monotonic()
        self._daily_send_count: int = 0
        self._daily_reset_date: Optional[str] = None

        # Sustained-failure tracking for agent_error clustering
        self._agent_error_times: Deque[float] = deque()

        # P8 (v3): when a WhatsApp send fails, the same message lands in
        # the Action Required Telegram topic with a 📵 marker so a
        # phone-worthy item is never silently lost while the bridge is
        # down (the 2026-07-16/18 credential_loss escalations died
        # exactly this way). Once per unique message per 6h so the
        # 900s retry loops don't spam the topic with fallbacks.
        self._fallback_guard = RepeatGuard(window_seconds=6 * 3600.0)
        # Repeating identical escalations collapse (30-min window).
        self._wa_repeat_guard = RepeatGuard()

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
            return (
                failure_cluster_eligible(event)
                and self._agent_error_cluster_triggered()
            )
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
        # Cycle prevention (2026-04-30): this subscriber EMITS delivery
        # events from _deliver(); consuming them would recurse. Belt-
        # and-braces over the routing policy's current DROP behavior.
        if event.event_type in _NEVER_CONSUME:
            return

        route = policy_classify(event)
        if event.event_type == EventType.AGENT_ERROR:
            if (
                not failure_cluster_eligible(event)
                or not self._agent_error_cluster_triggered()
            ):
                return
        elif route.wa_tier is None:
            return

        if is_sustained_resource_repeat(event):
            return

        # Reset daily counter at midnight
        today = datetime.now().strftime("%Y-%m-%d")
        if self._daily_reset_date != today:
            self._daily_send_count = 0
            self._daily_reset_date = today

        message = self.format_message(event, route=route)

        # v3 P4 on the phone lane: a repeating identical escalation
        # (normalized — digits ignored) within 30 min is one page, not N.
        # IMMEDIATE tier is exempt (interview/offer/secret/credential).
        tier = _TIER_BY_LABEL.get(route.wa_tier)
        if (tier != EscalationTier.IMMEDIATE
                and self._wa_repeat_guard.is_repeat("wa", message)):
            return

        if self._is_quiet_hours() and tier != EscalationTier.IMMEDIATE:
            self._queue_message(message)
            return

        # Breakthrough events always deliver immediately (bypass throttle).
        # Pass `event` so _deliver emits the NOTIFICATION_DELIVERED /
        # NOTIFICATION_FAILED reverse signal carrying original_event_id +
        # latency. Throttled / queued sends call _deliver WITHOUT an event
        # so they DO NOT emit per-event reverse signals (Phase 1 scope —
        # spec §"Where to emit"). Failure paths in throttle/queue flushes
        # still log via the existing logger.warning; their reverse-signal
        # coverage is a Phase 2 follow-up.
        if tier == EscalationTier.IMMEDIATE:
            if not self._deliver(message, event=event):
                # 2026-07-11: a failed breakthrough send (e.g. WhatsApp
                # bridge 503 mid-reconnect) used to be dropped on the
                # floor. Requeue into the bounded quiet queue so the
                # stranded-queue retry in gateway_integration re-attempts
                # it instead of losing the one escalation that mattered.
                self._queue_message(message)
                logger.warning(
                    "WhatsAppEscalator: IMMEDIATE delivery failed; "
                    "requeued for stranded-queue retry"
                )
            return

        # Throttle: buffer events within 15-minute windows
        now = time.monotonic()
        if self._throttle_start is None:
            self._throttle_start = now

        self._throttle_buffer.append(message.split("\n\nDetails in Telegram")[0])
        self._persist_throttle_buffer()

        if now - self._throttle_start >= self.THROTTLE_WINDOW_SECONDS:
            self._flush_throttle_buffer()

    def format_message(self, event: Event, route=None) -> str:
        """Format event as plain-text WhatsApp message.

        ``route`` is optional for direct/test callers. Delivery passes its
        already-computed route so routing, escalation, and presentation share
        exactly one outcome verdict.

        Every escalated type gets a complete plain-English sentence. The
        pre-2026-07-11 fallback (`json.dumps(payload)[:200]`) shipped
        truncated raw JSON to Diego's phone — never reintroduce it; add a
        branch (or a shared body helper in events.formatting) instead.
        """
        from events.formatting import (
            blocked_question_line,
            blocked_question_options_block,
            boot_summary_body,
            container_crash_loop_body,
            failure_cluster_body,
            format_whatsapp_message,
            humanize_health_detail,
            probe_transition_body,
            resource_pressure_body,
            silence_alert_body,
            watchdog_burst_body,
        )

        if route is None:
            route = policy_classify(event)
        if route.priority is not event.priority:
            event = dataclasses.replace(event, priority=route.priority)

        p = event.payload
        et = event.event_type

        if et == EventType.INTERVIEW_SIGNAL:
            text = f"Interview signal from {p.get('company', '?')}. {p.get('detail', '')}"
        elif et == EventType.OFFER_SIGNAL:
            text = f"Offer received from {p.get('company', '?')}! {p.get('detail', '')}"
        elif et == EventType.APPLICATION_BLOCKED:
            # The choices go on their own lines, not inline: an answer to a
            # Workday listbox has to be VERBATIM one of the tenant's labels or
            # it is never clicked, and a comma run inside `question` is both
            # ambiguous about where a label ends and subject to the 200-char
            # summary budget that already truncated the real Capital One list.
            text = (f"Application blocked at {p.get('company', '?')}: "
                    f"{blocked_question_line(p)}")
            options = blocked_question_options_block(p)
            if options:
                text = text + "\n\n" + options
        elif et == EventType.APPLICATION_FAILED:
            text = f"Application failed for {p.get('company', '?')}: {p.get('error', 'unknown error')}"
        elif et == EventType.APPLICATION_READY:
            text = f"Dry-run complete for {p.get('company', '?')} {p.get('title', '')}. Approve submission? Reply YES or NO."
        elif et == EventType.JOB_HIGH_SCORE:
            text = f"High-score job: {p.get('title', '?')} at {p.get('company', '?')} scored {p.get('score', '?')}"
        elif et == EventType.CRON_FAILED_CONSECUTIVE:
            text = f"Cron job '{p.get('job_name', '?')}' has failed {p.get('consecutive_errors', '?')} times in a row: {p.get('error', '')}"
        elif et == EventType.GATEWAY_HEALTH:
            reason = humanize_health_detail(p.get("detail", ""))
            text = f"The {p.get('platform', '?')} gateway is DOWN"
            text += f" — {reason}" if reason else "."
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
        elif et == EventType.CREDENTIAL_LOSS:
            # Named credential/infra loss from the watchdog sweep (2026-07-10).
            probe = p.get("probe", "?")
            after = str(p.get("after", "down")).upper()
            text = f"🔑 Credential loss: {probe} is {after}. {p.get('detail', '')}".strip()
        elif et == EventType.MODEL_RATE_LIMITED:
            # Added 2026-08-14 with the event type. Without an arm here the
            # message fell through to the generic key:value dump
            # ("model_rate_limited: provider: deepseek · model: ... ·
            # outcome: chain_exhausted"), which is the one place an operator
            # reads at 3am. The two ACT outcomes are deliberately worded
            # apart because their REMEDY differs: chain_exhausted means every
            # configured alternative is also down (wait, or divert outside the
            # chain); no_fallback means none was ever configured (go add one).
            _outcome = (p.get("outcome") or "").strip().lower()
            _model = p.get("model") or "?"
            _provider = p.get("provider") or "?"
            _calls = p.get("diverted_calls") or 0
            if _outcome == "recovered":
                text = (f"{_model} ({_provider}) is back — rate limit cleared "
                        f"after {_calls} diverted call(s).")
            elif _outcome == "no_fallback":
                text = (f"{_model} ({_provider}) is rate limited and has NO "
                        f"fallback configured — runs are failing. {_calls} "
                        f"call(s) affected. Add a fallback provider.")
            elif _outcome == "chain_exhausted":
                text = (f"{_model} ({_provider}) is rate limited and every "
                        f"fallback is exhausted — runs are failing. {_calls} "
                        f"call(s) affected.")
            else:
                text = (f"{_model} ({_provider}) is rate limited — traffic "
                        f"diverted to {p.get('fallback_model') or '?'}. "
                        f"{_calls} call(s) so far.")
            if p.get("resets_at"):
                text += f" Resets {p['resets_at']}."
        elif et == EventType.WATCHDOG_BURST:
            text = watchdog_burst_body(p)
        elif et == EventType.WATCHDOG_PROBE_TRANSITION:
            text = probe_transition_body(p)
        elif et == EventType.WATCHDOG_SILENCE_ALERT:
            text = silence_alert_body(p)
        elif et == EventType.AGENT_FAILURE_CLUSTER:
            text = failure_cluster_body(p)
        elif et == EventType.CONTAINER_CRASH_LOOP:
            text = container_crash_loop_body(p)
        elif et == EventType.RESOURCE_PRESSURE:
            # Without this branch the scalar fallback takes scalars[:6] in
            # payload order and stops BEFORE disk_c_free_gb — a disk-full page
            # that never mentions the disk (2026-08-14).
            text = resource_pressure_body(p)
        elif et == EventType.BOOT_SUMMARY:
            # Only reachable via an explicit --priority critical emit (WARN
            # pages at CRITICAL); the scalar fallback would silently DROP
            # failures/anomalies, the only two fields that say what broke.
            text = boot_summary_body(p, max_listed=3)
        elif et == EventType.SECRET_DETECTED:
            text = (
                f"Possible secret ({p.get('rule_id', '?')}) found in "
                f"{p.get('file_path', '?')}:{p.get('line_no', '?')}. "
                f"Verify and rotate if real."
            )
        elif et == EventType.APPROVAL_REQUEST:
            text = (
                f"JobFlow is paused waiting on your approval: "
                f"{p.get('job_title', '?')} at {p.get('job_company', '?')} "
                f"(score {p.get('score', '?')})."
            )
        elif et == EventType.APPLY_PACKET:
            text = (
                f"Apply packet ready: {p.get('title', '?')} at "
                f"{p.get('company', '?')} — everything staged for your manual submit."
            )
        elif et == EventType.CRITIC_PROPOSAL:
            text = (
                f"Critic proposal ({p.get('kind', 'tuning')}): "
                f"{str(p.get('summary') or 'see Telegram for the proposal')[:200]}"
            )
        elif et == EventType.DEVFLOW_BUILD_FAILED:
            text = f"Build '{p.get('build_name', '?')}' failed in {p.get('repo', '?')}"
            if p.get("branch"):
                text += f" on {p['branch']}"
            text += "."
            err = str(p.get("error_summary") or "").strip()
            if err:
                text += f" {err[:160]}"
        elif et == EventType.DEVFLOW_PR_REVIEW_REQUESTED:
            text = (
                f"PR #{p.get('pr_number', '?')} in {p.get('repo', '?')} "
                f"needs your review: {p.get('title', '?')}"
            )
            if p.get("url"):
                text += f"\n{p['url']}"
        elif et == EventType.DEVFLOW_APPROVAL_REQUESTED:
            text = f"DevFlow run {p.get('run_id', '?')} needs your approval"
            reason = str(p.get("reason") or p.get("title") or "").strip()
            text += f": {reason[:160]}" if reason else "."
        else:
            # Last-resort fallback for types added to the tier map without a
            # branch above: readable scalar pairs, never raw/truncated JSON.
            scalars = [
                f"{k}: {v}" for k, v in p.items()
                if isinstance(v, (str, int, float, bool))
                and str(v).strip() and k != "watchdog_type"
            ]
            body = " · ".join(scalars[:6])[:300]
            text = (
                f"{et.type_string}: {body}" if body
                else f"{et.type_string} event — details in Telegram"
            )

        formatted = format_whatsapp_message(
            event, text.strip(), verdict=route.verdict,
        )
        return f"{formatted}\n\nDetails in Telegram"

    def _flush_throttle_buffer(self) -> None:
        """Flush accumulated throttle buffer into a single WhatsApp message.

        On delivery failure the combined message is requeued into the
        bounded quiet queue (2026-07-11) — before that, a bridge outage at
        flush time silently dropped every buffered escalation (observed
        2026-07-11 11:29, bridge 503 "Not connected to WhatsApp").
        """
        if not self._throttle_buffer:
            self._throttle_start = None
            return
        if len(self._throttle_buffer) == 1:
            text = self._throttle_buffer[0] + "\n\nDetails in Telegram"
        else:
            text = f"{len(self._throttle_buffer)} updates:\n\n"
            text += "\n\n".join(f"- {m}" for m in self._throttle_buffer)
            text += "\n\nDetails in Telegram"
        if not self._deliver(text):
            self._queue_message(text)
            logger.warning(
                "WhatsAppEscalator: throttle flush failed; requeued to quiet queue"
            )
        self._throttle_buffer.clear()
        self._throttle_start = None
        self._persist_throttle_buffer()

    def poll(self) -> int:
        """Poll events, then age out the throttle buffer.

        2026-07-11: the throttle buffer used to flush only from inside
        handle() — i.e. only when a LATER escalatable event arrived. A
        lone URGENT event with no follow-up sat buffered indefinitely
        (until shutdown). Piggybacking on the registry's poll cadence
        gives a wall-clock flush without a dedicated timer thread.
        """
        n = super().poll()
        self._maybe_flush_throttle()
        return n

    def _maybe_flush_throttle(self) -> None:
        """Flush the throttle buffer once its window has aged out."""
        if not self._throttle_buffer or self._throttle_start is None:
            return
        if time.monotonic() - self._throttle_start < self.THROTTLE_WINDOW_SECONDS:
            return
        if self._is_quiet_hours():
            # The window aged out INTO quiet hours (e.g. buffered 22:50,
            # flush due 23:05): move to the morning queue instead of
            # pinging overnight.
            for m in self._throttle_buffer:
                self._queue_message(m + "\n\nDetails in Telegram")
            self._throttle_buffer.clear()
            self._throttle_start = None
            self._persist_throttle_buffer()
            return
        self._flush_throttle_buffer()

    def _persist_throttle_buffer(self) -> None:
        """Write throttle state to disk so it survives restart."""
        try:
            from events.paths import whatsapp_throttle_path
            from events.state import save_state
            save_state(whatsapp_throttle_path(), {"buffer": list(self._throttle_buffer)})
        except Exception:
            logger.exception("WhatsAppEscalator: failed to persist throttle buffer")

    def shutdown(self) -> None:
        """Flush pending throttle buffer and queue on shutdown."""
        self._flush_throttle_buffer()

    def _deliver(
        self,
        message: str,
        *,
        event: Optional[Event] = None,
    ) -> bool:
        """Send message via WhatsApp. Returns True on success, False on failure.

        When ``event`` is provided (non-throttled, non-queued delivery
        from handle()), emits NOTIFICATION_DELIVERED on success and
        NOTIFICATION_FAILED on failure carrying the original event id +
        target. Both reverse-signal emits swallow their own exceptions;
        a bus failure here MUST NOT mask the upstream delivery state.

        Throttled / queue-flush call sites pass no event so they do
        NOT emit per-event reverse signals (Phase 1 scoping per the
        design doc — keeps overnight digest flushes from doubling the
        bus volume). Spec at
        docs/superpowers/specs/2026-04-30-notification-delivered-design.md.
        """
        # Formatter observability (2026-08-19), mirroring the Telegram lane
        # (e3ffba1eaa / e1bd6b1c74). The rendered escalation existed NOWHERE
        # on disk: audit.jsonl carries the event PAYLOAD, the RepeatGuards
        # keep only a sha of the normalized text and die with the process,
        # and no message id is persisted. That is exactly how the UNKNOWN
        # AGENT_NOTE header rendered wrong on EVERY Telegram delivery while
        # 759 tests, an 83/83 coverage gate and three clean delivery
        # receipts all passed -- a receipt proves DELIVERY, never that the
        # delivered TEXT is right. This method is the single choke point
        # for all three send paths (handle, throttle flush, queue flush)
        # and for both the _send_fn (test) and _deliver_result (production)
        # branches; _telegram_fallback is reached only from here, so it is
        # covered too.
        #
        # HEADER LINE ONLY, deliberately. The header carries everything the
        # formatter decides -- priority marker, label, icon, type, source,
        # timestamp -- which is the whole surface this exists to watch. The
        # body is CALLER CONTENT and may be sensitive, so it must not be
        # written to a log file. body_chars is a length, never content, and
        # is enough to catch truncation. Do not widen this back to the full
        # message.
        #
        # Uses splitlines() rather than a newline-literal split: no
        # backslash escape, so the line cannot be corrupted by a shell
        # heredoc eating an escape level -- which is exactly how the first
        # attempt at this edit produced an unterminated string literal.
        header = message.splitlines()[0] if message else ""
        logger.info(
            "WhatsAppEscalator sending: %r (+%d body chars)",
            header, len(message) - len(header),
        )
        t0 = time.monotonic()
        ok = False
        exc: Optional[Exception] = None

        if self._send_fn:
            try:
                self._send_fn(message)
                ok = True
            except Exception as e:
                logger.error("WhatsApp delivery failed (send_fn): %s", e)
                exc = e
        else:
            try:
                from cron.scheduler import _deliver_result
                err = _deliver_result(
                    {"deliver": "whatsapp", "id": "event-bus", "name": "event-bus"},
                    message,
                    skip_cron_framing=True,
                )
                if err:
                    logger.warning("WhatsApp delivery failed: %s", err)
                    # _deliver_result returns an error string; surface it
                    # as a synthetic exception so the reverse signal
                    # records why the bridge rejected the send.
                    exc = RuntimeError(str(err))
                else:
                    ok = True
            except Exception as e:
                logger.error("WhatsApp delivery failed: %s", e)
                exc = e

        latency_ms = int((time.monotonic() - t0) * 1000)
        if event is not None:
            if ok:
                self._safe_emit_delivered(event, latency_ms)
            else:
                self._safe_emit_failed(
                    event, latency_ms, exc or RuntimeError("unknown"),
                )
        if not ok:
            self._telegram_fallback(message)
        return ok

    def _telegram_fallback(self, message: str) -> None:
        """P8: the escalation lane monitors itself. On WhatsApp failure,
        deliver the same text to the Action Required Telegram topic with a
        📵 prefix. Swallows every exception — fallback must never break
        the caller's requeue path. Production only: when a test injects
        ``send_fn`` there is no real bridge and no real Telegram either.
        """
        if self._send_fn is not None:
            return
        try:
            if self._fallback_guard.is_repeat("wa-fallback", message):
                return
            import json as _json
            from events.paths import telegram_topics_path
            from cron.scheduler import _deliver_result
            data = _json.loads(
                Path(telegram_topics_path()).read_text(encoding="utf-8"))
            chat_id = data.get("group_chat_id", "")
            _key, thread_id = resolve_topic_thread(
                data.get("topics", {}), "action_required")
            if not chat_id or not thread_id:
                return
            _deliver_result(
                {"deliver": f"telegram:{chat_id}:{thread_id}",
                 "id": "event-bus", "name": "event-bus"},
                f"📵 WhatsApp unreachable — escalation delivered here instead:\n\n{message}",
                skip_cron_framing=True,
            )
            logger.warning(
                "WhatsAppEscalator: bridge send failed; message delivered "
                "to Telegram action_required as fallback")
        except Exception:
            logger.exception("WhatsAppEscalator: telegram fallback failed")

    def _whatsapp_target(self) -> Dict[str, str]:
        """Render the target field for the reverse-signal payload.

        WHATSAPP_HOME_CHANNEL is the env-bound destination per the
        2026-04-19 hardening memo; record the symbolic name to avoid
        leaking PII (the actual phone number / channel id) into
        audit.jsonl. A future retry-router can resolve the symbolic
        name to a routable channel.
        """
        channel = os.environ.get("WHATSAPP_HOME_CHANNEL", "WHATSAPP_HOME_CHANNEL")
        return {"phone_or_channel": channel}

    def _safe_emit_delivered(self, event: Event, latency_ms: int) -> None:
        """Emit NOTIFICATION_DELIVERED. Swallows all exceptions."""
        try:
            self.bus.emit(
                event_type=EventType.NOTIFICATION_DELIVERED,
                source="whatsapp-escalator",
                payload={
                    "original_event_id": event.event_id,
                    "original_event_type": event.event_type.type_string,
                    "platform": "whatsapp",
                    "target": self._whatsapp_target(),
                    "latency_ms": latency_ms,
                },
                priority=Priority.LOW,
                correlation_id=event.event_id,
                tags=["delivery", "whatsapp"],
            )
        except Exception:
            logger.exception(
                "WhatsAppEscalator: failed to emit NOTIFICATION_DELIVERED "
                "for event %s", event.event_id,
            )

    def _safe_emit_failed(
        self, event: Event, latency_ms: int, exc: Exception,
    ) -> None:
        """Emit NOTIFICATION_FAILED. Swallows all exceptions."""
        try:
            self.bus.emit(
                event_type=EventType.NOTIFICATION_FAILED,
                source="whatsapp-escalator",
                payload={
                    "original_event_id": event.event_id,
                    "original_event_type": event.event_type.type_string,
                    "platform": "whatsapp",
                    "target": self._whatsapp_target(),
                    "latency_ms": latency_ms,
                    "error": {
                        "kind": type(exc).__name__,
                        # Cap so a multi-KB stacktrace doesn't bloat the
                        # bus DB. Full exception remains in the gateway
                        # log via logger.error above.
                        "message": str(exc)[:500],
                    },
                },
                priority=Priority.NORMAL,
                correlation_id=event.event_id,
                tags=["delivery", "whatsapp", "failure"],
            )
        except Exception:
            logger.exception(
                "WhatsAppEscalator: failed to emit NOTIFICATION_FAILED "
                "for event %s", event.event_id,
            )

    def _queue_message(self, message: str) -> None:
        """Queue a message for the morning flush.

        Bounded at ``_MAX_QUEUE_SIZE`` (drop-oldest). Without a cap, a multi-day
        WhatsApp outage grows quiet_queue.json without limit — the flush delivers
        OVER WhatsApp, so while the bridge is down nothing drains and every
        queued alert accumulates (2026-04-30 saw 1814 queued events = 508KB; the
        2026-07-10 bridge outage stranded 105). The newest alerts are the most
        actionable, so overflow drops the oldest and logs the loss."""
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
        if len(queue) > self._MAX_QUEUE_SIZE:
            dropped = len(queue) - self._MAX_QUEUE_SIZE
            queue = queue[-self._MAX_QUEUE_SIZE:]
            logger.warning(
                "WhatsAppEscalator: quiet queue exceeded %d; dropped %d oldest "
                "message(s) — WhatsApp bridge down for an extended period?",
                self._MAX_QUEUE_SIZE, dropped,
            )
        self._queue_path.write_text(json.dumps(queue, indent=2), encoding="utf-8")

    # Maximum body characters per chunk. Sized to keep the full summary
    # (header + body + footer) under WhatsApp's 4096-char per-message text
    # limit, with margin for the "(N/M)" header and the "Details in Telegram"
    # footer. The bridge's express.json() body limit (~100KB by default) is
    # also satisfied by chunks of this size.
    _FLUSH_CHUNK_BODY_LIMIT = 3500

    # Hard cap on queued morning-flush messages (drop-oldest on overflow). Bounds
    # quiet_queue.json growth during an extended WhatsApp outage, when nothing
    # drains (the flush delivers over WhatsApp itself). 500 keeps the file well
    # under ~200KB while preserving a generous window of the most recent alerts.
    _MAX_QUEUE_SIZE = 500

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

    def has_queued_messages(self) -> bool:
        """True if the morning-flush queue currently holds any items.

        Lets the gateway flush trigger distinguish a DRAINED flush from a
        STRANDED one: flush_queue() preserves the queue on delivery failure
        (e.g. the WhatsApp bridge itself down at 7am, as on 2026-07-10 when a
        0/105 flush left the whole overnight queue behind) and returns 0 — which
        is indistinguishable from "nothing to flush" by the return value alone.
        The caller keys its once-per-day gate on this so a failed flush RETRIES
        later instead of burning the day's single attempt. A missing/corrupt
        queue file reads as empty (nothing stranded)."""
        if not self._queue_path.exists():
            return False
        try:
            return bool(json.loads(self._queue_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return False
