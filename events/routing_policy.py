"""Attention-first routing policy — the single source of truth for
"where does this event go, and does it page the operator's phone?"

Design: docs/superpowers/specs/2026-07-18-notification-routing-v3-design.md
(in the ~/.hermes parent repo). Both delivery subscribers (TelegramNotifier,
WhatsAppEscalator) consult classify(); neither keeps its own event table.
That kills the drift that produced the pre-v3 mess: five independently
patched decision points (TOPIC_ROUTING + resolve_target overrides +
CROSS_POST_TO_ALERTS + the escalator's _TIER_BY_EVENT + ScribeRealtime
narration) delivering one interview_signal to three Telegram topics.

Model
-----
Every event gets an attention class — the operator-first question:

  ACT   Diego must do something (approve, decide, unblock, respond).
        Always lands in the cross-domain ``action_required`` topic and
        always escalates to WhatsApp.
  WARN  Something is broken/degraded. Lands in ``watchdog_alerts`` (or
        ``security_and_system`` for the security domain); escalates to
        WhatsApp only at CRITICAL or via an explicit per-type override.
  INFO  Meaningful progress worth seeing today. Domain topic.
  TRACE Telemetry. Domain firehose, batched below HIGH priority.

Invariants (enforced by tests/events/test_routing_policy.py):
  * every EventType classifies (no silent "system" catch-all);
  * ACT routes to action_required and escalates (immediate|urgent);
  * effective priority is clamped: ACT >= HIGH, WARN >= NORMAL;
  * INFO/TRACE never escalate unless explicitly flagged phone-worthy;
  * only TRACE routes may batch.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Collection, Dict, Optional, Tuple

from events.outcomes import (
    OutcomeState,
    OutcomeVerdict,
    evaluate_outcome,
    legacy_cron_output_is_failure,
)
from events.producers.agent_source_mapping import canonical_agent_source
from events.schema import Event, EventType, Priority

logger = logging.getLogger(__name__)


class Attention(Enum):
    ACT = "act"
    WARN = "warn"
    INFO = "info"
    TRACE = "trace"


# WhatsApp tier labels (string form of the escalator's EscalationTier —
# strings here so the escalator can import this module without a cycle).
WA_IMMEDIATE = "immediate"
WA_URGENT = "urgent"
WA_IMPORTANT = "important"


@dataclass(frozen=True)
class Route:
    attention: Attention
    topic_key: str
    priority: Priority          # effective (clamped) priority
    wa_tier: Optional[str]      # immediate | urgent | important | None
    batch: bool                 # True only for TRACE below HIGH
    verdict: OutcomeVerdict     # computed once; subscribers do not reinterpret


# Topic keys. action_required and agents_memory are new in v3; consumers
# fall back through TOPIC_ALIASES when topics.json predates the cutover.
ACTION_REQUIRED = "action_required"
ALERTS = "watchdog_alerts"
SECURITY = "security_and_system"
JOBFLOW = "jobflow_firehose"
DEVFLOW = "devflow_firehose"
CRITIC = "critic"
AGENTS_MEMORY = "agents_memory"
DAILY_BRIEF = "scribe_daily"
OPS_TRACE = "cron_firehose"

# key → fallback key when topics.json lacks the primary (old config with
# new code, or new config with old code — both directions stay routable).
TOPIC_ALIASES: Dict[str, str] = {
    ACTION_REQUIRED: "jobflow_decisions",   # same thread 9637
    CRITIC: "critic_proposals",             # compatibility until topic creation
    AGENTS_MEMORY: "critic_proposals",      # temporary legacy config fallback
    OPS_TRACE: ALERTS,                      # pre-2026-07-11 degrade, kept
}


@dataclass(frozen=True)
class _Spec:
    attention: Attention
    topic_key: str
    # "auto" derives from the (attention, priority) contract; an explicit
    # tier label pins it; "none" suppresses for INFO/WARN (never for ACT).
    wa: str = "auto"
    # Per-type floor applied on top of the class clamp (e.g. a detected
    # secret must stay IMMEDIATE-grade even though its default is HIGH).
    priority_floor: Optional[Priority] = None
    # Symmetric counterpart: a per-type CEILING on the effective priority.
    # Added 2026-08-19 for AGENT_NOTE, whose producer is arbitrary agent code
    # — capping at HIGH is what makes "an agent note never pages" true by
    # construction rather than by convention, because _derive_wa escalates a
    # WARN route only at CRITICAL. Belongs here, with the routing data, for
    # the same reason priority_floor does: it is a property of the type, not
    # a branch in the derivation.
    priority_cap: Optional[Priority] = None


_E = EventType
_POLICY: Dict[EventType, _Spec] = {
    # ----- cron lifecycle → TRACE ops firehose ------------------------
    _E.CRON_STARTED: _Spec(Attention.TRACE, OPS_TRACE),
    _E.CRON_TRIGGERED: _Spec(Attention.TRACE, OPS_TRACE),
    _E.CRON_PAUSED: _Spec(Attention.TRACE, OPS_TRACE),
    _E.CRON_RESUMED: _Spec(Attention.TRACE, OPS_TRACE),
    # Asymmetric, Diego 2026-08-26 -- see the EventType comment. Arming a
    # barrier is routine and stays in the firehose; LIFTING one removes
    # protection and is pushed to alerts, because a barrier lift going
    # unnoticed is precisely what cost 2026-08-26.
    _E.CRON_BARRIER_SET: _Spec(Attention.TRACE, OPS_TRACE),
    _E.CRON_BARRIER_CLEARED: _Spec(Attention.WARN, ALERTS),
    _E.CRON_COMPLETED: _Spec(Attention.TRACE, OPS_TRACE),   # content sniffer may upgrade
    _E.CRON_SKIPPED: _Spec(Attention.TRACE, OPS_TRACE),
    _E.CRON_SKIPPED_DUPLICATE: _Spec(Attention.TRACE, OPS_TRACE),
    _E.CRON_SKIPPED_MIN_INTERVAL: _Spec(Attention.TRACE, OPS_TRACE),
    # cron failures → WARN alerts (single jobflow-pipeline failures demote
    # to the jobflow domain topic via _jobflow_demotion below; consecutive
    # failures are systemic and always stay on alerts + page).
    _E.CRON_FAILED: _Spec(Attention.WARN, ALERTS),
    _E.CRON_FAILED_CONSECUTIVE: _Spec(Attention.WARN, ALERTS),  # CRITICAL → urgent
    _E.CRON_STALE: _Spec(Attention.WARN, ALERTS),
    # ----- jobflow domain --------------------------------------------
    _E.JOB_DISCOVERED: _Spec(Attention.TRACE, JOBFLOW),
    _E.JOB_SCORED: _Spec(Attention.TRACE, JOBFLOW),
    _E.JOB_HIGH_SCORE: _Spec(Attention.INFO, JOBFLOW),      # ≥9.0 → phone (hook)
    _E.JOB_VIP_DISCOVERED: _Spec(Attention.INFO, JOBFLOW),
    _E.TAILOR_COMPLETED: _Spec(Attention.TRACE, JOBFLOW),
    _E.TAILOR_ITERATION: _Spec(Attention.TRACE, JOBFLOW),
    _E.STAGE_TRANSITION: _Spec(Attention.INFO, JOBFLOW),
    _E.APPLICATION_SUBMITTED: _Spec(Attention.INFO, JOBFLOW),
    _E.APPLICATION_FAILED: _Spec(Attention.WARN, ALERTS),   # CRITICAL → urgent
    # ----- operator decisions → ACT ----------------------------------
    _E.APPROVAL_REQUEST: _Spec(Attention.ACT, ACTION_REQUIRED),
    _E.APPLICATION_READY: _Spec(Attention.ACT, ACTION_REQUIRED),
    _E.APPLICATION_BLOCKED: _Spec(Attention.ACT, ACTION_REQUIRED),
    _E.APPLY_PACKET: _Spec(Attention.ACT, ACTION_REQUIRED),
    _E.INTERVIEW_SIGNAL: _Spec(Attention.ACT, ACTION_REQUIRED),
    _E.OFFER_SIGNAL: _Spec(Attention.ACT, ACTION_REQUIRED),
    _E.FOLLOWUP_DUE: _Spec(Attention.ACT, ACTION_REQUIRED),
    _E.TRACKER_PARTIAL_BACKLOG: _Spec(Attention.ACT, ACTION_REQUIRED),
    _E.DEVFLOW_APPROVAL_REQUESTED: _Spec(Attention.ACT, ACTION_REQUIRED),
    _E.DEVFLOW_PR_REVIEW_REQUESTED: _Spec(Attention.ACT, ACTION_REQUIRED),
    # Security incidents need Diego's hands (rotate/verify) — ACT, and
    # floored to CRITICAL so they page IMMEDIATE (2026-07-11 operator
    # request preserved). credential_loss already defaults CRITICAL.
    _E.SECRET_DETECTED: _Spec(
        Attention.ACT, ACTION_REQUIRED, priority_floor=Priority.CRITICAL),
    _E.CREDENTIAL_LOSS: _Spec(Attention.ACT, ACTION_REQUIRED),
    # ----- devflow domain --------------------------------------------
    _E.DEVFLOW_RUN_STARTED: _Spec(Attention.TRACE, DEVFLOW),
    _E.DEVFLOW_RUN_COMPLETED: _Spec(Attention.TRACE, DEVFLOW),
    _E.DEVFLOW_TRACE_SNAPSHOT: _Spec(Attention.TRACE, DEVFLOW),
    _E.DEVFLOW_PR_OPENED: _Spec(Attention.INFO, DEVFLOW),
    _E.DEVFLOW_PR_MERGED: _Spec(Attention.INFO, DEVFLOW),
    _E.DEVFLOW_PR_CLOSED: _Spec(Attention.TRACE, DEVFLOW),
    _E.DEVFLOW_BUILD_STARTED: _Spec(Attention.TRACE, DEVFLOW),
    _E.DEVFLOW_BUILD_SUCCEEDED: _Spec(Attention.TRACE, DEVFLOW),
    # Broken build = WARN; phone-worthy by operator request 2026-07-11.
    _E.DEVFLOW_BUILD_FAILED: _Spec(Attention.WARN, ALERTS, wa=WA_URGENT),
    # ----- DevFlow Delegation Plane lifecycle (2026-08-06) ------------
    # Control-plane telemetry for delegated work. New work and human-facing
    # merge/deploy gates are INFO in the DevFlow firehose; flood-control
    # outcomes (duplicate/suppressed/declined) are TRACE so a flapping
    # detector batched-collapses instead of paging. merge_pending is INFO,
    # not ACT: during shadow mode EVERY request parks there by design.
    # auto_merged is WARN because an autonomous merge must stand out.
    # deploy_failed mirrors build_failed: WARN alerts + urgent WhatsApp.
    _E.DEVFLOW_WORK_REQUESTED: _Spec(Attention.INFO, DEVFLOW),
    _E.DEVFLOW_WORK_TRIAGED: _Spec(Attention.TRACE, DEVFLOW),
    _E.DEVFLOW_WORK_PLANNED: _Spec(Attention.TRACE, DEVFLOW),
    _E.DEVFLOW_WORK_DUPLICATE: _Spec(Attention.TRACE, DEVFLOW),
    _E.DEVFLOW_WORK_DECLINED: _Spec(Attention.TRACE, DEVFLOW),
    _E.DEVFLOW_WORK_SUPPRESSED: _Spec(Attention.TRACE, DEVFLOW),
    _E.DEVFLOW_MERGE_PENDING: _Spec(Attention.INFO, DEVFLOW),
    _E.DEVFLOW_MERGED: _Spec(Attention.INFO, DEVFLOW),
    _E.DEVFLOW_AUTO_MERGED: _Spec(Attention.WARN, DEVFLOW),
    _E.DEVFLOW_DEPLOY_STARTED: _Spec(Attention.INFO, DEVFLOW),
    _E.DEVFLOW_DEPLOYED: _Spec(Attention.INFO, DEVFLOW),
    _E.DEVFLOW_DEPLOY_FAILED: _Spec(Attention.WARN, ALERTS, wa=WA_URGENT),
    # ----- system health → WARN alerts -------------------------------
    _E.GATEWAY_HEALTH: _Spec(Attention.WARN, ALERTS),       # hook: up → INFO
    _E.GATEWAY_STARTED: _Spec(Attention.INFO, ALERTS),
    _E.GATEWAY_STOPPED: _Spec(Attention.WARN, ALERTS),
    _E.AGENT_ERROR: _Spec(Attention.WARN, ALERTS, wa=WA_URGENT),  # cluster-gated in escalator
    _E.AGENT_FAILURE_CLUSTER: _Spec(Attention.WARN, ALERTS, wa=WA_URGENT),
    _E.AGENT_LOOP_FAULT: _Spec(Attention.WARN, ALERTS),
    # Off the alerts topic (2026-08-14, operator pref): the disk_low WARN
    # stage is an early warning that must stay legible, and alerts already
    # carries ~112 delivered msgs/day (gateway_health 634, watchdog_burst 409
    # per 30d) — the signal was indistinguishable from the noise. The disk
    # axis pages via the disk_critical hook below, which forces
    # action_required regardless of this line.
    _E.RESOURCE_PRESSURE: _Spec(Attention.WARN, SECURITY),
    _E.CODE_DRIFT: _Spec(Attention.WARN, ALERTS),           # hook: resolved → INFO
    # Base case is the common one: the limit was hit and a fallback absorbed
    # it, which is degraded-but-working. The conditional hook below upgrades
    # to ACT when nothing absorbed it. Deliberately NOT ACT by default —
    # ACT always pages WhatsApp, and paging for every successfully-diverted
    # 429 would make this feature intolerable within a week.
    _E.MODEL_RATE_LIMITED: _Spec(Attention.WARN, ALERTS),
    # Model override audit trail (Phase 2 task 5) — a RECORD of a deliberate
    # operator action (a Telegram tap diverting or un-diverting traffic), not
    # a decision or a fault: INFO on watchdog_alerts, never WARN/ACT, so it
    # never pages and never lands in Action Required. wa="none" for the same
    # reason WATCHDOG_SILENCE_ALERT does — INFO/WARN may suppress WhatsApp
    # entirely; ACT alone can never be silenced.
    _E.MODEL_OVERRIDE_SET: _Spec(Attention.INFO, ALERTS, wa="none"),
    # The boot report fires only when a boot broke something (2026-07-27):
    # degraded host, not an operator action. HIGH default + WARN means it
    # survives significant_only but does not page unless emitted CRITICAL.
    _E.BOOT_SUMMARY: _Spec(Attention.WARN, ALERTS),
    _E.WATCHDOG_TICK: _Spec(Attention.TRACE, ALERTS),
    _E.WATCHDOG_PROBE_TRANSITION: _Spec(Attention.WARN, ALERTS, wa=WA_URGENT),
    _E.WATCHDOG_BURST: _Spec(Attention.WARN, ALERTS, wa=WA_URGENT),
    # Silence alerts are frequently false (memory: watchdog_silence_false_
    # alarms — "cron emission dark") and recoveries are closure telemetry:
    # both are TRACE (batched into Alerts, calm dot, never paged). Real
    # deadness is covered by cron_failed/cron_stale/gateway_health.
    _E.WATCHDOG_SILENCE_ALERT: _Spec(Attention.TRACE, ALERTS, wa="none"),
    _E.WATCHDOG_RECOVERED: _Spec(Attention.TRACE, ALERTS),
    _E.WATCHDOG_SELF_DEGRADED: _Spec(Attention.WARN, ALERTS),  # hook: blackout → security
    _E.WATCHDOG_DAILY: _Spec(Attention.INFO, ALERTS),
    # A container burning restarts is broken even when the tray currently reads
    # GREEN — laptop-monitor's churn verdict is a per-pass delta that self-clears
    # 600s after the last restart, so bursts recover before anyone looks (the
    # 2026-08-12 hindsight-app postmortem). WARN + urgent so it survives
    # significant_only and reaches the phone; never batched.
    _E.CONTAINER_CRASH_LOOP: _Spec(Attention.WARN, ALERTS, wa=WA_URGENT),
    _E.BACKEND_CONTRACT_DRIFT: _Spec(Attention.WARN, SECURITY),
    # ----- critic domain ---------------------------------------------
    _E.CRITIC_PROPOSAL: _Spec(Attention.INFO, CRITIC),
    _E.CRITIC_AUTO_APPLIED: _Spec(Attention.INFO, CRITIC),
    # ----- agents & memory domain ------------------------------------
    _E.CURATOR_DAILY: _Spec(Attention.INFO, AGENTS_MEMORY),
    _E.MEMORY_CONSOLIDATED: _Spec(Attention.INFO, AGENTS_MEMORY),
    _E.SKILL_EVOLVED: _Spec(Attention.INFO, AGENTS_MEMORY),
    # Arbitrary agent-authored prose. This entry is only the DEFAULT: the
    # caller declares the class in payload["attention"] (hook below), because
    # the type carries no semantics for the policy to read. The cap is the
    # load-bearing half — with priority_cap=HIGH, _derive_wa returns None for
    # every reachable (attention, priority) pair, so no payload an agent can
    # author reaches WhatsApp. See the AGENT_NOTE clamp after the human-gate
    # block for why the ceiling is enforced there and not here.
    _E.AGENT_NOTE: _Spec(Attention.INFO, AGENTS_MEMORY,
                         priority_cap=Priority.HIGH),
    # ----- daily brief / conversational ------------------------------
    _E.DIGEST_GENERATED: _Spec(Attention.TRACE, DAILY_BRIEF),
    _E.MAILBOX_MESSAGE: _Spec(Attention.TRACE, DAILY_BRIEF),  # hook: NOTIFICATION `to:`
    _E.USER_INBOUND_MESSAGE: _Spec(Attention.INFO, DAILY_BRIEF),
    # ----- agent iteration: topic per agent (hook) --------------------
    _E.AGENT_ITERATION: _Spec(Attention.TRACE, JOBFLOW),
    # ----- delivery reverse-signals: bus-only (subscribers guard),
    # routed for coverage so a guard regression surfaces on alerts.
    _E.NOTIFICATION_DELIVERED: _Spec(Attention.TRACE, ALERTS),
    _E.NOTIFICATION_FAILED: _Spec(Attention.TRACE, ALERTS),
}

# AGENT_ITERATION per-agent topics (migrated from telegram_notifier).
AGENT_TOPIC_MAP: Dict[str, str] = {
    'scout': JOBFLOW, 'matcher': JOBFLOW, 'matcher-shadow': JOBFLOW,
    'tailor': JOBFLOW, 'applier': JOBFLOW, 'tracker': JOBFLOW,
    'sentinel': JOBFLOW, 'cv-handler': JOBFLOW, 'notifier': JOBFLOW,
    'main': JOBFLOW,
    'devflow': DEVFLOW, 'devflow-standup': DEVFLOW, 'devflow-bridge': DEVFLOW,
    'critic': CRITIC, 'curator': AGENTS_MEMORY,
    'watchdog': ALERTS, 'scribe': DAILY_BRIEF,
}

# AGENT_NOTE caller-declared attention class → the lane it means.
# "act" is accepted here rather than dropped so the clamp below can LOG a
# rejected escalation attempt; silently mapping it to INFO would hide a
# producer that believes it is paging.
_AGENT_NOTE_ATTENTION: Dict[str, Attention] = {
    "info": Attention.INFO,
    "warn": Attention.WARN,
    "trace": Attention.TRACE,
    "act": Attention.ACT,
}

# Class → topic for AGENT_NOTE, following the v3 convention. ACT maps to
# ALERTS because the clamp below rewrites ACT to WARN; it never survives.
_AGENT_NOTE_TOPIC: Dict[Attention, str] = {
    Attention.INFO: AGENTS_MEMORY,
    Attention.WARN: ALERTS,
    Attention.TRACE: OPS_TRACE,
    Attention.ACT: ALERTS,
}

# Single failures from JobFlow pipeline agents demote to the domain topic
# (2026-07-16 operator request, preserved). Systemic signals — clusters,
# consecutive failures — deliberately do NOT demote in v3: they are
# page-worthy and the alert stream they used to drown is now flap-collapsed
# and noop-suppressed.
JOBFLOW_DEMOTE_TYPES = frozenset({_E.AGENT_ERROR, _E.CRON_FAILED, _E.CRON_STALE})
JOBFLOW_PIPELINE_AGENTS = frozenset({
    'applier', 'archiver', 'cv-handler', 'matcher', 'matcher-shadow',
    'notifier', 'scout', 'sentinel', 'tailor', 'tracker',
})

# Plain-success cron runs whose output IS primary job-search signal, not
# ops plumbing — their SUCCEEDED telemetry batches into the JobFlow topic
# instead of the ops firehose (2026-08-06 operator request: JobFlow run-
# summaries were piling into Ops Trace alongside postgres-sync and inbox
# sweeps, producing one oversized mixed batch). NARROW by operator choice:
# discoveries/scores/stage-moves already reach JobFlow as their own domain
# events, so only the run-summary jobs Diego reads move. Shadow/enrich/
# archive/soak/sweeper runs (jobflow-matcher-shadow, jobflow-archiver,
# jobflow-*-soak-recheck, jaum-inbox-sweeper, jobflow-ats-url-resolve) are
# housekeeping and stay in Ops Trace. Matched by RAW job name (event.source
# / payload.job_name), NOT canonical_agent_source — the split is per-job,
# not per-agent (tracker-cycle moves; tracker-followup/weekly do not).
JOBFLOW_CRON_JOBS = frozenset({
    "jobflow-scout",
    "jobflow-matcher",
    "jobflow-tracker-cycle",
    "jaum-daytime-relay",
})

# watchdog_self_degraded reasons meaning "monitoring itself has gone dark"
# → security_and_system (low-traffic, actually seen). Must match
# watchdog_sweep.py's _emit_self_degraded() strings exactly.
STATUS_BLACKOUT_SELF_DEGRADED_REASONS = frozenset({
    'laptop-monitor status.json stale',
    'status.json unreadable',
})

HIGH_SCORE_WA_THRESHOLD = 9.0

# Cron output that means "this run found trouble" even though it arrived as
# cron_completed. The compatibility parser lives in outcomes so presentation
# and routing cannot drift on historical inventory or exit-code syntax.
_CRON_ALERT_SCAN_CHARS = 400

# cron_completed output that ASKS DIEGO SOMETHING — "Reply ALL or...", a y/n
# prompt, an explicit ACTION REQUIRED banner. Reported 2026-07-27: these were
# landing in the ops firehose as telemetry, so a run that stopped to ask a
# question just sat there unanswered.
#
# Scanned over the TAIL, not the head: a prompt is the LAST thing a job
# prints, so _CRON_ALERT_SCAN_CHARS' head window structurally cannot see it.
#
# Deliberately TIGHT. An ACT route ALWAYS escalates to WhatsApp (_derive_wa),
# a tradeoff Diego accepted explicitly — so a false positive costs a phone
# page. Bare "reply"/"needs" in prose must not match; each alternative
# requires the verb AND its object ("Reply ALL", "Needs your approval").
_CRON_ACTION_RE = re.compile(
    r"(?:\bACTION REQUIRED\b"
    r"|\b(?:R|r)eply (?:ALL|YES|NO|STOP|with|to)\b"
    r"|\bREPLY (?:ALL|YES|NO|STOP|WITH|TO)\b"
    r"|\b(?:R|r)espond (?:with|to)\b"
    r"|\b(?:A|a)waiting (?:your )?(?:reply|response|input|approval|decision)\b"
    r"|\bAWAITING (?:YOUR )?(?:REPLY|RESPONSE|INPUT|APPROVAL|DECISION)\b"
    r"|\b(?:N|n)eeds? (?:your )?(?:input|decision|approval|sign-?off)\b"
    r"|\bNEEDS? (?:YOUR )?(?:INPUT|DECISION|APPROVAL|SIGN-?OFF)\b"
    r"|\b(?:P|p)lease (?:reply|respond|confirm|approve|choose|pick)\b"
    r"|[\[(] ?[yY] ?/ ?[nN] ?[\])]"
    r"|[\[(] ?[yY]es ?/ ?[nN]o ?[\])]"
    r")"
)
_CRON_ACTION_SCAN_CHARS = 800

_HUMAN_ACTION_KINDS = frozenset({
    "approval",
    "decision",
    "credential",
    "credits",
    "manual_intervention",
})


_NON_ACTIONABLE_STATES = frozenset({"healthy", "skipped"})


def _probe_actionable(transition: dict) -> bool:
    """A probe change worth attention: a degradation of a non-optional-tier
    probe. Recoveries (→healthy) and probe SKIPS (monitor over budget —
    the "55 probes skipped this pass" storm bursts carry no real state
    change) are not actionable. Missing tier fails open (actionable)."""
    return (
        transition.get("after") not in _NON_ACTIONABLE_STATES
        and transition.get("tier") != "optional"
    )


def _class_floor(attention: Attention) -> Optional[Priority]:
    if attention is Attention.ACT:
        return Priority.HIGH
    if attention is Attention.WARN:
        return Priority.NORMAL
    return None


def _class_cap(attention: Attention) -> Optional[Priority]:
    # TRACE caps at NORMAL: producers historically boosted routine
    # telemetry to HIGH to cross the old significant_only gate (e.g.
    # cron_emitter's >=120-char "meaningful output" boost). v3 placement
    # ignores that hack — telemetry batches and wears a calm dot. The
    # content sniffer hook upgrades genuinely alarming output to WARN
    # BEFORE this cap applies.
    if attention is Attention.TRACE:
        return Priority.NORMAL
    return None


def _clamp(priority: Priority, floor: Optional[Priority]) -> Priority:
    if floor is not None and priority.level < floor.level:
        return floor
    return priority


def _cap(priority: Priority, cap: Optional[Priority]) -> Priority:
    if cap is not None and priority.level > cap.level:
        return cap
    return priority


def cron_output_is_alert(output_summary: str) -> bool:
    """True iff a cron run's output text signals trouble (P9: content can
    outrank type)."""
    return legacy_cron_output_is_failure(
        output_summary,
        limit=_CRON_ALERT_SCAN_CHARS,
    )


def cron_output_is_actionable(output_summary: str) -> bool:
    """True iff a cron run's output text asks the operator for something
    (P9: content can outrank type). Scans the TAIL — a prompt is the last
    thing a job prints — unlike cron_output_is_alert()'s head window."""
    text = str(output_summary or "")[-_CRON_ACTION_SCAN_CHARS:]
    return bool(_CRON_ACTION_RE.search(text))


