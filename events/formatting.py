"""Emoji + visual formatting helpers for event-bus notifications.

Provides priority dots, event-type icons, and header/body builders used by
TelegramNotifier, TelegramMirror, WhatsAppEscalator, and DigestComposer.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from events.outcomes import OutcomeVerdict, marker_for_verdict
from events.schema import Event, EventType, Priority

# Priority -> colored dot (matches severity)
PRIORITY_EMOJI = {
    Priority.CRITICAL: "🔴",
    Priority.HIGH:     "🟠",
    Priority.NORMAL:   "🟡",
    Priority.LOW:      "🟢",
}

# Event type -> icon
EVENT_TYPE_EMOJI = {
    EventType.CRON_STARTED:             "▶️",
    EventType.CRON_TRIGGERED:           "👆",
    EventType.CRON_COMPLETED:           "✔️",
    EventType.CRON_FAILED:              "💥",
    EventType.CRON_FAILED_CONSECUTIVE:  "🔥",
    EventType.CRON_STALE:               "⌛",
    EventType.CRON_SKIPPED:             "💤",
    EventType.CRON_SKIPPED_DUPLICATE:   "⏭️",
    EventType.CRON_SKIPPED_MIN_INTERVAL: "⏳",
    EventType.JOB_DISCOVERED:           "🎯",
    EventType.JOB_SCORED:               "📊",
    EventType.JOB_HIGH_SCORE:           "⭐",
    EventType.JOB_VIP_DISCOVERED:       "💎",
    EventType.TAILOR_COMPLETED:         "✍️",
    EventType.APPLICATION_READY:        "📋",
    EventType.APPLICATION_SUBMITTED:    "✅",
    EventType.APPLICATION_FAILED:       "❌",
    EventType.APPLICATION_BLOCKED:      "🚧",
    EventType.INTERVIEW_SIGNAL:         "🗓️",
    EventType.OFFER_SIGNAL:             "💰",
    EventType.STAGE_TRANSITION:         "➡️",
    EventType.FOLLOWUP_DUE:             "⏰",
    EventType.DIGEST_GENERATED:         "📝",
    EventType.GATEWAY_HEALTH:           "🛰️",
    EventType.AGENT_ERROR:              "⚠️",
    EventType.MEMORY_CONSOLIDATED:      "🧠",
    EventType.SKILL_EVOLVED:            "🚀",
    EventType.MAILBOX_MESSAGE:          "📨",
    # SR-001 secret scanner (fork-patch carried alongside SECRET_DETECTED enum +
    # TOPIC_ROUTING entry). Padlock chosen because (a) no existing icon conflicts,
    # (b) operators scanning the Security topic need a distinct visual hook
    # separate from the generic HIGH dot. Added 2026-04-19 per SR-408 post-
    # flood remediation — without this entry event_icon() returned "" and the
    # header rendered with a double-space gap that swam in a noisy feed.
    EventType.SECRET_DETECTED:          "🔐",
    # Credential/infra loss (2026-07-10, R70 alert-gap fix). Key icon = a
    # credential is gone; distinct from the padlock (a secret was *found*).
    EventType.CREDENTIAL_LOSS:          "🔑",
    # Phase B Stage-3 iter2 — HITL approvals + apply packet
    EventType.APPROVAL_REQUEST:         "🙋",
    EventType.APPLY_PACKET:             "📦",
    # Phase C iter2 — Critic proposals
    EventType.CRITIC_PROPOSAL:          "🧐",
    # Watchdog signals (iter5, 2026-04-25) — promoted from AGENT_ERROR fallback
    EventType.WATCHDOG_TICK:            "💓",
    EventType.WATCHDOG_PROBE_TRANSITION:"🔄",
    EventType.WATCHDOG_SILENCE_ALERT:   "🔕",
    EventType.WATCHDOG_RECOVERED:       "💚",
    EventType.AGENT_FAILURE_CLUSTER:    "🌪️",
    # Curator nightly consolidation (2026-04-26)
    EventType.CURATOR_DAILY:            "📚",
    # DevFlow bridge (2026-04-26)
    EventType.DEVFLOW_RUN_STARTED:      "🏃",
    EventType.DEVFLOW_RUN_COMPLETED:    "🏁",
    EventType.DEVFLOW_APPROVAL_REQUESTED:"🗳️",
    EventType.DEVFLOW_TRACE_SNAPSHOT:   "📷",
    # Scribe action telemetry (2026-04-28)
    EventType.USER_INBOUND_MESSAGE:     "💬",
    # Critic auto-apply (2026-04-29) + Watchdog burst-coalesce + self-degraded
    EventType.CRITIC_AUTO_APPLIED:      "✅",
    EventType.WATCHDOG_BURST:           "🌊",
    EventType.WATCHDOG_SELF_DEGRADED:   "🤕",
    # Tailor structured iteration (2026-04-29) — counts + reason so the
    # Critic can distinguish "nothing to do" from "something is broken"
    EventType.TAILOR_ITERATION:         "✂️",
    # Generic agent iteration (2026-04-30) — per-agent run summary
    # extending TAILOR_ITERATION pattern across all cron-driven agents.
    EventType.AGENT_ITERATION:          "🔁",
    # Watchdog daily heartbeat (2026-04-30) — once-per-day aggregate health
    # summary. Stethoscope picks up on the existing health-theme set (💓
    # tick, 🤕 self-degraded, 💚 recovered) while staying visually distinct
    # from the per-failure signals so an operator scanning watchdog_alerts
    # can spot the once-a-day summary at a glance.
    EventType.WATCHDOG_DAILY:           "🩺",
    # DevFlow PR + build telemetry (2026-04-30) — visibility-restoration
    # B11 item 2-3. Spec docs/superpowers/specs/2026-04-30-devflow-pr-build-events.md.
    EventType.DEVFLOW_PR_OPENED:        "🔃",
    EventType.DEVFLOW_PR_MERGED:        "🟣",
    EventType.DEVFLOW_PR_CLOSED:        "🚫",
    EventType.DEVFLOW_PR_REVIEW_REQUESTED:"👀",
    EventType.DEVFLOW_BUILD_STARTED:    "🔨",
    EventType.DEVFLOW_BUILD_SUCCEEDED:  "🟢",
    EventType.DEVFLOW_BUILD_FAILED:     "🧨",
    # Notification delivery reverse-signal (2026-04-30) — visibility
    # for whether a notification reached the user. Distinct from generic
    # green/red so an operator scanning watchdog_alerts can tell a
    # delivery report apart from a build/system signal at a glance.
    # The cycle guard in handle() makes these effectively unreachable
    # in chat, but the icon entry keeps test_event_icons_cover_all_types
    # honest and gives a fallback render if the guard ever regresses.
    EventType.NOTIFICATION_DELIVERED:   "📬",
    EventType.NOTIFICATION_FAILED:      "📭",
    # Gateway lifecycle (boot/shutdown) -> watchdog_alerts. Green up / red down,
    # mirroring the build up/down convention but for the gateway process itself.
    EventType.GATEWAY_STARTED:          "🟢",
    EventType.GATEWAY_STOPPED:          "🔴",
    # Fork resilience signals emitted without an icon: backend-conformance
    # canary (SR-470) + agent loop-fault loud-failure boundary (SR-471).
    # 📐 = contract/conformance check; 💥 = loop fault at non-retryable abort.
    EventType.BACKEND_CONTRACT_DRIFT:   "📐",
    EventType.AGENT_LOOP_FAULT:         "💥",
    # Resource-pressure early-warning (2026-06-11 pagefile-burst remediation).
    # Fire extinguisher = "resource exhaustion fire, grab it now." Distinct
    # from every other watchdog_alerts icon and (deliberately) not a priority
    # dot, so it doesn't render adjacent to its own HIGH 🟠 dot in the header.
    EventType.RESOURCE_PRESSURE:        "🧯",
    # Tracker partial/ backlog alert (2026-07-14) — a growing queue of intents
    # whose Postgres mirror is stuck; the inbox-tray icon reads as "pile-up".
    EventType.TRACKER_PARTIAL_BACKLOG:  "📥",
    # Agent-src code drift (2026-07-21) — the deployed detached checkout is
    # not running what main says should be running. Shuffle arrows read as
    # "the code paths crossed"; distinct from 🔃 (PR opened).
    EventType.CODE_DRIFT:               "🔀",
    # Laptop boot report (2026-07-27) — a boot that came up with services
    # missing. The boot glyph is deliberately the same code point that
    # ~/laptop-start.ps1 hardcodes in its non-bus fallback header
    # (U+1F97E), so a fallback message and a bus-rendered one look alike;
    # changing it here means changing it there too.
    EventType.BOOT_SUMMARY:             "🥾",
}

# Inner mailbox-message type -> icon (overrides generic mailbox icon when known)
MAILBOX_INNER_EMOJI = {
    "SCORE_RESULT":        "📊",
    "SCORE_BATCH_SUMMARY": "📊",
    "SCOUT_DISCOVERY":     "🎯",
    "TAILOR_COMPLETE":     "✍️",
    "TAILOR_REQUEST":      "✍️",
    "SUBMIT_REQUEST":      "📋",
    "DRY_RUN_COMPLETE":    "📋",
    "SUBMIT_CONFIRM":      "✅",
    "BLOCKED_QUESTION":    "🚧",
    "PIPELINE_UPDATE":     "➡️",
    "FOLLOWUP_ALERT":      "⏰",
    "NOTIFICATION":        "📨",
    "ERROR":               "⚠️",
    "VIP_DISCOVERY":       "💎",
    "STATUS_RESPONSE":     "📨",
    "HIGH_SCORE_ALERT":    "⭐",
}

# 15 box-drawing dashes -- renders cleanly on both Telegram and WhatsApp
SEPARATOR = "───────────────"


def priority_dot(priority: Priority) -> str:
    return PRIORITY_EMOJI.get(priority, "")


# Event types that ARE a recovery — no payload inspection needed. Their
# priority exists to make the *transition* land (a gateway coming back is
# worth a message), not to say "something is wrong".
_ALWAYS_RECOVERY_TYPES = frozenset({
    EventType.GATEWAY_STARTED,
    EventType.WATCHDOG_RECOVERED,
})

# Event types that are a recovery only for certain payloads:
#   type -> (payload field, value that means "recovered")
_RECOVERY_WHEN = {
    EventType.GATEWAY_HEALTH: ("status", "up"),
    EventType.CODE_DRIFT: ("status", "resolved"),
    EventType.WATCHDOG_PROBE_TRANSITION: ("after", "healthy"),
}


def header_dot(event: Event, verdict: OutcomeVerdict | None = None) -> str:
    """Return a verdict-backed marker, with a legacy priority fallback.

    When delivery already classified the event, the immutable verdict is the
    presentation authority.  Callers outside the notification path may omit it
    and retain the historical recovery overrides below.

    Legacy behavior overrides the priority color where an event semantically
    reads as a recovery rather than a problem.

    GATEWAY_HEALTH carries a fixed HIGH priority (so a real outage escalates),
    which means a 'down' AND a 'back-up' both inherited the amber 🟠 dot. An
    operator scanning the feed asked (2026-07-18) for the recovery/'up' line to
    read as green — visually distinct from an outage — without touching the
    event's priority (routing/escalation stay as-is). Only the dot changes.

    Generalized 2026-07-27 after Diego reported the same mismatch on the rest
    of the recovery family: "messages saying that components are up with an
    amber indication (should be green)". GATEWAY_STARTED and
    WATCHDOG_RECOVERED wore a yellow dot beside their own green icon;
    WATCHDOG_PROBE_TRANSITION → healthy wore the amber HIGH dot. Same scoping
    as the original: PRESENTATIONAL ONLY, priority/routing/escalation
    untouched.
    """
    if verdict is not None:
        return marker_for_verdict(verdict, event.priority)
    if event.event_type in _ALWAYS_RECOVERY_TYPES:
        return PRIORITY_EMOJI[Priority.LOW]  # 🟢 — recovery, not an alert
    when = _RECOVERY_WHEN.get(event.event_type)
    if when is not None:
        field, recovered_value = when
        if (event.payload or {}).get(field) == recovered_value:
            return PRIORITY_EMOJI[Priority.LOW]  # 🟢 — recovery, not an alert
    return priority_dot(event.priority)


def event_icon(event: Event) -> str:
    """Return the icon for an event.

    For mailbox_message, use inner message_type when available.
    """
    if event.event_type == EventType.MAILBOX_MESSAGE:
        inner_type = (event.payload or {}).get("message_type", "")
        return MAILBOX_INNER_EMOJI.get(inner_type, EVENT_TYPE_EMOJI[EventType.MAILBOX_MESSAGE])
    return EVENT_TYPE_EMOJI.get(event.event_type, "")


def _short_time(iso_ts: str) -> str:
    """Format ISO timestamp as HH:MM UTC. Falls back to raw on parse error."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.strftime("%H:%M UTC")
    except Exception:
        return iso_ts


