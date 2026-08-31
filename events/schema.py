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

    Each member is a tuple of (event_type_string, default_priority, icon).

    The icon is a REQUIRED member field, on purpose
    -----------------------------------------------
    It used to live in a parallel ``EVENT_TYPE_EMOJI`` dict in
    events/formatting.py, which drifted out of sync with this enum FOUR times
    (2026-04-27 twice, 2026-05-29, 2026-08-11). Each time a member landed with
    complete schema + routing_policy entries but no icon, ``event_icon()``
    returned "" and the event rendered in Telegram with a double-space gap in
    the header. The last one hid all twelve DevFlow Delegation Plane types for
    five days.

    Nothing caught it earlier: ruff F601/F602 structurally cannot see it (the
    dict keys were EventType attribute accesses, not string literals), and a
    test only fires after the member has already landed on a branch.

    Making the icon a required member field removes the failure mode instead of
    detecting it: "an EventType with no icon" is now unrepresentable. Omitting
    the third element is a TypeError at class-creation time, so ``import
    events.schema`` fails outright — and every producer imports events.schema,
    not events.formatting, which is exactly why the old placement let authors
    ship green. Passing "" or "   " is the ValueError in ``__init__`` below.
    Neither can be bypassed by ``--no-verify`` or by a path that never runs
    pre-commit. ``EVENT_TYPE_EMOJI`` is now a read-only view derived from this
    enum; there is no second table to keep in sync.

    Adding a member? Pick a glyph disjoint from its neighbours in the same
    Telegram topic, and put the rationale for any non-obvious pick in a comment
    right above the member.

    STILL NOT A ONE-FILE CHANGE. The routing table is a separate, genuinely
    partial table and is NOT enforced here — ``classify()`` degrades to
    WARN-on-watchdog_alerts for an unmapped type by design, and turning that
    into an import-time raise would trade a cosmetic routing defect for a
    gateway that will not boot. So it stays a check rather than a constructor
    invariant:

        events/routing_policy.py _POLICY          (topic + WhatsApp tier)

    Run

        python -m events.coverage

    which is also wired as a pre-commit hook, and see events/coverage.py for
    the manifest of which tables must be total and which are deliberately
    partial.
    """

    # Cron lifecycle
    CRON_STARTED = ("cron_started", Priority.LOW, "▶️")
    # Off-schedule trigger record — emitted by trigger_job() in cron/jobs.py
    # whenever a caller sets next_run_at = NOW (CLI `hermes cron run`, LLM
    # cronjob tool action="run", HTTP API trigger endpoint). Carries caller
    # + reason + previous/new next_run_at so off-schedule fires can be
    # attributed in postmortems. LOW priority => audit-logger captures it
    # but Telegram/WhatsApp routing leaves it out by default.
    CRON_TRIGGERED = ("cron_triggered", Priority.LOW, "👆")
    # Added 2026-08-25: the pause/resume half of the cron audit trail. Until
    # now ONLY trigger_job emitted anything — pause_job and resume_job both
    # routed through update_job, which has no emit path, so a paused job left
    # no record of the transition anywhere. Two independent investigations of
    # the 2026-08-24/25 jobflow/jaum/tracker pause churn (eight rows paused and
    # resumed repeatedly) tried to attribute it from audit.jsonl and from agent
    # transcripts and both failed, for the simple reason that there was nothing
    # to find. The job RECORD gained the WHY in cfe15649ad (paused_reason,
    # archived to paused_history on resume); these two carry the TRANSITION.
    #
    # Payload (both): job_id, job_name, caller, reason, paused_at,
    # previous_state, new_state — plus next_run_at on resume. ``reason`` is the
    # PAUSE's why in both directions: on resume it is the reason being retired,
    # which is what makes a pause/resume span readable from either end.
    # ``previous_state`` is what separates a real transition from a repeat
    # pause of an already-paused job — the shape the churn investigation
    # needed and could not get. LOW priority, same as CRON_TRIGGERED: the
    # audit logger captures it, Telegram/WhatsApp leave it out by default.
    CRON_PAUSED = ("cron_paused", Priority.LOW, "⏸️")
    # ⏯️ (play/pause) rather than ▶️, which CRON_STARTED already owns in this
    # same cron_firehose topic — the disjointness standard is per-topic.
    CRON_RESUMED = ("cron_resumed", Priority.LOW, "⏯️")
    # Added 2026-08-26: the AUTHORIZATION half, distinct from the pause half
    # above. A resume_barrier is a durable condition on a job ("do not run
    # until X"); setting or lifting one is not a schedule transition and must
    # not be recorded as a pause, so it gets its own pair rather than reusing
    # CRON_PAUSED/CRON_RESUMED.
    #
    # These exist because of a measured failure of the pause pair. On
    # 2026-08-26T05:17:33-35Z the three Gate-2 barrier jobs were resumed by
    # bin/gate2_resume_barrier_set.py, a sanctioned tool -- which at that
    # moment omitted its caller= argument (added at 01:27:44 EDT, ~10 min
    # after). The resulting caller=None on three CRON_RESUMED rows was
    # indistinguishable on the bus from an unattributed actor, and TWO
    # sessions independently read it as an unsanctioned bypass and acted on
    # that reading -- one re-pausing all three jobs, reversing an operator
    # override. The events were emitted correctly; the ATTRIBUTION was empty,
    # and an empty attribution is not neutral, it actively misleads.
    #
    # Hence: cron.jobs.set_resume_barrier / clear_resume_barrier REFUSE a
    # blank caller outright rather than warning, so these two can never carry
    # caller=None the way that CRON_RESUMED trio did.
    #
    # Payload (both): job_id, job_name, caller, action, reason, barrier_reason,
    # barrier_set_at, barrier_set_by. On "barrier_set" ``reason`` IS the new
    # barrier's condition. On "barrier_cleared" ``reason`` is the
    # JUSTIFICATION for lifting -- the evidence the condition is now met --
    # while barrier_* carry the barrier being retired, so the set/clear span
    # reads from either end without joining to the job record.
    #
    # ASYMMETRIC ON PURPOSE, Diego's call 2026-08-26. Setting a barrier ADDS
    # protection and is routine, so it stays LOW/TRACE with the rest of the
    # cron lifecycle family. Clearing one REMOVES protection, and that is the
    # action this whole saga was about being invisible or misattributed -- so
    # it is promoted to HIGH/WARN and routed to watchdog_alerts, where it
    # reaches him directly rather than sitting in the firehose.
    #
    # The asymmetry is the point: an audit trail nobody reads is only useful
    # after the fact, and every expensive hour of 2026-08-26 was spent AFTER
    # the fact. A lift is rare -- a handful ever, each one deliberate -- so
    # pushing it costs almost no traffic and buys the one notification that
    # would have collapsed this incident on day one.
    CRON_BARRIER_SET = ("cron_barrier_set", Priority.LOW, "🛑")
    CRON_BARRIER_CLEARED = ("cron_barrier_cleared", Priority.HIGH, "🔓")
    CRON_COMPLETED = ("cron_completed", Priority.NORMAL, "✔️")
    CRON_FAILED = ("cron_failed", Priority.HIGH, "💥")
    CRON_FAILED_CONSECUTIVE = ("cron_failed_consecutive", Priority.CRITICAL, "🔥")
    CRON_STALE = ("cron_stale", Priority.HIGH, "⌛")
    # Added 2026-04-30: emitted by cron/scheduler.py:tick() when a recurring
    # job is fast-forwarded past a missed fire window (gateway downtime
    # exceeded the catch-up grace, OR the job is opted out of fire-once via
    # recovery_policy="skip_only"). Spec: 2026-04-30-cron-restart-catchup-gap-design.md.
    # Distinct from CRON_SKIPPED_DUPLICATE below — that one is the concurrency-
    # guard reject; this one is the gateway-downtime miss.
    CRON_SKIPPED = ("cron_skipped", Priority.HIGH, "💤")
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
    #   cross_process_fire_blocked   (Guard #5, 2026-08-25: the prior fire is
    #                                 live in ANOTHER process, proved via the
    #                                 execution ledger rather than _in_flight;
    #                                 prior_cron_started_event_id is always
    #                                 None here — that id only exists in the
    #                                 owning process's memory)
    CRON_SKIPPED_DUPLICATE = ("cron_skipped_duplicate", Priority.LOW, "⏭️")
    # Cron min-interval-since-last-fire guard (Guard #4) -- added
    # 2026-04-30 to close the SEQUENTIAL-burst gap left by Guard #3
    # (CRON_SKIPPED_DUPLICATE only catches CONCURRENT fires). The
    # 2026-04-30 sentinel-vip-morning fires at 14:02 / 14:34 / 14:49 UTC
    # were spaced 28 min and 13 min apart -- each prior fire had already
    # released its in-flight slot before the next one arrived, so Guard
    # #3 never engaged.  Guard #4 rejects a tick-time fire when the
    # job's last_run_at is within ``min_seconds_between_fires`` of NOW,
    # regardless of whether the prior fire was tick-scheduled or trigger-
    # scheduled.  Default off (``min_seconds_between_fires`` unset = 0);
    # opt-in per-job via the field in jobs.json.  See
    # ~/.hermes/profiles/main/workspace/sentinel-vip-burst-rc-2026-04-30.md
    # §6 for the full design + per-job rollout recommendation.
    # Payload:
    #   job_id, job_name, last_run_at, elapsed_since_last_seconds,
    #   min_seconds_between_fires
    CRON_SKIPPED_MIN_INTERVAL = ("cron_skipped_min_interval", Priority.LOW, "⏳")

    # Job discovery & scoring
    JOB_DISCOVERED = ("job_discovered", Priority.NORMAL, "🎯")
    JOB_SCORED = ("job_scored", Priority.NORMAL, "📊")
    JOB_HIGH_SCORE = ("job_high_score", Priority.HIGH, "⭐")
    JOB_VIP_DISCOVERED = ("job_vip_discovered", Priority.HIGH, "💎")

    # Tailoring & applications
    TAILOR_COMPLETED = ("tailor_completed", Priority.NORMAL, "✍️")
    APPLICATION_READY = ("application_ready", Priority.HIGH, "📋")
    APPLICATION_SUBMITTED = ("application_submitted", Priority.HIGH, "✅")
    APPLICATION_FAILED = ("application_failed", Priority.CRITICAL, "❌")
    APPLICATION_BLOCKED = ("application_blocked", Priority.CRITICAL, "🚧")

    # Pipeline tracking
    STAGE_TRANSITION = ("stage_transition", Priority.NORMAL, "➡️")
    INTERVIEW_SIGNAL = ("interview_signal", Priority.CRITICAL, "🗓️")
    OFFER_SIGNAL = ("offer_signal", Priority.CRITICAL, "💰")
    FOLLOWUP_DUE = ("followup_due", Priority.HIGH, "⏰")

    # System
    DIGEST_GENERATED = ("digest_generated", Priority.LOW, "📝")
    GATEWAY_HEALTH = ("gateway_health", Priority.HIGH, "🛰️")
    AGENT_ERROR = ("agent_error", Priority.HIGH, "⚠️")
    # Model rate limiting — added 2026-08-14. Emitted by
    # events/rate_limit_signal.py when any Hermes activity hits a provider
    # rate limit, credit exhaustion, or usage cap. Coalesced into "episodes"
    # keyed (provider, model) so a sustained outage produces one alert, not
    # hundreds. Attention is payload-driven via the `outcome` field (see the
    # conditional hook in routing_policy.classify): diverted -> WARN on
    # alerts, chain_exhausted/no_fallback -> ACT + page, recovered -> INFO.
    # Icon: stop sign = this model is not taking traffic. Verified disjoint
    # from all 80 pre-existing icons; 🚦 was the first pick and collides with
    # DEVFLOW_MERGE_PENDING.
    MODEL_RATE_LIMITED = ("model_rate_limited", Priority.HIGH, "🛑")
    # Model reroute override audit trail — added 2026-08-18 (Phase 2 task 5).
    # Emitted by events/model_override.py's set_override()/clear_override()
    # on every write that actually LANDS (a rejected set_override — self-
    # target, divert-into-a-wall — emits nothing; a no-op clear_override on
    # an absent key emits nothing either). Spec §Containment: "each write
    # emits an event, so audit.jsonl records who diverted what, when" — a
    # forgotten override is the main way this feature can hurt the operator,
    # and this is how it gets found later. INFO, not WARN/ACT: it is a
    # record of a deliberate operator action, not a decision or a fault.
    # Icon: scroll = a written record/audit trail. Verified disjoint from
    # all 25 existing watchdog_alerts icons (and from every icon globally).
    MODEL_OVERRIDE_SET = ("model_override_set", Priority.NORMAL, "📜")
    MEMORY_CONSOLIDATED = ("memory_consolidated", Priority.LOW, "🧠")
    SKILL_EVOLVED = ("skill_evolved", Priority.LOW, "🚀")
    MAILBOX_MESSAGE = ("mailbox_message", Priority.LOW, "📨")
    USER_INBOUND_MESSAGE = ("user_inbound_message", Priority.NORMAL, "💬")

    # Security
    # Icon: padlock, because (a) no existing icon conflicts and (b) operators
    # scanning the Security topic need a visual hook distinct from the generic
    # HIGH dot. Added 2026-04-19 per SR-408 post-flood remediation — before it
    # existed the header rendered with a double-space gap that swam in a noisy
    # feed.
    SECRET_DETECTED = ("secret_detected", Priority.HIGH, "🔐")
    # Credential/infra loss detected by the laptop-monitor watchdog sweep
    # (2026-07-10, R70 alert-gap fix). A curated allowlist of probes (WhatsApp
    # session creds, the ENOSPC 0-byte credential/config sweep, OAuth token
    # validity) transitioning healthy->down/error is emitted INDIVIDUALLY by
    # watchdog_sweep._emit_transitions (never folded into WATCHDOG_BURST) so the
    # specific loss is NAMED, at CRITICAL priority mapped to WhatsApp
    # EscalationTier.IMMEDIATE + Telegram security_and_system so it BREAKS quiet
    # hours. Closes the 2026-07-10 02:44 WhatsApp-creds-zeroing miss where the
    # burst-coalesced, URGENT-tier, quiet-hours-queued signal never woke Diego.
    # See memory whatsapp_session_zeroed_repair_pending.md.
    # Icon: key = a credential is gone; deliberately distinct from the padlock
    # on SECRET_DETECTED, which is a secret being *found*.
    CREDENTIAL_LOSS = ("credential_loss", Priority.CRITICAL, "🔑")

    # Phase B Stage-3 iter2 (HITL + apply + tracker -> Postgres)
    APPROVAL_REQUEST = ("approval_request", Priority.HIGH, "🙋")
    APPLY_PACKET = ("apply_packet", Priority.NORMAL, "📦")

    # Tailor structured iteration event — added 2026-04-29 (plan
    # 2026-04-29-tailor-structured-iteration-event). Emitted by the
    # cron-wrapper after `jobflow-tailor` runs, carrying counts +
    # categorical reason so the Critic + Watchdog can distinguish
    # "nothing to do" from "something is broken" — a discrimination
    # the existing `[SILENT]` marker cannot make. Bus-only metadata;
    # the human-readable Telegram pathway is unchanged.
    TAILOR_ITERATION = ("tailor_iteration", Priority.LOW, "✂️")

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
    AGENT_ITERATION = ("agent_iteration", Priority.LOW, "🔁")

    # Phase C iter2 (Critic proposals routed to WhatsApp/Telegram)
    CRITIC_PROPOSAL = ("critic_proposal", Priority.NORMAL, "🧐")
    # Critic auto-apply event — added 2026-04-29 (consolidation phase C).
    # Emitted by auto_applier.apply_decision after a successful mutation +
    # changelog write. Existing TOPIC_ROUTING in telegram_notifier.py already
    # routes critic_auto_applied -> critic_proposals topic; this enum entry
    # makes the event actually emittable via EventType.from_string().
    # ✅ COLLISION VERDICT (settled 2026-08-11 — do NOT re-open):
    #   ✅ is also APPLICATION_SUBMITTED's. This is DRIFT, not design:
    #   fa9915e07 added this entry purely to close a coverage gap ("Additions
    #   only"), and its verification line names test_event_icons_cover_all_types
    #   only — never a uniqueness check. Compare SECRET_DETECTED above, where the
    #   author explicitly recorded "no existing icon conflicts"; that sentence is
    #   absent here because nobody looked.
    #   It is nonetheless LEGAL and is being KEPT: the standard is per-topic
    #   (see the EventType docstring — "disjoint from its neighbours in the same
    #   Telegram topic"), and these two land in different lanes —
    #   application_submitted -> JOBFLOW, critic_auto_applied -> CRITIC
    #   (events/routing_policy.py). No operator sees both ✅ in one feed, which
    #   is exactly what test_event_icons_are_unique_within_a_telegram_topic
    #   asserts — it permits this pair on purpose.
    #   If a future change ever makes it worth de-duping, CRITIC_AUTO_APPLIED is
    #   the safer target (narrower audience) — but these glyphs are operator-
    #   facing muscle memory, so ASK DIEGO before changing one.
    #   Contrast 🟢 (GATEWAY_STARTED, below): that cross-topic dupe IS deliberate.
    CRITIC_AUTO_APPLIED = ("critic_auto_applied", Priority.NORMAL, "✅")

    # Watchdog signals — added 2026-04-25 (iter5).
    # Previously these were emitted via _emit_event()'s AGENT_ERROR fallback
    # when the requested EventType didn't exist, with the real type stuffed
    # into payload.watchdog_type. That caused the 2026-04-24 cluster-feedback
    # flood (watchdog detected its own AGENT_ERROR emissions as a cluster ->
    # emitted more AGENT_ERRORs -> next sweep saw a bigger cluster -> ...).
    # Promoting them to first-class EventType members removes the
    # source=watchdog hack downstream.
    WATCHDOG_TICK = ("watchdog_tick", Priority.LOW, "💓")
    WATCHDOG_PROBE_TRANSITION = ("watchdog_probe_transition", Priority.HIGH, "🔄")
    # Coalesced form of N>=5 simultaneous probe transitions emitted by the
    # watchdog sweep. Payload schema:
    #   {
    #     "transitions": [ {probe, tier, category, before, after, detail}, ... ],
    #     "count": int,
    #     "trigger": "burst_threshold" | "future-other-trigger",
    #   }
    # Added 2026-04-28 after Docker-crash -> 22-event WAL-lock flood incident.
    # See docs/superpowers/plans/2026-04-28-watchdog-burst-coalesce-and-ack-dead-letter.md
    WATCHDOG_BURST = ("watchdog_burst", Priority.HIGH, "🌊")
    WATCHDOG_SILENCE_ALERT = ("watchdog_silence_alert", Priority.HIGH, "🔕")
    WATCHDOG_RECOVERED = ("watchdog_recovered", Priority.NORMAL, "💚")
    WATCHDOG_SELF_DEGRADED = ("watchdog_self_degraded", Priority.HIGH, "🤕")
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
    # Icon: the stethoscope picks up the existing health-theme set (💓 tick,
    # 🤕 self-degraded, 💚 recovered) while staying visually distinct from the
    # per-failure signals, so an operator scanning watchdog_alerts can spot the
    # once-a-day summary at a glance.
    WATCHDOG_DAILY = ("watchdog_daily", Priority.NORMAL, "🩺")
    # Cumulative container restart budget -- added 2026-08-12 after the
    # hindsight-app 429 crash-loop ran for a week. laptop-monitor's churn
    # verdict (Get-ContainerChurnVerdict) is a ONE-PASS delta with a 600s
    # self-clear, so a container that burned 264 restarts in a morning renders
    # "RestartCount stable (266)" and GREEN -- the absolute count is fetched,
    # printed into the detail string, and then thrown away. This event is the
    # missing aggregate: restarts summed over a rolling 24h window, emitted
    # EVEN WHILE THE TRAY ROW READS HEALTHY. Payload schema:
    #   {
    #     "container": str, "restarts_24h": int, "restart_count_now": int,
    #     "threshold": int, "tray_state": str, "tray_tier": str,
    #     "tray_detail": str,
    #   }
    # Emit logic: ~/.hermes/profiles/watchdog/workspace/watchdog_sweep.py
    # (_restart_budget_alarms, sourced from laptop-monitor's
    # ~/.claude/logs/container-churn-state.json). Deliberately NOT tier-gated:
    # a crash-looping container is broken at any row tier, and tier only ever
    # set PRIORITY in the sweep, never reachability.
    CONTAINER_CRASH_LOOP = ("container_crash_loop", Priority.HIGH, "🔁")
    AGENT_FAILURE_CLUSTER = ("agent_failure_cluster", Priority.HIGH, "🌪️")

    # Curator nightly consolidation -- added 2026-04-26.
    # Emitted by curator.orchestrator after backfill / nightly delta
    # passes. Consumed by Scribe (morning digest) and the memory-writer
    # subscriber for cursor advance.
    CURATOR_DAILY = ("curator_daily", Priority.NORMAL, "📚")

    # DevFlow bridge -- added 2026-04-26.
    # Emitted by the devflow profile (see ~/.hermes/profiles/devflow/SOUL.md
    # emit-hooks section) and consumed by ~/.hermes/bridges/hermes_to_devflow.py
    # which projects them into DevFlow Postgres so Mission Control UI :3040
    # reflects live agent activity.
    DEVFLOW_RUN_STARTED = ("devflow.run_started", Priority.NORMAL, "🏃")
    DEVFLOW_RUN_COMPLETED = ("devflow.run_completed", Priority.NORMAL, "🏁")
    DEVFLOW_APPROVAL_REQUESTED = ("devflow.approval_requested", Priority.HIGH, "🗳️")
    DEVFLOW_TRACE_SNAPSHOT = ("devflow.trace_snapshot", Priority.LOW, "📷")

    # DevFlow PR + build telemetry -- added 2026-04-30 (visibility-restoration
    # B11 item 2-3). Surfaces SDLC activity in the devflow_firehose and
    # devflow_decisions Telegram topics so Mission Control reflects the
    # software-delivery side of Hermes, not just bridge ticks. Producers
    # are deferred -- see events/producers/devflow_pr_build.py for emitter
    # helpers any future poller / webhook receiver / manual trigger can
    # call. Spec at docs/superpowers/specs/2026-04-30-devflow-pr-build-events.md.
    DEVFLOW_PR_OPENED = ("devflow.pr_opened", Priority.NORMAL, "🔃")
    DEVFLOW_PR_MERGED = ("devflow.pr_merged", Priority.HIGH, "🟣")
    DEVFLOW_PR_CLOSED = ("devflow.pr_closed", Priority.NORMAL, "🚫")
    DEVFLOW_PR_REVIEW_REQUESTED = ("devflow.pr_review_requested", Priority.HIGH, "👀")
    DEVFLOW_BUILD_STARTED = ("devflow.build_started", Priority.LOW, "🔨")
    DEVFLOW_BUILD_SUCCEEDED = ("devflow.build_succeeded", Priority.NORMAL, "🟢")
    DEVFLOW_BUILD_FAILED = ("devflow.build_failed", Priority.HIGH, "🧨")

    # DevFlow Delegation Plane (DDP) lifecycle -- added 2026-08-06. Stage 1
    # control plane of the delegation design (spec:
    # docs/superpowers/specs/2026-08-06-devflow-delegation-plane-design.md).
    # Producers: devflow_delegation.emitter / .lifecycle / .cli. The existing
    # devflow.build_* / devflow.pr_* members above are REUSED for the
    # BUILDING and PR_OPEN lifecycle states; only genuinely new lifecycle
    # names are added here. work_requested/work_triaged/work_planned surface
    # new delegated work; duplicate/suppressed are flood-control outcomes
    # (LOW so they batch quietly), while declined is NORMAL (a real decision
    # worth surfacing); merge/deploy members land in Stage 2/3 but are
    # registered now so the routing table is total.
    # Icons for this block are deliberately disjoint from the PR/build set above
    # so an operator scanning devflow_firehose can tell a *delegation* signal
    # from an *SDLC* one: 🎫 work arrives, 🏷️ triaged, 🗺️ planned. The two
    # flood-control outcomes read as 'nothing new' (👯 dupe, 🔇 suppressed) and
    # stay visually quieter than 🙅, which is a real decision.
    DEVFLOW_WORK_REQUESTED = ("devflow.work_requested", Priority.NORMAL, "🎫")
    DEVFLOW_WORK_TRIAGED = ("devflow.work_triaged", Priority.NORMAL, "🏷️")
    DEVFLOW_WORK_PLANNED = ("devflow.work_planned", Priority.NORMAL, "🗺️")
    DEVFLOW_WORK_DUPLICATE = ("devflow.work_duplicate", Priority.LOW, "👯")
    DEVFLOW_WORK_DECLINED = ("devflow.work_declined", Priority.NORMAL, "🙅")
    DEVFLOW_WORK_SUPPRESSED = ("devflow.work_suppressed", Priority.LOW, "🔇")
    # Icons: 🚦 = gated waiting for green; 🧩 = the pieces fit (distinct from
    # 🟣 devflow.pr_merged, which is the *PR* event, and from 🔀 CODE_DRIFT);
    # 🤖 marks the merge that happened with no human gate, which is why
    # routing_policy gives it WARN and not INFO.
    DEVFLOW_MERGE_PENDING = ("devflow.merge_pending", Priority.HIGH, "🚦")
    DEVFLOW_MERGED = ("devflow.merged", Priority.HIGH, "🧩")
    DEVFLOW_AUTO_MERGED = ("devflow.auto_merged", Priority.HIGH, "🤖")
    DEVFLOW_DEPLOY_STARTED = ("devflow.deploy_started", Priority.NORMAL, "🛫")
    DEVFLOW_DEPLOYED = ("devflow.deployed", Priority.NORMAL, "🛬")
    # Icon: siren, not 🧨/💥/❌ — those are build/cron/application failures; a
    # failed deploy is the one that escalates to WhatsApp (WA_URGENT).
    DEVFLOW_DEPLOY_FAILED = ("devflow.deploy_failed", Priority.HIGH, "🚨")

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
    # Icons: 📬/📭 are distinct from the generic green/red so an operator
    # scanning watchdog_alerts can tell a delivery report apart from a
    # build/system signal at a glance. The cycle guard in handle() makes these
    # effectively unreachable in chat; the icons still matter as a fallback
    # render if that guard ever regresses.
    NOTIFICATION_DELIVERED = ("notification_delivered", Priority.LOW, "📬")
    NOTIFICATION_FAILED = ("notification_failed", Priority.NORMAL, "📭")

    # Gateway lifecycle — added 2026-04-30 (gateway-restart-cluster
    # mitigation M1, profiles/sentinel/workspace/
    # gateway-restart-cluster-2026-04-30.md). Without these the only signal
    # of a gateway boot is the platforms-up cluster, which fires for any
    # boot regardless of cause; restart investigations had to triangulate
    # via gateway.pid mtime + agent.log boot lines + watchdog probe gaps.
    # GATEWAY_STARTED carries pid + parent_pid + parent_cmdline + boot_reason
    # so a watchdog recovery is distinguishable from an operator restart.
    # GATEWAY_STOPPED carries pid + exit_reason + runtime_seconds + the
    # inflight cron correlation_ids list so spawn-task-#1's CRON_ABORTED
    # synthesizer can see what got killed. NORMAL priority so digest_only
    # verbosity surfaces them alongside HIGH+ failures, but not LOW where
    # they'd batch out of operator visibility.
    # Icons: green up / red down, mirroring the build up/down convention but
    # for the gateway process itself.
    GATEWAY_STARTED = ("gateway_started", Priority.NORMAL, "🟢")
    GATEWAY_STOPPED = ("gateway_stopped", Priority.NORMAL, "🔴")

    # R57 backend-drift detection nets (added 2026-05-29, ADR-0024 §2-3).
    # BACKEND_CONTRACT_DRIFT: the synthetic canary (obs/backend_conformance_canary)
    # found a Codex/Anthropic backend returning a shape the stock SDK parser
    # cannot consume (the R57 output=None signature). AGENT_LOOP_FAULT: the agent
    # loop hit an unhandled stream-accumulation exception that the classifier would
    # otherwise have buried as a silent non-retryable "empty response" (SR-471).
    # Both HIGH so they survive significant_only/digest_only verbosity.
    # Icons: 📐 = contract/conformance check; 🌀 = loop fault at non-retryable
    # abort.
    # 🌀 (spiral = the agent loop itself) replaced 💥 on 2026-08-11: 💥 was
    # already CRON_FAILED's, and both route to watchdog_alerts, so an operator
    # scanning that feed saw two different failures wearing one glyph. The
    # cron family keeps 💥/🔥; this is the agent's own loop dying, not a job's.
    BACKEND_CONTRACT_DRIFT = ("backend_contract_drift", Priority.HIGH, "📐")
    AGENT_LOOP_FAULT = ("agent_loop_fault", Priority.HIGH, "🌀")

    # System-resource exhaustion early-warning — added 2026-06-11 after the
    # pagefile-expansion disk burst (commit charge hit 84.2/85.6 GB = 98.4%,
    # Windows expanded pagefile.sys 36->54.4 GB in ~22 min, eating ~18 GB of
    # C: with ZERO alerting; Windows' own Resource-Exhaustion-Detector logged
    # nothing). Emitted by events.producers.resource_monitor.ResourcePressureMonitor,
    # which samples commit charge / pagefile allocation / C: free on the
    # gateway poll loop and fires on the rising edge of any pressure trigger:
    #   - commit charge > 85% of the commit limit, OR
    #   - C: free < 15 GB, OR
    #   - pagefile allocation grew > 2 GB within 10 minutes.
    # HIGH so it survives significant_only / digest_only verbosity and reaches
    # Telegram (watchdog_alerts) BEFORE absolute exhaustion. Payload schema:
    #   reasons (list[str])              — which triggers fired this edge
    #   commit_used_gb / commit_limit_gb / commit_pct (float)
    #   pagefile_allocated_gb (float)    — approx Win32_PageFileUsage.AllocatedBaseSize
    #   pagefile_growth_gb_10min (float) — rise over the trailing 10-min window
    #   disk_c_free_gb (float)
    #   thresholds (dict)                — the limits that were evaluated
    # Icon: fire extinguisher = 'resource exhaustion fire, grab it now.'
    # Distinct from every other watchdog_alerts icon and (deliberately) not a
    # priority dot, so it does not render adjacent to its own HIGH 🟠 dot in
    # the header.
    RESOURCE_PRESSURE = ("resource_pressure", Priority.HIGH, "🧯")

    # Tracker-intent-applier partial-backlog early-warning — added 2026-07-14.
    # On 2026-07-13 thirteen APPROVAL_INTENT partials piled up in
    # ~/.hermes/mailbox/tracker/partial/ and sat ~a day unnoticed. A partial is
    # an intent whose pipeline.json write succeeded but whose Postgres mirror
    # (:4100 step 4) did not; the idempotency key is unburned so it stays
    # re-drivable. Emitted by events.producers.partial_backlog_monitor.
    # PartialBacklogMonitor, which read-only counts partial/ on the shared
    # subscriber poll loop and fires on the rising edge of count > threshold
    # (default 3). HIGH so it survives significant_only / digest_only verbosity;
    # routed to jobflow_decisions (the human-action lane). Payload:
    #   count (int)                — partial *_INTENT_*.json files right now
    #   threshold (int)            — the alert threshold that was crossed
    #   oldest_age_seconds (float) — age of the oldest partial (entered-partial mtime)
    #   capped_count (int)         — number of job IDs in sample_job_ids
    #   sample_job_ids (list[str]) — up to SAMPLE_CAP job IDs for triage
    # Icon: the inbox tray reads as 'pile-up' — a growing queue of intents
    # whose Postgres mirror is stuck.
    TRACKER_PARTIAL_BACKLOG = ("tracker_partial_backlog", Priority.HIGH, "📥")

    # Agent-src code-drift alert — added 2026-07-21. The gateway's editable
    # install imports the WORKING TREE of ~/.hermes/agent-src, which is
    # deliberately kept on a detached HEAD so worktree agents can land
    # commits onto the `main` ref via `git branch -f`. A commit landed on
    # main therefore does NOT run until the checkout is fast-forwarded and
    # the gateway restarted — on 2026-07-20/21 three restart cycles ran
    # stale code while every session believed the fix was live. Emitted by
    # events.producers.code_drift_monitor.CodeDriftMonitor (read-only git
    # probe every 15 min on the subscriber poll loop; rising edge / shape
    # change / 6h re-ping, plus a falling-edge status="resolved" event).
    # HIGH so it survives significant_only / digest_only verbosity. Payload:
    #   status (str)            — "drifting" | "resolved"
    #   state (str)             — "behind" | "ahead" | "diverged" (drifting only)
    #   head / main (str)       — short SHAs
    #   behind_count / ahead_count (int)
    #   dirty (bool)            — uncommitted changes in the checkout
    #   missed_subjects (list[str]) — up to 5 "<sha> <subject>" lines (behind)
    #   repo (str)              — checkout path probed
    # Icon: shuffle arrows read as 'the code paths crossed'; distinct from
    # 🔃 (devflow.pr_opened).
    CODE_DRIFT = ("code_drift", Priority.HIGH, "🔀")

    # Laptop boot report — added 2026-07-27. ~/laptop-start.ps1 posts a summary
    # of the logon boot (which services came up, which failed, which anomalies
    # fired) and used to send it as RAW TEXT straight at the watchdog_alerts
    # thread, bypassing events.formatting: it was the one message in the feed
    # with no priority dot, no icon and no source/timestamp header. The script
    # now shells out to `python -m events.emit_external --type boot_summary`
    # (see events/emit_external.py) so an out-of-process producer can reach the
    # bus; the raw send survives only as a fallback for a boot so broken that
    # Python or the bus DB is unusable. Emitted ONLY when the boot had trouble
    # (Get-BootSummaryPayload returns null on a clean boot), so this is an
    # alert, not a heartbeat. HIGH so it survives significant_only /
    # digest_only verbosity; WARN on watchdog_alerts, which means no WhatsApp
    # page unless a caller explicitly passes --priority critical. Payload:
    #   boot_id (str)          — laptop-start's boot id, e.g. "20260727-132212"
    #   state (str)            — "done" | "failed"
    #   total/done/failed/skipped (int) — step counts for the boot
    #   failures (list[str])   — "[tier] name: detail" per failed step
    #   anomalies (list[str])  — "kind: detail" per error-severity anomaly
    # Icon: the boot glyph is deliberately the same code point that
    # ~/laptop-start.ps1 hardcodes in its non-bus fallback header (U+1F97E), so
    # a fallback message and a bus-rendered one look alike; changing it here
    # means changing it there too.
    BOOT_SUMMARY = ("boot_summary", Priority.HIGH, "🥾")

    # Arbitrary agent- or script-authored prose — added 2026-08-19. Every
    # other type renders only the fields ITS branch of
    # TelegramNotifier._format_payload knows, so a caller with a sentence to
    # send had to borrow the least-wrong type and watch the sentence be
    # discarded. Worse than ugly: a borrowed type renders IDENTICALLY for any
    # payload, and RepeatGuard fingerprints the RENDERED message, so two
    # distinct notes collapsed to one fingerprint and the second was dropped
    # with no dead-letter and no audit line (2026-08-19 spec). That is why the
    # calling code left the bus for the raw Bot API, losing the dot, icon,
    # source header and durable queue.
    #
    # The body renders `headline` + a multi-line `detail` VERBATIM — the same
    # contract AGENT_ITERATION already honours for its structured `brief`.
    # Because the type has no intrinsic semantics, the CALLER declares the
    # attention class in the payload (`attention`: info|warn|trace); see
    # events/routing_policy.py. That declaration is capped: `act` clamps to
    # WARN and _Spec(priority_cap=HIGH) keeps wa_tier None for every possible
    # payload, so an agent note can never page the phone. Payload:
    #   headline (str, required) — one line, the subject
    #   detail (str, optional)   — multi-line free text, rendered verbatim
    #   attention (str, optional)— info (default) | warn | trace
    # NORMAL default: INFO notes stay calm and the WARN class floor lifts the
    # ones that matter without the producer having to know the floors.
    # Icon: a spiral notepad reads as 'a note someone wrote'. Verified free
    # across all members and disjoint from the glyphs already in both topics
    # this type can reach (agents_memory 📚🚀🧠, watchdog_alerts's 26).
    AGENT_NOTE = ("agent_note", Priority.NORMAL, "🗒️")

    # P6 Claude fleet controller audit pair — added 2026-08-31 (storm board
    # row 41). The controller is the ACTION half of the spawn-churn defense:
    # D7's spawn_latency axis (RESOURCE_PRESSURE above) detects; these two
    # record what the fleet controller decided and what actually happened.
    # Deliberately NOT overloaded onto RESOURCE_PRESSURE: a detector reading
    # and a projected/executed process action are different audit species,
    # and the 08-26 storm forensics ran aground precisely on actions that
    # left no typed record. Emitted by claude_fleet_control.controller via
    # cursorless EventBus.query()/emit() — the controller is NOT a
    # subscriber and must never join subscriber_roster.json.
    #
    # CLAUDE_FLEET_PLAN payload: schema/policy versions, run/plan ids, mode,
    # decision (no_action | shadow_projected | enforce_projected), trigger
    # reasons, fleet_root_count, bounded D7 evidence metadata, the selected
    # tree as identities only (pid:create_time — never command lines or
    # transcript contents), and a rejection-reason histogram.
    # CLAUDE_FLEET_RESULT payload: run/plan ids, status (no_action |
    # shadow_projected | cancelled | hard_terminated | failed),
    # executor_called, exited/surviving identities, detail. The "status" key
    # name is load-bearing: events.outcomes scans it, so status=="failed"
    # earns a FAILED verdict and routing's generic TRACE->WARN promotion —
    # no fleet-specific hook exists. LOW priority: routine shadow telemetry
    # batches in the security_and_system topic and never pages.
    # Icons: abacus = counting/planning the fleet; control knobs = the
    # bounded action lane reporting back. Both disjoint from their topic's
    # existing glyphs (🧯 resource_pressure, 📐 backend_contract_drift).
    CLAUDE_FLEET_PLAN = ("claude_fleet_plan", Priority.LOW, "🧮")
    CLAUDE_FLEET_RESULT = ("claude_fleet_result", Priority.LOW, "🎛️")

    def __init__(self, type_string: str, default_priority: Priority, icon: str):
        # The icon is REQUIRED, and that is the whole point of it living here
        # rather than in a parallel dict — see the class docstring. Omitting it
        # is a TypeError from Enum's member construction; passing "" to silence
        # that TypeError is this ValueError. Either way the failure is on the
        # line being edited, at class-creation time, and no importer of
        # events.schema (i.e. every producer) can get past it.
        if not isinstance(icon, str) or not icon.strip():
            raise ValueError(
                f"EventType.{type_string}: icon must be a non-empty string. "
                "Every event type needs a distinct glyph — an empty icon "
                "renders in Telegram as a double-space gap in the header."
            )
        self.type_string = type_string
        self.default_priority = default_priority
        self.icon = icon

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
