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
    EventType.CRON_COMPLETED:           "✔️",
    EventType.CRON_FAILED:              "💥",
    EventType.CRON_FAILED_CONSECUTIVE:  "🔥",
    EventType.CRON_STALE:               "⌛",
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