def format_header(
    event: Event,
    verdict: OutcomeVerdict | None = None,
) -> str:
    """Top-line header with an optional normalized textual outcome.

    For mailbox_message events, surfaces the inner message_type and includes
    sender -> recipient: '🟡 UNKNOWN 📊 SCORE_RESULT — matcher → main · 14:37 UTC'.
    Legacy callers that omit ``verdict`` retain the historical unlabeled header.
    """
    dot = header_dot(event, verdict)
    label = f" {verdict.state.value.upper()}" if verdict is not None else ""
    icon = event_icon(event)
    ts = _short_time(event.timestamp)

    if event.event_type == EventType.MAILBOX_MESSAGE:
        p = event.payload or {}
        inner_type = p.get("message_type", "MAILBOX_MESSAGE")
        sender = p.get("from", "?")
        recipient = p.get("to", "?")
        return f"{dot}{label} {icon} {inner_type} — {sender} → {recipient} · {ts}"

    return (
        f"{dot}{label} {icon} {event.event_type.type_string.upper()} — "
        f"{event.source} · {ts}"
    )


def format_event_message(
    event: Event,
    body: str,
    verdict: OutcomeVerdict | None = None,
) -> str:
    """Full formatted message for Telegram: header + separator + body."""
    header = format_header(event, verdict)
    if body:
        return f"{header}\n{SEPARATOR}\n{body}"
    return header