def structured_human_gate(payload: dict) -> bool:
    """Return whether structured payload evidence proves a human-only gate."""
    return (
        payload.get("action_required") is True
        and payload.get("action_kind") in _HUMAN_ACTION_KINDS
    )


def critic_decision_gate(event_type: EventType, payload: dict) -> bool:
    """Return whether a Critic proposal explicitly requests a user decision."""
    return (
        event_type is EventType.CRITIC_PROPOSAL
        and payload.get("decision_required") is True
    )


def classify(
    event: Event,
    known_topic_keys: Optional[Collection[str]] = None,
) -> Route:
    """Classify an event into its Route. Total: unknown/unmapped types
    surface as WARN on the alerts topic rather than vanishing into the
    group's General thread (pre-v3 ``"system"`` catch-all)."""
    verdict = evaluate_outcome(event)
    spec = _POLICY.get(event.event_type)
    if spec is None:
        # Unrouted type — someone added an EventType without a policy
        # entry (the coverage test should have caught it). Loud, visible.
        spec = _Spec(Attention.WARN, ALERTS)

    attention = spec.attention
    topic_key = spec.topic_key
    wa = spec.wa
    payload = event.payload if isinstance(event.payload, dict) else {}
    et = event.event_type

    # --- conditional hooks (migrated from pre-v3 scattered overrides) ---
    if et == EventType.AGENT_NOTE:
        # The caller declares the attention class; the topic follows from it
        # by the v3 class→lane convention rather than being named directly,
        # so a note cannot be filed into a domain topic where it means
        # nothing. An unrecognised or absent value keeps the INFO default —
        # a typo must not silently promote a note into Alerts.
        attention = _AGENT_NOTE_ATTENTION.get(
            str(payload.get("attention") or "").strip().lower(), Attention.INFO)
        topic_key = _AGENT_NOTE_TOPIC[attention]

    elif et == EventType.AGENT_ITERATION:
        agent = (payload.get('agent') or '').strip().lower()
        topic_key = AGENT_TOPIC_MAP.get(agent, JOBFLOW)

    elif et == EventType.GATEWAY_HEALTH:
        if payload.get("status") == "down":
            wa = WA_URGENT
        else:
            attention = Attention.INFO   # recovery / heartbeat
            wa = "none"

    elif et == EventType.CODE_DRIFT:
        if payload.get("status") == "resolved":
            attention = Attention.INFO   # recovery — closure telemetry
            wa = "none"

    elif et == EventType.MODEL_RATE_LIMITED:
        _outcome = (payload.get("outcome") or "").strip().lower()
        if _outcome in {"chain_exhausted", "no_fallback"}:
            # Nothing absorbed the traffic — runs are failing, not degrading.
            # This is the case that needs a decision, so it pages.
            attention = Attention.ACT
            topic_key = ACTION_REQUIRED
        elif _outcome == "recovered":
            attention = Attention.INFO
            wa = "none"

    elif et == EventType.JOB_HIGH_SCORE:
        try:
            score = float(payload.get("score", 0) or 0)
        except (TypeError, ValueError):
            score = 0.0
        wa = WA_IMPORTANT if score >= HIGH_SCORE_WA_THRESHOLD else "none"

    elif et == EventType.WATCHDOG_PROBE_TRANSITION:
        if not _probe_actionable(payload):
            attention = Attention.TRACE   # recovery / optional-tier: batch
            wa = "none"

    elif et == EventType.WATCHDOG_BURST:
        transitions = [t for t in (payload.get("transitions") or [])
                       if isinstance(t, dict)]
        if transitions and not any(_probe_actionable(t) for t in transitions):
            attention = Attention.TRACE
            wa = "none"

    elif et == EventType.RESOURCE_PRESSURE:
        # Disk is the one resource axis that does NOT self-resolve. Commit,
        # phys and pagefile spikes drain on their own; a filling disk keeps
        # filling until a human frees space, and at zero the box stops working.
        # Verified 2026-08-14: disk_low fired on 11 days since 07-17 -- five of
        # them reaching 0.0 GB free -- and was DELIVERED every time, into the
        # alerts topic alongside ~2,200 watchdog events. Detection was never the
        # problem; the signal was indistinguishable from the noise.
        # Two stages on purpose: disk_low (60 GB) stays a WARN early warning so
        # it is cheap to receive, and only disk_critical (25 GB) becomes ACT ->
        # action_required + WhatsApp. Paging on the 60 GB axis would fire every
        # cooldown for a whole day of ordinary Docker churn and teach exactly
        # the alert-blindness that caused this.
        reasons = payload.get("reasons")
        if isinstance(reasons, (list, tuple)) and "disk_critical" in reasons:
            attention = Attention.ACT
            topic_key = ACTION_REQUIRED

    elif et == EventType.WATCHDOG_SELF_DEGRADED:
        if payload.get("reason", "") in STATUS_BLACKOUT_SELF_DEGRADED_REASONS:
            topic_key = SECURITY

    elif et == EventType.CRON_COMPLETED:
        # Actionable is checked FIRST: a run that both errored AND asked a
        # question needs a human, not an alert-thread post-mortem. ACT's
        # class floor (>= HIGH) and mandatory WhatsApp escalation follow.
        if cron_output_is_actionable(payload.get("output_summary", "")):
            attention = Attention.ACT
            topic_key = ACTION_REQUIRED
        elif cron_output_is_alert(payload.get("output_summary", "")):
            attention = Attention.WARN
            topic_key = ALERTS
            # A RED/FAILED finding must read as an incident (amber dot),
            # not blend in at the cron default NORMAL.
            spec = dataclasses.replace(spec, priority_floor=Priority.HIGH)
        elif (
            event.source in JOBFLOW_CRON_JOBS
            or payload.get("job_name") in JOBFLOW_CRON_JOBS
        ):
            # Plain-success summaries from the narrow set of JobFlow jobs
            # Diego reads belong with JobFlow signal, not ops telemetry.
            # Keep TRACE semantics: calm dot, batched, never phone-paged.
            topic_key = JOBFLOW

    elif et == EventType.MAILBOX_MESSAGE:
        if payload.get("message_type") == "NOTIFICATION":
            requested_to = payload.get("to", "")
            if known_topic_keys and requested_to in known_topic_keys:
                topic_key = requested_to
            else:
                topic_key = DAILY_BRIEF
            attention = Attention.INFO

    # --- outcome + structured human-gate policy ------------------------
    # Intrinsically actionable types stay ACT even when their operation
    # failed: the outstanding approval/decision still belongs to Diego.
    # Structured gates are authoritative for wrappers. Otherwise a failed or
    # degraded INFO/TRACE wrapper promotes to WARN/Alerts.
    if structured_human_gate(payload) or critic_decision_gate(et, payload):
        attention = Attention.ACT
        topic_key = ACTION_REQUIRED
    elif (
        verdict.state in {OutcomeState.FAILED, OutcomeState.DEGRADED}
        and attention in {Attention.INFO, Attention.TRACE}
    ):
        attention = Attention.WARN
        topic_key = ALERTS

    # AGENT_NOTE ceiling. THIS MUST STAY AFTER THE HUMAN-GATE BLOCK ABOVE.
    #
    # structured_human_gate() promotes ANY event carrying action_required +
    # a recognised action_kind straight to ACT/ACTION_REQUIRED, and _derive_wa
    # then always escalates an ACT route. AGENT_NOTE payloads are written by
    # arbitrary agent code, so those two keys would be a self-service phone
    # page. Clamping here — after every promotion path has run — is what makes
    # the ceiling hold; moved into the conditional-hooks block it would be
    # silently overridden and this file would still read as if it were safe.
    # tests/events/test_routing_policy.py pins both the clamp and the
    # placement.
    if et is EventType.AGENT_NOTE and attention is Attention.ACT:
        logger.warning(
            "agent_note from %s requested ACT; clamped to WARN on %s "
            "(agent notes never page — see the 2026-08-19 AGENT_NOTE spec)",
            event.source, ALERTS,
        )
        attention = Attention.WARN
        topic_key = ALERTS

    # Preserve routing-v3's single-failure domain demotion for explicit
    # failure event types. Verdict-promoted wrappers are deliberately absent
    # from JOBFLOW_DEMOTE_TYPES and therefore remain in Alerts.
    if et in JOBFLOW_DEMOTE_TYPES and _is_jobflow_source(event):
        topic_key = JOBFLOW

    # --- effective priority: class/outcome/per-type floor + class cap ----
    priority = _clamp(event.priority, _class_floor(attention))
    priority = _clamp(priority, verdict.priority_floor)
    priority = _clamp(priority, spec.priority_floor)
    # A CRITICAL-tier probe that has actually FAILED is a red alert: 160ed2d477
    # made it verdict FAILED, but marker_for_verdict reserves the red dot for
    # Priority.CRITICAL and these arrive at HIGH, so a :8642 outage still wore
    # the same amber as a routine warning. Raise the PRIORITY rather than
    # special-casing the marker, so the dot follows from the verdict contract.
    #
    # ESCALATION IS DELIBERATELY UNCHANGED. The policy entry pins wa=WA_URGENT
    # and _derive_wa returns an explicit pin untouched, so wa_tier stays
    # "urgent" and quiet hours still QUEUE this rather than breaking through.
    # If a future edit drops that pin, CRITICAL would derive a different tier
    # and a 3am blip would wake the operator; the routing test named
    # ...does_not_become_a_quiet_hours_breakthrough exists to catch exactly that.
    #
    # Scoped three ways on purpose: FAILED only (a recovery must not inherit a
    # red-alert priority), DEGRADED excluded (a partial outage is not a red
    # alert), and tier == "critical" only -- 159 of the 301 real probe
    # transitions on this box are tier=important, so flooring on any tier would
    # repaint most of the feed red and cost the dot its meaning.
    _tier = event.payload.get("tier") if isinstance(event.payload, dict) else None
    if verdict.state is OutcomeState.FAILED and str(_tier or "").strip().lower() == "critical":
        priority = _clamp(priority, Priority.CRITICAL)
    priority = _cap(priority, _class_cap(attention))
    # Per-type ceiling last, so it beats every floor above it — including the
    # verdict floor, which a payload key such as status:"failed" can raise.
    priority = _cap(priority, spec.priority_cap)

    # --- WhatsApp tier ---------------------------------------------------
    wa_tier = _derive_wa(attention, priority, wa)

    batch = attention is Attention.TRACE
    return Route(
        attention=attention,
        topic_key=topic_key,
        priority=priority,
        wa_tier=wa_tier,
        batch=batch,
        verdict=verdict,
    )


