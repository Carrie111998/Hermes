"""Emoji + visual formatting helpers for event-bus notifications.

Provides priority dots, event-type icons, and header/body builders used by
TelegramNotifier, TelegramMirror, WhatsAppEscalator, and DigestComposer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

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


def format_header(event: Event) -> str:
    """Top-line header: '🟠 ⚠️ AGENT_ERROR — source · 05:02 UTC'.

    For mailbox_message events, surfaces the inner message_type and includes
    sender -> recipient: '🟡 📊 SCORE_RESULT — matcher → main · 14:37 UTC'.
    """
    dot = priority_dot(event.priority)
    icon = event_icon(event)
    ts = _short_time(event.timestamp)

    if event.event_type == EventType.MAILBOX_MESSAGE:
        p = event.payload or {}
        inner_type = p.get("message_type", "MAILBOX_MESSAGE")
        sender = p.get("from", "?")
        recipient = p.get("to", "?")
        return f"{dot} {icon} {inner_type} — {sender} → {recipient} · {ts}"

    return f"{dot} {icon} {event.event_type.type_string.upper()} — {event.source} · {ts}"


def format_event_message(event: Event, body: str) -> str:
    """Full formatted message for Telegram: header + separator + body."""
    header = format_header(event)
    if body:
        return f"{header}\n{SEPARATOR}\n{body}"
    return header


def format_whatsapp_message(event: Event, body: str) -> str:
    """Compact formatted message for WhatsApp: header + body, no separator.

    WhatsApp is a scanning medium; body should already be concise. Caller
    decides whether to append 'Details in Telegram.'
    """
    header = format_header(event)
    if body:
        return f"{header}\n{body}"
    return header