# WhatsApp header titles in plain language (2026-07-11 operator feedback:
# "what the heck is a WATCHDOG_BURST????"). WhatsApp is Diego's phone-
# lockscreen surface — enum names like WATCHDOG_PROBE_TRANSITION are
# system jargon there. Telegram keeps the raw enum names (format_header)
# because its topics are the operator/diagnostic surface. Types not in
# this map fall back to the enum name.
WHATSAPP_TITLE_BY_EVENT = {
    EventType.WATCHDOG_BURST:              "SYSTEM HEALTH ALERT",
    EventType.WATCHDOG_PROBE_TRANSITION:   "SYSTEM HEALTH ALERT",
    EventType.WATCHDOG_SILENCE_ALERT:      "AGENT WENT QUIET",
    EventType.AGENT_FAILURE_CLUSTER:       "REPEATED FAILURES",
    EventType.GATEWAY_HEALTH:              "GATEWAY HEALTH",
    EventType.CREDENTIAL_LOSS:             "CREDENTIAL LOST",
    EventType.CRON_FAILED_CONSECUTIVE:     "CRON JOB FAILING",
    EventType.AGENT_ERROR:                 "AGENT ERRORS",
    EventType.DEVFLOW_BUILD_FAILED:        "BUILD FAILED",
    EventType.DEVFLOW_PR_REVIEW_REQUESTED: "PR REVIEW NEEDED",
    EventType.DEVFLOW_APPROVAL_REQUESTED:  "DEVFLOW APPROVAL NEEDED",
    EventType.APPROVAL_REQUEST:            "APPROVAL NEEDED",
    EventType.APPLY_PACKET:                "APPLY PACKET READY",
    EventType.CRITIC_PROPOSAL:             "CRITIC PROPOSAL",
    EventType.INTERVIEW_SIGNAL:            "INTERVIEW SIGNAL",
    EventType.OFFER_SIGNAL:                "JOB OFFER",
    EventType.SECRET_DETECTED:             "SECRET DETECTED",
    EventType.RESOURCE_PRESSURE:           "RESOURCE PRESSURE",
    EventType.CODE_DRIFT:                  "STALE CODE RUNNING",
    EventType.DIGEST_GENERATED:            "MORNING DIGEST",
    EventType.BOOT_SUMMARY:                "BOOT PROBLEMS",
}


