"""Event schema definitions for the Hermes Event Bus.

Defines the typed event envelope, event type catalog with default priorities,
and priority levels used for notification routing.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class Priority(Enum):
    """Event priority levels for notification routing.

    Each level has a numeric value for comparison and filtering.
    """

    CRITICAL = ("critical", 40)
    HIGH = ("high", 30)
    NORMAL = ("normal", 20)
    LOW = ("low", 10)

    def __init__(self, label: str, level: int):
        self.label = label
        self.level = level

    @classmethod
    def from_string(cls, value: str) -> "Priority":
        """Parse a priority string, falling back to NORMAL for unknown values."""
        lookup = {p.label: p for p in cls}
        return lookup.get(value.lower(), cls.NORMAL)


class EventType(Enum):
    """Catalog of all event types emitted by the Hermes Event Bus.

    Each member is a tuple of (event_type_string, default_priority).
    """

    # Cron lifecycle
    CRON_STARTED = ("cron_started", Priority.LOW)
    # Off-schedule trigger record — emitted by trigger_job() in cron/jobs.py
    # whenever a caller sets next_run_at = NOW (CLI `hermes cron run`, LLM
    # cronjob tool action="run", HTTP API trigger endpoint). Carries caller
    # + reason + previous/new next_run_at so off-schedule fires can be
    # attributed in postmortems. LOW priority => audit-logger captures it
    # but Telegram/WhatsApp routing leaves it out by default.
    CRON_TRIGGERED = ("cron_triggered", Priority.LOW)
    CRON_COMPLETED = ("cron_completed", Priority.NORMAL)
    CRON_FAILED = ("cron_failed", Priority.HIGH)
    CRON_FAILED_CONSECUTIVE = ("cron_failed_consecutive", Priority.CRITICAL)
    CRON_STALE = ("cron_stale", Priority.HIGH)
    # Added 2026-04-30: emitted by cron/scheduler.py:tick() when a recurring
    # job is fast-forwarded past a missed fire window (gateway downtime
    # exceeded the catch-up grace, OR the job is opted out of fire-once via
    # recovery_policy="skip_only"). Spec: 2026-04-30-cron-restart-catchup-gap-design.md.
    # Distinct from CRON_SKIPPED_DUPLICATE below — that one is the concurrency-
    # guard reject; this one is the gateway-downtime miss.
    CRON_SKIPPED = ("cron_skipped", Priority.HIGH)
    # Cron same-job concurrency guard -- added 2026-04-30 to close the
    # 2026-04-30 sentinel-vip-morning triple-fire (canonical case
    # event_id 4edcb4b1-aa07-4dbb-b799-8af167d4f92e). Emitted by the
    # _in_flight guard in cron/scheduler.py when a duplicate concurrent
    # fire is detected (typically a user-initiated trigger_job racing a
    # tick-scheduled fire). Subscribers should treat as low-priority
    # informational telemetry.  Payload:
    #   job_id, job_name, prior_cron_started_event_id,
    #   prior_elapsed_seconds, reason
    # where reason is one of:
    #   concurrent_fire_blocked      (prior is healthy, still running)
    #   prior_fire_exceeded_timeout  (prior is wedged-but-tracked)
    CRON_SKIPPED_DUPLICATE = ("cron_skipped_duplicate", Priority.LOW)
    # Cron aborted on gateway shutdown -- Guard #1, added 2026-04-30 to
    # close the audit gap surfaced by the sentinel-vip-morning incident
    # (canonical event_id 4edcb4b1-aa07-4dbb-b799-8af167d4f92e). When the
    # gateway shuts down with a cron future still in flight, emit one
    # cron_aborted per still-tracked job so audit.jsonl never accumulates
    # dangling cron_started rows. Subscribers should treat as terminal --
    # CronStaleMonitor must clear _open_jobs / _alerted on receipt, same
    # as cron_completed / cron_failed.  Payload:
    #   job_id, job_name, cron_started_event_id, started_at, aborted_at,
    #   elapsed_seconds, reason
    # where reason is one of:
    #   gateway_shutdown    (gateway is going down with futures in flight)
    #   wallclock_timeout   (HERMES_CRON_HARD_TIMEOUT enforcement)
    # HIGH priority so the abort surfaces in operator alerts rather than
    # being batched into low-priority telemetry.
    CRON_ABORTED = ("cron_aborted", Priority.HIGH)

    # Job discovery & scoring
    JOB_DISCOVERED = ("job_discovered", Priority.NORMAL)
    JOB_SCORED = ("job_scored", Priority.NORMAL)
    JOB_HIGH_SCORE = ("job_high_score", Priority.HIGH)
    JOB_VIP_DISCOVERED = ("job_vip_discovered", Priority.HIGH)

    # Tailoring & applications
    TAILOR_COMPLETED = ("tailor_completed", Priority.NORMAL)
    APPLICATION_READY = ("application_ready", Priority.HIGH)
    APPLICATION_SUBMITTED = ("application_submitted", Priority.HIGH)
    APPLICATION_FAILED = ("application_failed", Priority.CRITICAL)
    APPLICATION_BLOCKED = ("application_blocked", Priority.CRITICAL)

    # Pipeline tracking
    STAGE_TRANSITION = ("stage_transition", Priority.NORMAL)
    INTERVIEW_SIGNAL = ("interview_signal", Priority.CRITICAL)
    OFFER_SIGNAL = ("offer_signal", Priority.CRITICAL)
    FOLLOWUP_DUE = ("followup_due", Priority.HIGH)

    # System
    DIGEST_GENERATED = ("digest_generated", Priority.LOW)
    GATEWAY_HEALTH = ("gateway_health", Priority.HIGH)
    AGENT_ERROR = ("agent_error", Priority.HIGH)
    MEMORY_CONSOLIDATED = ("memory_consolidated", Priority.LOW)
    SKILL_EVOLVED = ("skill_evolved", Priority.LOW)
    MAILBOX_MESSAGE = ("mailbox_message", Priority.LOW)
    USER_INBOUND_MESSAGE = ("user_inbound_message", Priority.NORMAL)

    # Security
    SECRET_DETECTED = ("secret_detected", Priority.HIGH)

    # Phase B Stage-3 iter2 (HITL + apply + tracker -> Postgres)
    APPROVAL_REQUEST = ("approval_request", Priority.HIGH)
    APPLY_PACKET = ("apply_packet", Priority.NORMAL)

    # Tailor structured iteration event — added 2026-04-29 (plan
    # 2026-04-29-tailor-structured-iteration-event). Emitted by the
    # cron-wrapper after `jobflow-tailor` runs, carrying counts +
    # categorical reason so the Critic + Watchdog can distinguish
    # "nothing to do" from "something is broken" — a discrimination
    # the existing `[SILENT]` marker cannot make. Bus-only metadata;
    # the human-readable Telegram pathway is unchanged.
    TAILOR_ITERATION = ("tailor_iteration", Priority.LOW)

    # Generic per-agent iteration summary event — added 2026-04-30 to
    # extend the TAILOR_ITERATION pattern to every cron-driven agent
    # (Scout, Matcher, Applier, Tracker, Sentinel, Critic, Curator,
    # Watchdog, Scribe, DevFlow). Each agent's SOUL.md contracts it to
    # emit an <AGENT_ITERATION_JSON>{...}</AGENT_ITERATION_JSON> block
    # at the end of every cron run; the cron-wrapper extracts and
    # emits this event. Payload schema:
    #   agent (str, required)     — canonical agent name (e.g. "scout")
    #   summary (str, required)   — one-line human-readable text
    #   counters (dict, optional) — agent-specific metrics
    #   anomalies (list, optional)— flagged issues for Critic triage
    #   reason (str, optional)    — categorical reason like "no_work"
    # Telegram routing is per-agent (see AGENT_TOPIC_MAP in
    # telegram_notifier.py) so jobflow agents land in jobflow_firehose,
    # platform agents in their own topics. LOW priority => batched 5-min
    # coalescing window keeps volume bounded.
    AGENT_ITERATION = ("agent_iteration", Priority.LOW)

    # Phase C iter2 (Critic proposals routed to WhatsApp/Telegram)
    CRITIC_PROPOSAL = ("critic_proposal", Priority.NORMAL)
    # Critic auto-apply event — added 2026-04-29 (consolidation phase C).
    # Emitted by auto_applier.apply_decision after a successful mutation +
    # changelog write. Existing TOPIC_ROUTING in telegram_notifier.py already
    # routes critic_auto_applied -> critic_proposals topic; this enum entry
    # makes the event actually emittable via EventType.from_string().
    CRITIC_AUTO_APPLIED = ("critic_auto_applied", Priority.NORMAL)

    # Watchdog signals — added 2026-04-25 (iter5).
    # Previously these were emitted via _emit_event()'s AGENT_ERROR fallback
    # when the requested EventType didn't exist, with the real type stuffed
    # into payload.watchdog_type. That caused the 2026-04-24 cluster-feedback
    # flood (watchdog detected its own AGENT_ERROR emissions as a cluster ->
    # emitted more AGENT_ERRORs -> next sweep saw a bigger cluster -> ...).
    # Promoting them to first-class EventType members removes the
    # source=watchdog hack downstream.
    WATCHDOG_TICK = ("watchdog_tick", Priority.LOW)
    WATCHDOG_PROBE_TRANSITION = ("watchdog_probe_transition", Priority.HIGH)
    WATCHDOG_BURST = ("watchdog_burst", Priority.HIGH)
    WATCHDOG_SILENCE_ALERT = ("watchdog_silence_alert", Priority.HIGH)
    WATCHDOG_RECOVERED = ("watchdog_recovered", Priority.NORMAL)
    WATCHDOG_SELF_DEGRADED = ("watchdog_self_degraded", Priority.HIGH)
    # Once-per-day aggregate health heartbeat — added 2026-04-30. Diego's
    # B9 visibility-restoration ask: per-failure events already cover "fire
    # when something breaks"; the missing half is a 7am ET heartbeat with
    # aggregate probe health (probes_total / healthy / degraded / down /
    # escalations_24h / stale_probes) so a quiet feed reads as "Watchdog
    # alive, all green" instead of "Watchdog might be dead." NORMAL priority
    # (not HIGH) keeps the heartbeat out of WhatsApp escalation tiers; the
    # ``digest_only`` verbosity mode in TelegramNotifier passes it through
    # alongside HIGH+ failure-fires for operators who want both without LOW
    # chatter. Emit logic lives in ~/.hermes/profiles/watchdog/workspace/
    # watchdog_sweep.py (snapshot-anchored to last_daily_summary_emitted).
    WATCHDOG_DAILY = ("watchdog_daily", Priority.NORMAL)
    AGENT_FAILURE_CLUSTER = ("agent_failure_cluster", Priority.HIGH)

    # Curator nightly consolidation -- added 2026-04-26.
    # Emitted by curator.orchestrator after backfill / nightly delta
    # passes. Consumed by Scribe (morning digest) and the memory-writer
    # subscriber for cursor advance.
    CURATOR_DAILY = ("curator_daily", Priority.NORMAL)

    # DevFlow bridge -- added 2026-04-26.
    # Emitted by the devflow profile (see ~/.hermes/profiles/devflow/SOUL.md
    # emit-hooks section) and consumed by ~/.hermes/bridges/hermes_to_devflow.py
    # which projects them into DevFlow Postgres so Mission Control UI :3040
    # reflects live agent activity.
    DEVFLOW_RUN_STARTED = ("devflow.run_started", Priority.NORMAL)
    DEVFLOW_RUN_COMPLETED = ("devflow.run_completed", Priority.NORMAL)
    DEVFLOW_APPROVAL_REQUESTED = ("devflow.approval_requested", Priority.HIGH)
    DEVFLOW_TRACE_SNAPSHOT = ("devflow.trace_snapshot", Priority.LOW)

    # DevFlow PR + build telemetry -- added 2026-04-30 (visibility-restoration
    # B11 item 2-3). Surfaces SDLC activity in the devflow_firehose and
    # devflow_decisions Telegram topics so Mission Control reflects the
    # software-delivery side of Hermes, not just bridge ticks. Producers
    # are deferred -- see events/producers/devflow_pr_build.py for emitter
    # helpers any future poller / webhook receiver / manual trigger can
    # call. Spec at docs/superpowers/specs/2026-04-30-devflow-pr-build-events.md.
    DEVFLOW_PR_OPENED = ("devflow.pr_opened", Priority.NORMAL)
    DEVFLOW_PR_MERGED = ("devflow.pr_merged", Priority.HIGH)
    DEVFLOW_PR_CLOSED = ("devflow.pr_closed", Priority.NORMAL)
    DEVFLOW_PR_REVIEW_REQUESTED = ("devflow.pr_review_requested", Priority.HIGH)
    DEVFLOW_BUILD_STARTED = ("devflow.build_started", Priority.LOW)
    DEVFLOW_BUILD_SUCCEEDED = ("devflow.build_succeeded", Priority.NORMAL)
    DEVFLOW_BUILD_FAILED = ("devflow.build_failed", Priority.HIGH)

    # Notification delivery reverse-signal — added 2026-04-30. The bus
    # is one-way today: events emit -> telegram_notifier / whatsapp_escalator
    # deliver via their adapters -> no signal flows back. These two types
    # close the loop so audit-logger, a future delivery dashboard, and a
    # future retry-router can see whether each notification reached the
    # user. Producers: the two delivery subscribers' _deliver() methods,
    # wrapped in try/except so a downstream emit failure never breaks the
    # upstream delivery. Cycle prevention: subscribers MUST NOT consume
    # their own delivery events — see _NEVER_CONSUME guards in each
    # subscriber's handle(). Spec at
    # docs/superpowers/specs/2026-04-30-notification-delivered-design.md.
    #   NOTIFICATION_DELIVERED — LOW so it batches in the audit layer
    #   without flooding watchdog_alerts; carries original_event_id +
    #   platform + target + latency_ms.
    #   NOTIFICATION_FAILED — NORMAL so it surfaces in operator alerts
    #   (digest_only verbosity passes it through alongside HIGH+ failures);
    #   carries the same fields plus error.kind / error.message.
    NOTIFICATION_DELIVERED = ("notification_delivered", Priority.LOW)
    NOTIFICATION_FAILED = ("notification_failed", Priority.NORMAL)

    def __init__(self, type_string: str, default_priority: Priority):
        self.type_string = type_string
        self.default_priority = default_priority

    @classmethod
    def from_string(cls, value: str) -> Optional["EventType"]:
        """Look up an EventType by its string name. Returns None if not found."""
        lookup = {et.type_string: et for et in cls}
        return lookup.get(value.lower())


@dataclass
class Event:
    """A single typed event in the Hermes Event Bus.

    Events are the universal unit of communication between producers
    (cron jobs, agents, health monitors) and subscribers (Telegram notifier,
    WhatsApp escalator, memory writer, etc.).
    """

    event_id: str
    event_type: EventType
    source: str
    timestamp: str  # ISO8601 UTC
    priority: Priority
    payload: Dict[str, Any]
    correlation_id: Optional[str] = None
    job_id: Optional[str] = None
    tags: List[str] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        event_type: EventType,
        source: str,
        payload: Dict[str, Any],
        priority: Optional[Priority] = None,
        correlation_id: Optional[str] = None,
        job_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> "Event":
        """Create a new event with auto-generated ID and timestamp."""
        return cls(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            source=source,
            timestamp=datetime.now(timezone.utc).isoformat(),
            priority=priority or event_type.default_priority,
            payload=payload,
            correlation_id=correlation_id,
            job_id=job_id,
            tags=tags or [],
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.type_string,
            "source": self.source,
            "timestamp": self.timestamp,
            "priority": self.priority.label,
            "payload": self.payload,
            "correlation_id": self.correlation_id,
            "job_id": self.job_id,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        """Deserialize from a dict (e.g., from SQLite JSON or audit log)."""
        event_type = EventType.from_string(data["event_type"])
        if event_type is None:
            raise ValueError(f"Unknown event type: {data['event_type']}")
        return cls(
            event_id=data["event_id"],
            event_type=event_type,
            source=data["source"],
            timestamp=data["timestamp"],
            priority=Priority.from_string(data["priority"]),
            payload=data.get("payload", {}),
            correlation_id=data.get("correlation_id"),
            job_id=data.get("job_id"),
            tags=data.get("tags", []),
        )