def _derive_wa(attention: Attention, priority: Priority, wa: str) -> Optional[str]:
    """P3: escalation is a contract over (attention, priority); explicit
    per-type pins are allowed for WARN/INFO; ACT ALWAYS escalates."""
    if attention is Attention.ACT:
        # Explicit pins may raise but never suppress an ACT page.
        if wa == WA_IMMEDIATE or priority is Priority.CRITICAL:
            return WA_IMMEDIATE
        return WA_URGENT
    if wa == "none" or wa is None:
        return None
    if wa in (WA_IMMEDIATE, WA_URGENT, WA_IMPORTANT):
        return wa
    # wa == "auto"
    if attention is Attention.WARN and priority is Priority.CRITICAL:
        return WA_URGENT
    return None


def _is_jobflow_source(event: Event) -> bool:
    """True iff the failing party behind a failure event is a JobFlow
    pipeline agent (or a jobflow-* cron job). Normalises the three
    producer shapes (mailbox: prefix, canonical agent, raw job name)."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    raw = (payload.get("source_agent") or event.source or "").strip()
    if raw.startswith("mailbox:"):
        raw = raw[len("mailbox:"):]
    if raw.startswith("jobflow-"):
        return True
    return canonical_agent_source(raw) in JOBFLOW_PIPELINE_AGENTS


def resolve_topic_thread(
    topics: Dict[str, dict], topic_key: str
) -> Tuple[str, str]:
    """Resolve a policy topic_key against a loaded topics.json ``topics``
    mapping, following TOPIC_ALIASES, degrading to the alerts topic rather
    than an empty thread (the pre-v3 empty-thread bug posted unmapped
    events to the group's General thread). Returns (resolved_key, thread_id);
    thread_id may be "" only if even the alerts topic is missing."""
    seen = set()
    key = topic_key
    while True:
        topic = topics.get(key)
        if topic and str(topic.get("thread_id", "")):
            return key, str(topic["thread_id"])
        seen.add(key)
        nxt = TOPIC_ALIASES.get(key)
        if nxt is None or nxt in seen:
            break
        key = nxt
    fallback = topics.get(ALERTS, {})
    return ALERTS, str(fallback.get("thread_id", ""))


# ── runtime drift signal (report, never repair) ─────────────────────────────
#
# Same residual gap as events.formatting: events.coverage contracts _POLICY to
# be total over EventType, but every enforcement point it has is commit-time.
# A gateway running an unmapped type says nothing — classify() silently falls
# back to WARN-on-watchdog_alerts, so the event lands in the wrong topic and
# its WhatsApp escalation is decided by accident. One ERROR line at import
# names every unmapped type; delivery is unchanged.
#
# Publishes the RECORD only. Back-filling _POLICY here would make
# coverage.TableSpec.resolve() — which reads this table after importing this
# module — see a complete table forever, disarming the commit-time guard.
from events.coverage import log_missing_members  # noqa: E402 - table must exist

EVENT_TYPES_WITHOUT_POLICY = log_missing_members(
    _POLICY, "events.routing_policy._POLICY", logger
)