def format_whatsapp_header(
    event: Event,
    verdict: OutcomeVerdict | None = None,
) -> str:
    """WhatsApp header: like format_header but with a plain-language title."""
    title = WHATSAPP_TITLE_BY_EVENT.get(event.event_type)
    if title is None or event.event_type == EventType.MAILBOX_MESSAGE:
        return format_header(event, verdict)
    dot = header_dot(event, verdict)
    label = f" {verdict.state.value.upper()}" if verdict is not None else ""
    icon = event_icon(event)
    ts = _short_time(event.timestamp)
    return f"{dot}{label} {icon} {title} — {event.source} · {ts}"


def format_whatsapp_message(
    event: Event,
    body: str,
    verdict: OutcomeVerdict | None = None,
) -> str:
    """Compact formatted message for WhatsApp: header + body, no separator.

    WhatsApp is a scanning medium; body should already be concise. Caller
    decides whether to append 'Details in Telegram.'
    """
    header = format_whatsapp_header(event, verdict)
    if body:
        return f"{header}\n{body}"
    return header


# --- Plain-language alert bodies (2026-07-11) --------------------------------
# Shared by WhatsAppEscalator (compact) and TelegramNotifier (full detail) so
# both surfaces tell the same story. Contract: every function returns complete
# sentences an operator can act on from a phone — NEVER raw payload JSON.
# Payload shapes come from profiles/watchdog/workspace/watchdog_sweep.py
# (_detect_transitions / _detect_silences) and events/producers/health_monitor.py.

