"""Emoji + visual formatting helpers for event-bus notifications.

Provides priority dots, event-type icons, and header/body builders used by
TelegramNotifier, TelegramMirror, WhatsAppEscalator, and DigestComposer.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from events.outcomes import OutcomeState, OutcomeVerdict, marker_for_verdict
from events.schema import Event, EventType, Priority

logger = logging.getLogger(__name__)

# Priority -> colored dot (matches severity)
PRIORITY_EMOJI = {
    Priority.CRITICAL: "🔴",
    Priority.HIGH:     "🟠",
    Priority.NORMAL:   "🟡",
    Priority.LOW:      "🟢",
}

# Event type -> icon. DERIVED, not hand-maintained: the icon is a required
# field on the EventType member itself (see events/schema.py, which also
# carries the per-glyph rationale that used to live in this table). The name
# is kept because callers and tests import it, but it is a read-only view --
# there is no second table left to keep in sync, which is the whole point.
# Four separate drifts (2026-04-27 x2, 2026-05-29, 2026-08-11) came from this
# being an independently-edited dict.
EVENT_TYPE_EMOJI: Mapping[EventType, str] = MappingProxyType(
    {et: et.icon for et in EventType}
)

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

    Never returns "" for a real EventType: the icon is a required member field
    validated at class-creation time (events/schema.py), so there is no
    "missing icon" branch left to fall through. MAILBOX_INNER_EMOJI is a
    genuinely partial override keyed by an arbitrary producer-supplied string,
    hence its explicit fallback.
    """
    if event.event_type == EventType.MAILBOX_MESSAGE:
        inner_type = (event.payload or {}).get("message_type", "")
        return MAILBOX_INNER_EMOJI.get(inner_type, EventType.MAILBOX_MESSAGE.icon)
    return event.event_type.icon