# Probe tiers as assigned by laptop-monitor status.json. Anything not
# explicitly "optional" is treated as operator-actionable (fail-open on
# unknown/missing tiers so producer drift degrades to noisy, not silent).
_TIER_RANK = {"critical": 0, "important": 1, "optional": 2}


def format_duration(seconds) -> str:
    """Render a second count as '45s' / '8m 29s' / '2h 5m' / '2d 19h'."""
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return str(seconds)
    s = max(s, 0)
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m {sec}s" if sec else f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def humanize_health_detail(detail: str) -> str:
    """Translate a raw requests/urllib3 error string into plain language.

    'HTTPConnectionPool(host=..., port=3000): Max retries exceeded ...
    NewConnectionError(...)' reads as gibberish on a phone; render the
    diagnosis ('connection refused — nothing listening on 127.0.0.1:3000')
    instead. Unrecognized details pass through trimmed to one line.
    """
    d = str(detail or "").strip()
    if not d:
        return ""
    m = re.search(r"host='([^']+)'(?:,\s*port=(\d+))?", d)
    where = ""
    if m:
        where = f"{m.group(1)}:{m.group(2)}" if m.group(2) else m.group(1)
    if "Read timed out" in d:
        base = "it accepted the connection but never answered (timed out)"
    elif (
        "Failed to establish a new connection" in d
        or "Connection refused" in d
        or "NewConnectionError" in d
        or "Max retries exceeded" in d
    ):
        base = "connection refused — nothing is listening"
    elif "getaddrinfo" in d or "Name or service not known" in d:
        base = "DNS lookup failed"
    elif d.startswith("HTTP "):
        return f"health endpoint returned {d}"
    else:
        return d.splitlines()[0][:160]
    return f"{base} on {where}" if where else base


def watchdog_burst_body(payload: dict, *, max_listed: int = 5,
                        aggregate_optional: bool = True) -> str:
    """Plain-language summary of a coalesced watchdog_burst payload.

    A 'burst' is the watchdog sweep seeing 2+ monitored probes change state
    in the same pass. Lead with what is FAILING (critical first), aggregate
    optional-tier flaps and recoveries into counts. With
    aggregate_optional=False (Telegram), optional-tier failures are listed
    individually like the rest.
    """
    transitions = [t for t in (payload.get("transitions") or [])
                   if isinstance(t, dict)]
    count = payload.get("count") or len(transitions)
    if not transitions:
        return (f"{count} monitored services changed state in one sweep "
                f"(no probe detail attached).")

    # "unknown" = the probe was skipped this pass (monitor over its time
    # budget), not a verdict. Newer sweeps drop these at the producer; this
    # keeps older/in-flight bursts honest instead of calling skips failures.
    skipped = [t for t in transitions if t.get("after") == "unknown"]
    failing = [t for t in transitions
               if t.get("after") not in ("healthy", "unknown")]
    recovered = [t for t in transitions if t.get("after") == "healthy"]

    skipped_note = (f"({len(skipped)} probes skipped this pass — health "
                    f"monitor over its time budget, not real failures)"
                    if skipped else "")

    if not failing and not recovered:
        return (f"No real state changes — {len(skipped)} probes were skipped "
                f"this pass because the health monitor ran over its time "
                f"budget. Nothing is known to be down.")

    if not failing:
        names = ", ".join(t.get("probe", "?") for t in recovered[:3])
        more = f" +{len(recovered) - 3} more" if len(recovered) > 3 else ""
        text = (f"Good news — all {len(recovered)} changes were recoveries: "
                f"{names}{more}.")
        return f"{text}\n{skipped_note}" if skipped_note else text

    if aggregate_optional:
        listed = [t for t in failing if t.get("tier") != "optional"]
        optional_failing = [t for t in failing if t.get("tier") == "optional"]
    else:
        listed = list(failing)
        optional_failing = []
    listed.sort(key=lambda t: _TIER_RANK.get(t.get("tier"), 1))

    lines = []
    if recovered:
        lines.append(f"{len(failing)} checks failing, {len(recovered)} recovered:")
    else:
        plural = "s" if len(failing) != 1 else ""
        lines.append(f"{len(failing)} health check{plural} failing:")

    for t in listed[:max_listed]:
        line = f"✗ {t.get('probe', '?')}: {t.get('before', '?')} → {t.get('after', '?')}"
        if t.get("tier") == "critical":
            line += " [critical]"
        detail = str(t.get("detail") or "").strip()
        if detail:
            line += f" — {detail[:90]}"
        lines.append(line)
    hidden = len(listed) - max_listed
    if hidden > 0:
        lines.append(f"…and {hidden} more failing")
    if optional_failing:
        plural = "s" if len(optional_failing) != 1 else ""
        lines.append(f"(+{len(optional_failing)} low-priority probe flap{plural})")
    if recovered:
        names = ", ".join(t.get("probe", "?") for t in recovered[:3])
        more = f" +{len(recovered) - 3} more" if len(recovered) > 3 else ""
        lines.append(f"✓ Recovered: {names}{more}")
    if skipped_note:
        lines.append(skipped_note)
    return "\n".join(lines)


def watchdog_self_degraded_body(payload: dict) -> str:
    """The watchdog itself can't do its job — say why in plain language."""
    reason = str(payload.get("reason") or "unspecified").strip()
    lines = [f"The health monitor itself is degraded: {reason}."]
    skipped = payload.get("skipped_probes")
    if skipped:
        lines.append(f"{skipped} probes were not checked this pass "
                     f"(monitor ran over its time budget — usually resource "
                     f"pressure). Service states are unchanged, not down.")
    age = payload.get("age_seconds")
    if age is not None:
        lines.append(f"status.json hasn't updated in "
                     f"{format_duration(age)} — laptop-monitor may be stuck.")
    path = payload.get("path")
    if path:
        lines.append(f"File: {path}")
    return "\n".join(lines)


def probe_transition_body(payload: dict) -> str:
    """One monitored probe changed state — say which and what it means."""
    probe = payload.get("probe", "?")
    before = payload.get("before", "?")
    after = payload.get("after", "?")
    detail = str(payload.get("detail") or "").strip()
    if after == "healthy":
        text = f"✓ {probe} recovered ({before} → healthy)."
    else:
        text = f"✗ {probe} went {before} → {after}."
        if payload.get("tier") == "critical":
            text += " This is a critical check."
    if detail:
        text += f" {detail[:140]}"
    return text