def _short_time(iso_ts: str) -> str:
    """Format ISO timestamp as HH:MM UTC. Falls back to raw on parse error."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.strftime("%H:%M UTC")
    except Exception:
        return iso_ts


_STATE_LABELS = {
    OutcomeState.NO_WORK: "NO WORK",
}


def _label_is_noise(event: Event, verdict: OutcomeVerdict) -> bool:
    """True iff the verdict label says nothing worth a word in the header.

    Exactly one case (2026-08-19, found by the AGENT_NOTE live delivery test —
    no offline assertion covered it): an agent note carrying no status/outcome
    key evaluates to UNKNOWN and rendered as "UNKNOWN AGENT_NOTE", which reads
    as a determination that FAILED rather than one that was never called for.
    A note is a statement; it has no operation behind it to have an outcome.

    Deliberately NOT generalised to every type. For an operation — a cron run,
    a probe, a delivery — UNKNOWN is real information: the result could not be
    established, which is the distinction the failure-wins outcome contract
    exists to preserve. Suppressing it there would hide a signal, so this stays
    a one-type exception rather than becoming a rule about UNKNOWN.

    The DOT is untouched: header_dot() derives it separately via
    marker_for_verdict(), so a note keeps its priority colour.
    """
    return (event.event_type is EventType.AGENT_NOTE
            and verdict.state is OutcomeState.UNKNOWN)


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
    if verdict is None or _label_is_noise(event, verdict):
        label = ""
    else:
        label = f" {_STATE_LABELS.get(verdict.state, verdict.state.value.upper())}"
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
    EventType.CONTAINER_CRASH_LOOP:        "CONTAINER CRASH-LOOPING",
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
    EventType.FOLLOWUP_DUE:                "FOLLOW-UP DUE",
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
    if reason == "laptop-monitor status.json stale":
        path = payload.get("path") or r"C:\Users\diego\architecture-map\status.json"
        age = format_duration(payload.get("age_seconds"))
        return "\n".join([
            "Laptop monitor stopped updating its health snapshot.",
            f"File: {path}",
            f"Last update: {age} ago (this alert starts after 10m).",
            "Service health shown by the watchdog may be out of date; "
            "this does not mean those services are down.",
            "No action is usually needed for one alert; the watchdog will "
            "check again automatically. If this persists, check laptop-monitor.",
        ])

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


def resource_pressure_body(payload: dict) -> str:
    """Host resource pressure — shared by Telegram and the WhatsApp escalator.

    Leads with the DISK line because that is where the severity band lives, and
    the band is the only part of this message the repeat guard can see:
    ``normalize_for_fingerprint`` collapses digit runs to "N", so before the
    band existed (2026-08-14) a single fingerprint covered every disk_low event
    from 56.63 GB free down to 0.0 GB — 101 of them below 5 GiB, 13 at exactly
    zero — and a dying disk was suppressed exactly like a healthy one. The band
    label is LETTERS, so crossing an edge mints a new message; two readings
    inside one band still collapse, which is what keeps a filling disk from
    paging every tick.

    Also the WhatsApp lane's only readable rendering of this type. Without it
    the escalator falls to its scalar fallback, which takes ``scalars[:6]`` in
    payload order and stops BEFORE ``disk_c_free_gb`` — a disk-full page that
    never mentions the disk. Never observed only because disk_critical had
    fired zero times when this landed.
    """
    p = payload or {}
    reasons = ", ".join(p.get("reasons") or []) or "?"
    disk = f"C: free: {p.get('disk_c_free_gb', '?')} GB"
    band = p.get("disk_band")
    if band:
        disk += f" — {str(band).upper()}"
        edge = p.get("disk_band_edge_gb")
        if edge is not None:
            disk += f" (under {edge:g} GiB)"

    # Commit and phys carry bands too since 2026-08-20, for exactly the reason
    # the disk band exists: the guard sees only letters. Before this, commit
    # 85.7% and commit 99.1% were ONE fingerprint, so a climb toward commit
    # exhaustion was suppressed as a verbatim repeat. Rendered as "over N%"
    # (the disk line reads "under N GiB") because these axes worsen upward.
    commit = (f"Commit: {p.get('commit_pct', '?')}% "
              f"({p.get('commit_used_gb', '?')}/{p.get('commit_limit_gb', '?')} GB)")
    commit_band = p.get("commit_band")
    if commit_band:
        commit += f" — {str(commit_band).upper()}"
        commit_edge = p.get("commit_band_edge_pct")
        if commit_edge is not None:
            commit += f" (over {commit_edge:g}%)"

    # phys was in ``reasons`` but rendered NOWHERE until 2026-08-20 — a paging
    # alert that never said how much RAM was left. The 2026-07-16 axis landed
    # without touching this renderer.
    phys = (f"Phys: {p.get('phys_used_pct', '?')}% used "
            f"({p.get('phys_available_gb', '?')} GB avail)")
    phys_band = p.get("phys_band")
    if phys_band:
        phys += f" — {str(phys_band).upper()}"
        phys_edge = p.get("phys_band_edge_pct")
        if phys_edge is not None:
            phys += f" (over {phys_edge:g}%)"

    return (
        f"⚠ Resource pressure: {reasons}\n"
        f"{disk}\n"
        f"{commit}\n"
        f"{phys}\n"
        f"Pagefile: {p.get('pagefile_allocated_gb', '?')} GB "
        f"(+{p.get('pagefile_growth_gb_10min', '?')} GB/10m)"
    )


def container_crash_loop_body(payload: dict) -> str:
    """A container burned its 24h restart budget — say how many, and that it may read green.

    Leads with the fact that makes this event exist at all: the tray row is
    very often HEALTHY right now. laptop-monitor's churn verdict is a one-pass
    RestartCount delta that self-clears 600s after the last restart, so a
    container that restarted 264 times in one morning renders
    "running, RestartCount stable (266)" and green (observed 2026-08-10T08:15
    for hindsight-app). Without that sentence an operator reads this alert,
    glances at a green tray, and dismisses it as stale.
    """
    name = payload.get("container", "?")
    restarts = payload.get("restarts_24h", "?")
    threshold = payload.get("threshold", "?")
    state = str(payload.get("tray_state") or "unknown").upper()
    detail = str(payload.get("tray_detail") or "").strip()
    text = (f"🔁 Container {name} restarted {restarts} times in the last 24h "
            f"(budget {threshold}).")
    if state == "HEALTHY":
        text += (" The monitor row reads HEALTHY right now — this is the 24h "
                 "total, not the current sample, so a green tray does not "
                 "clear it.")
    else:
        text += f" The monitor row currently reads {state}."
    if detail:
        text += f" Latest probe detail: {detail[:160]}"
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
    """Render canonical cluster diagnostics with legacy payload fallbacks."""
    source = payload.get("source")
    size = payload.get("count", payload.get("cluster_size"))
    last_type = (
        payload.get("failure_type")
        or payload.get("last_event_type")
        or payload.get("exception_type")
    )
    last_ts = payload.get("last_seen") or payload.get("last_timestamp")
    subject = f"{source} has" if source else "An agent has"
    if size is not None:
        headline = f"{subject} failed {size} times in a row"
    else:
        headline = f"{subject} failed repeatedly"
    if last_type:
        when = f" at {_short_time(last_ts)}" if last_ts else ""
        headline += f" (latest: {last_type}{when})"
    lines = [headline + "."]

    qualifiers = []
    exception_type = payload.get("exception_type")
    if exception_type and exception_type != last_type:
        qualifiers.append(f"exception {exception_type}")
    if payload.get("error_code"):
        qualifiers.append(f"error code {payload['error_code']}")
    if payload.get("phase"):
        qualifiers.append(f"phase {payload['phase']}")
    if payload.get("deadline_seconds") is not None:
        qualifiers.append(
            f"deadline {format_duration(payload['deadline_seconds'])}"
        )
    if qualifiers:
        lines.append(" · ".join(qualifiers) + ".")
    if payload.get("latest_cause"):
        lines.append(f"Latest cause: {payload['latest_cause']}")
    lines.append("Something is stuck — needs a look.")
    return "\n".join(lines)


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


def _utc_short_time(iso_ts) -> str:
    """``HH:MM UTC``, actually CONVERTED. Empty string if unparseable.

    Distinct from :func:`_short_time`, which formats the stamp in whatever
    offset it already carries while labelling the result "UTC". That is safe
    for bus timestamps — every one is ``datetime.now(timezone.utc)`` — but
    not for ``ran_at``, which the executions ledger stamps in LOCAL
    wall-clock via ``hermes_time.now()``. Unconverted, a run that began at
    17:00 UTC prints as "13:00 UTC" on this box.
    """
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if dt.tzinfo is None:
        # A naive stamp lost its offset upstream; it was local when written.
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).strftime("%H:%M UTC")


def cron_stale_body(p: dict) -> str:
    """Plain-language CRON_STALE body, keyed on ``scope``.

    One event type carries four different claims, and only ``scope``
    separates them. Without this branch all four reached Telegram through
    ``_format_payload``'s generic ``key: value`` fallback, which buried
    ``scope`` mid-list and splatted correlation UUIDs no operator can act on
    (the same defect the SECRET_DETECTED branch fixed for ``finding_hash``).
    A genuine wedge and a restart casualty then read almost identically.

    The four, in descending severity:

    - ``ticker`` — the SCHEDULER is gone; no job can fire at all. The
      ``__ticker__`` job_id is a sentinel, so rendering it as a stuck job
      would hide a total outage behind one job's name.
    - (no scope) — the original wedge alert: a real job started and never
      finished. The only one that means "something is stuck right now".
    - ``gateway_stopped`` — a shutdown cut the run short. Attributed to a
      SPECIFIC shutdown, so it can say which one and how far in.
    - ``owner_exited`` — the ledger found the run's owner dead without a
      terminal state. It cannot say what killed it or how far in, so this
      body must not imply either. See the deliberate absence of
      ``age_seconds`` in ``CronScheduler._emit_interrupted_cron_stale``.
    """
    p = p or {}
    scope = p.get("scope")
    job_name = p.get("job_name") or p.get("job_id") or "?"

    if scope == "ticker":
        return (
            "The cron SCHEDULER is not running — no job can fire until the "
            "gateway is restarted.\n"
            f"Ticker heartbeat {format_duration(p.get('age_seconds', 0))} old "
            f"(threshold {format_duration(p.get('threshold_seconds', 0))})."
        )

    if scope == "gateway_stopped":
        reason = p.get("exit_reason") or "reason not recorded"
        return (
            f"{job_name} was cut short by a gateway shutdown ({reason}) "
            f"{format_duration(p.get('age_seconds', 0))} into the run.\n"
            "Whether its side effects completed is not recorded."
        )

    if scope == "owner_exited":
        # No duration: the ledger knows only that the owner died, not when
        # the kill landed. now-minus-ran_at would silently bill however long
        # the box was down to the run.
        when = _utc_short_time(p.get("ran_at"))
        return (
            f"{job_name}'s owner exited before recording an outcome — "
            "whether the run finished is unknown."
            + (f"\nStarted {when}." if when else "")
        )

    return (
        f"{job_name} has been running "
        f"{format_duration(p.get('age_seconds', 0))} with no result "
        f"(threshold {format_duration(p.get('threshold_seconds', 0))})."
    )


# AGENT_NOTE body cap. Telegram's own message limit is 4096 characters and
# the header + separator eat some of that; 3000 leaves room and matches the
# spirit of CRON_SUMMARY_MAX_CHARS (1500) without truncating a genuine
# multi-paragraph verdict. Truncation is always ANNOUNCED — see below.
AGENT_NOTE_MAX_CHARS = 3000

# Payload keys that steer the header (verdict machinery, events/outcomes.py)
# or the routing (events/routing_policy.py) rather than the prose. Echoing
# them back into the body would be noise, so the no-headline fallback skips
# them — but ONLY them, so nothing the caller actually wrote is hidden.
_AGENT_NOTE_STEERING_KEYS = frozenset({
    "headline", "detail", "attention",
    "status", "reason", "outcome", "result", "conclusion", "message_type",
    "action_required", "action_kind",
})


def agent_note_body(payload: dict) -> str:
    """Verbatim AGENT_NOTE body (2026-08-19).

    The one type carrying arbitrary agent-authored prose, so this function's
    whole job is to NOT interpret it: ``headline`` then ``detail``, newlines
    and all, exactly as AGENT_ITERATION renders a structured ``brief``.

    Two rules earn their place, and both exist because of the defect that
    motivated the type (see docs/superpowers/specs/2026-08-19-agent-note-
    event-type-design.md):

    * **Never render a confident blank.** A payload with no prose falls
      through to key:value lines rather than a plausible-looking empty
      message. boot_summary_body rendering "Boot ? finished ?" for a payload
      it did not understand is what let two distinct messages collapse onto
      one RepeatGuard fingerprint and vanish.
    * **Truncation is announced.** A silent cut would recreate that same
      failure in miniature — two long notes sharing a prefix would render
      identically and the second would be suppressed.
    """
    p = payload if isinstance(payload, dict) else {}

    def _text(value) -> str:
        return value.strip() if isinstance(value, str) else ""

    headline = _text(p.get("headline"))
    # rstrip only: leading indentation can be meaningful in a pasted block.
    detail = p.get("detail")
    detail = detail.rstrip() if isinstance(detail, str) else ""

    parts = [part for part in (headline, detail) if part]
    if parts:
        body = "\n".join(parts)
    else:
        # No prose. Surface whatever the caller DID send, minus the keys that
        # only steer header/routing, so a malformed note is diagnosable from
        # the message itself instead of requiring a bus query.
        leftovers = [
            f"{k}: {v}" for k, v in p.items()
            if k not in _AGENT_NOTE_STEERING_KEYS and v not in (None, "", [], {})
        ]
        if leftovers:
            body = "(agent note with no headline)\n" + "\n".join(leftovers[:10])
        else:
            body = "(empty agent note — the producer sent no headline or detail)"

    if len(body) <= AGENT_NOTE_MAX_CHARS:
        return body
    # Announce the loss with the exact character count, and keep the count out
    # of the kept text so the note stays readable up to the cut.
    dropped = len(body) - AGENT_NOTE_MAX_CHARS
    note = f"\n…truncated {dropped} chars"
    keep = max(0, AGENT_NOTE_MAX_CHARS - len(note))
    return body[:keep] + note


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


# ── runtime drift signal (report, never repair) ─────────────────────────────
#
# EVENT_TYPE_EMOJI has drifted from EventType four times (2026-04-27 twice,
# 2026-05-29, 2026-08-11 — the last hid all twelve DevFlow Delegation Plane
# icons for five days). events.coverage now guards that pairing, but only where
# a developer is: the pre-commit hook, pytest, `python -m events.coverage`. All
# three are bypassed by `git commit --no-verify` and by any checkout where
# pre-commit was never installed, and none of them make a *running gateway* say
# anything — event_icon() just returns "" and the header renders with a
# double-space gap.
#
# So the process shipping the incomplete table reports it, once, at import.
# A Workday listbox answer is matched against the tenant's OWN option text, so
# an answer that is not verbatim one of these labels is never clicked: the run
# re-stalls looking exactly like nobody answered. The labels therefore have to
# reach Diego intact, which is what BLOCKED_QUESTION's `options` key carries
# (applier `blocked_question_payload`, MailboxTranslator `_blocked_question_
# options`). Rendering them as a numbered list rather than an inline comma run
# is not cosmetic: `question` is capped at MailboxWatcher._summarize's 200
# chars, and the Capital One list measured exactly 200 inside it -- one longer
# tenant list and the tail is lost to an ellipsis.
def blocked_question_options(payload: Mapping, *, max_listed: int | None = None):
    """(shown, hidden) -- the option labels to print and how many were dropped.

    Tolerant of a producer that sends non-strings: the labels are clicked as
    text downstream, so anything that stringifies is better than dropping the
    choice silently.
    """
    raw = payload.get("options") if isinstance(payload, Mapping) else None
    if not isinstance(raw, (list, tuple)):
        return [], 0
    labels = [str(o).strip() for o in raw if str(o).strip()]
    if max_listed is not None and len(labels) > max_listed:
        return labels[:max_listed], len(labels) - max_listed
    return labels, 0


# The applier's stop-gap framing, verbatim: `blocked_question_text` in
# profiles/applier/workspace/tmp_ready_sweep_cron.py builds
# "Answer needed for <label>. Options: a, b, c" because until the translator
# carried `options` the labels could only reach Diego inside `question`.
_INLINE_OPTIONS_MARKER = ". Options: "


def blocked_question_line(payload: Mapping) -> str:
    """The question sentence, with the producer's inline option run removed.

    Only when this rendering is about to print the labels as a list -- otherwise
    the inline run is the ONLY way they reach the reader and must stay. It is
    cut at the producer's own literal marker, so a `question` from any other
    producer, or one that never carried options, passes through untouched.

    Cutting at the marker rather than matching the exact tail is deliberate: the
    producer truncates `question` to MailboxWatcher._summarize's 200-char budget,
    so on a long tenant list the inline run arrives ALREADY ellipsised and no
    exact comparison could recognise it -- which is precisely the case where
    leaving a half-printed list above a complete one is most confusing.
    """
    question = str(payload.get("question") or "").strip() or "needs your input"
    shown, _hidden = blocked_question_options(payload, max_listed=None)
    if not shown:
        return question
    head, marker, _tail = question.rpartition(_INLINE_OPTIONS_MARKER)
    if marker and head.strip():
        return head.strip()
    return question


def blocked_question_options_block(
    payload: Mapping, *, max_listed: int | None = None
) -> str:
    """The numbered choice list appended to every blocked-question rendering.

    Empty string when the envelope offers nothing to choose from -- a free-text
    question, or an older producer that never emitted `options`. Both surfaces
    lead with their own sentence, so this is only the block. By default every
    tenant label is shown: hiding a tail choice makes the required verbatim
    answer impossible to send. ``max_listed`` remains available only for callers
    that have another complete source of labels to point at.
    """
    shown, hidden = blocked_question_options(payload, max_listed=max_listed)
    if not shown:
        return ""
    lines = ["Reply with EXACTLY one of these labels:"]
    lines += [f"{i}. {label}" for i, label in enumerate(shown, 1)]
    if hidden:
        lines.append(f"...and {hidden} more (see the attempt artifacts)")
    return "\n".join(lines)


# Non-fatal: a missing icon is cosmetic, and raising here would take the
# gateway down over it.
#
# This publishes the RECORD of what is missing. It must never back-fill
# EVENT_TYPE_EMOJI: coverage.TableSpec.resolve() reads this table *after*
# importing this module, so a table that healed itself here would make
# coverage_gaps() report zero forever and silently disarm the shipped guard.
#
# Since EVENT_TYPE_EMOJI became a view derived from EventType.icon, this call
# can no longer fire: a member with no icon fails at EventType class creation,
# so `import events.formatting` would not get this far. It stays as the runtime
# detector for the day the derivation is replaced by a hand-maintained dict —
# the same reason coverage.REQUIRED_TOTAL keeps its now-tautological entry.
# EVENT_TYPES_WITHOUT_ICON is therefore expected to be (); a non-empty value
# means the table stopped deriving.
from events.coverage import log_missing_members  # noqa: E402 - table must exist

EVENT_TYPES_WITHOUT_ICON = log_missing_members(
    EVENT_TYPE_EMOJI,
    "events.formatting.EVENT_TYPE_EMOJI",
    logger,
    # NOT the default "add one entry per type": this table is total by
    # construction (events.coverage.TOTAL_BY_CONSTRUCTION), so hand-adding the
    # entries would rebuild the exact parallel dict the derivation removed.
    fix=(
        "Fix: this table is derived from EventType.icon and cannot be partial "
        "unless the derivation was replaced. Restore it rather than adding the "
        "entries by hand"
    ),
)