def silence_alert_body(payload: dict) -> str:
    """An agent missed its expected reporting cadence — say for how long."""
    source = payload.get("source", "?")
    expected = format_duration(payload.get("expected_cadence_seconds"))
    since = payload.get("time_since_last_seconds")
    last_seen = payload.get("last_seen")
    if since is None or last_seen is None:
        return (f"{source} has never been heard from "
                f"(expected to report every {expected}).")
    text = (f"{source} went quiet — nothing heard for {format_duration(since)} "
            f"(normally reports every {expected}; last seen {_short_time(last_seen)}).")
    if payload.get("severity") == "dormant":
        text += " Quiet for 3×+ its normal cadence — likely dead, not just slow."
    return text


def failure_cluster_body(payload: dict) -> str:
    """An agent failed repeatedly in a row — say who and how many times."""
    source = payload.get("source", "?")
    size = payload.get("cluster_size", "?")
    last_type = payload.get("last_event_type", "?")
    last_ts = payload.get("last_timestamp")
    when = f" at {_short_time(last_ts)}" if last_ts else ""
    return (f"{source} has failed {size} times in a row "
            f"(latest: {last_type}{when}). Something is stuck — needs a look.")


def partial_backlog_body(payload: dict, *, max_ids: int = 10) -> str:
    """Plain-language summary of a TRACKER_PARTIAL_BACKLOG alert.

    A "partial" is a tracker approve/reject/archive intent whose pipeline.json
    write (the canonical store) succeeded but whose Postgres mirror (:4100)
    did not — so the dashboard and Postgres lag the real pipeline until the
    intent is re-driven. The idempotency key stays unburned, so every partial
    is safe to re-drive. See events/producers/partial_backlog_monitor.py.

    Before this (2026-07-18 operator feedback: "these are cryptic and don't
    really mean anything"), TRACKER_PARTIAL_BACKLOG hit TelegramNotifier's
    generic fallback, which splatted count/threshold/oldest_age_seconds/
    capped_count/sample_job_ids as raw key:value lines — a wall of numbers and
    full UUIDs that said nothing about what was wrong or what to do.
    """
    p = payload or {}
    count = p.get("count", "?")
    threshold = p.get("threshold")
    oldest = p.get("oldest_age_seconds")
    ids = [str(j) for j in (p.get("sample_job_ids") or [])]

    try:
        verb = "update is" if int(count) == 1 else "updates are"
    except (TypeError, ValueError):
        verb = "updates are"

    lines = [
        f"{count} tracker {verb} stuck half-applied — each was saved to the "
        f"pipeline but never mirrored to Postgres (:4100), so the dashboard "
        f"and Postgres are out of sync with the real pipeline. They're safe "
        f"to re-drive: the idempotency keys are unburned."
    ]
    if oldest is not None:
        lines.append(f"Oldest has been waiting {format_duration(oldest)}.")
    if threshold is not None:
        lines.append(f"(Alert fires above {threshold} stuck updates.)")
    if ids:
        shown = [j[:8] for j in ids[:max_ids]]
        try:
            total = int(count)
            scope = f"{len(shown)} of {total}" if total > len(shown) else str(len(shown))
        except (TypeError, ValueError):
            scope = str(len(shown))
        lines.append(f"Sample jobs ({scope}): {', '.join(shown)}")
    return "\n".join(lines)


def code_drift_body(p: dict) -> str:
    """Plain-language, branch-aware CODE_DRIFT diagnosis."""
    p = p or {}
    repo = p.get("repo", "~/.hermes/agent-src")
    trunk_ref = p.get("trunk_ref") or "refs/heads/main"
    trunk_name = p.get("trunk_name") or trunk_ref.rsplit("/", 1)[-1]
    trunk = p.get("trunk", p.get("main", "?"))
    branch = p.get("branch") or ""
    where = f"on {branch}" if branch and branch != "HEAD" else "detached"
    key = p.get("key") or p.get("repo_name")
    label = f"{key} checkout" if key else "Deployed checkout"

    if p.get("status") == "resolved":
        if p.get("inert"):
            return (f"{label} drift no longer touches executed code "
                    f"(still not merged with {trunk_name}).")
        return f"{label} back in sync with {trunk_name} @ {trunk}"

    state = p.get("state", "?")
    if state in {"trunk_missing", "misconfigured"}:
        detail = p.get("detail") or f"trunk ref {trunk_ref} does not resolve"
        lines = [
            f"CODE DRIFT IS UNMEASURABLE on {repo}: {detail}. "
            f"The checkout is {where}; treat this repo as unmonitored.",
        ]
        if state == "trunk_missing" or "trunk ref" in detail:
            lines.append(
                f"Fix: point the watched-repo entry at the real trunk ref "
                f"(git -C {repo} branch --list), then restart the gateway."
            )
        return "\n".join(lines)

    lines = []
    if state == "behind":
        lines.append(
            f"{label} ({where}) LAGS {trunk_name} by "
            f"{p.get('behind_count', '?')} commit(s) — landed fixes are NOT running."
        )
        for subj in (p.get("missed_subjects") or [])[:5]:
            lines.append(f"  missed: {subj}")
    elif state == "ahead":
        lines.append(
            f"{label} ({where}) is AHEAD of {trunk_name} by "
            f"{p.get('ahead_count', '?')} commit(s) — the working tree carries "
            "unlanded state."
        )
    else:
        lines.append(
            f"{label} ({where}) has DIVERGED from {trunk_name} "
            f"(HEAD {p.get('head', '?')} vs {trunk_name} {trunk})."
        )

    changed = p.get("executed_changed")
    if changed is None:
        changed = p.get("executed_files") or []
    for path in changed[:5]:
        lines.append(f"  executed: {path}")
    if p.get("dirty"):
        lines.append("Working tree is DIRTY (uncommitted changes).")
    if state == "behind":
        if branch and branch not in {"HEAD", trunk_name}:
            lines.append(
                f"Fix: re-point {repo} from {branch} to {trunk_name}, then "
                "restart the gateway."
            )
        else:
            lines.append(
                f"Fix: git -C {repo} merge --ff-only {trunk_name}, "
                "then restart the gateway."
            )
    return "\n".join(lines)


def boot_summary_body(payload: dict, *, max_listed: int = 5) -> str:
    """Plain-language BOOT_SUMMARY body (2026-07-27).

    ~/laptop-start.ps1 emits this only when the logon boot had trouble, so the
    body leads with the damage: how many services came up out of how many, then
    the failed steps and error-severity anomalies as individual lines. Both
    ``failures`` and ``anomalies`` are LISTS, which the notifier's generic
    key:value fallback renders as Python list reprs — hence a dedicated body.

    Counts are rendered as given rather than recomputed from the lists: the
    producer's counts cover every step, while the lists carry only the ones it
    chose to name.
    """
    p = payload or {}
    boot_id = p.get("boot_id") or "?"
    state = str(p.get("state") or "?").upper()
    failures = [str(f).strip() for f in (p.get("failures") or []) if str(f).strip()]
    anomalies = [str(a).strip() for a in (p.get("anomalies") or []) if str(a).strip()]

    head = (f"Boot {boot_id} finished {state} — "
            f"{p.get('done', '?')}/{p.get('total', '?')} services up")
    extra = [f"{p[k]} {k}" for k in ("failed", "skipped") if p.get(k)]
    if extra:
        head += f" ({', '.join(extra)})"
    lines = [head + "."]

    for label, items, mark in (("failing step", failures, "✗"),
                               ("anomaly", anomalies, "⚠")):
        for item in items[:max_listed]:
            lines.append(f"{mark} {item}")
        hidden = len(items) - max_listed
        if hidden > 0:
            plural = "s" if hidden != 1 else ""
            lines.append(f"…and {hidden} more {label}{plural}")

    if not failures and not anomalies:
        lines.append("No failing steps were named.")
    lines.append("Full detail: tray Boot panel.")
    return "\n".join(lines)
