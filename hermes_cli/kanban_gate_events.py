"""Human plan/deploy gate events: one vocabulary, one safe renderer (G11).

Hermes has more than one notification surface. The gateway notifier
(``gateway/kanban_watchers.py``) delivers to Telegram/Discord/Slack/…; the TUI
gateway (``tui_gateway/server.py``) delivers to the Desktop app, the TUI, and
the dashboard chat. Both claim rows out of ``task_events`` and both render them
for a person.

Everything about a *gate* event that both surfaces need lives here, so neither
can drift:

* :data:`PLAN_GATE_NOTIFY_KINDS` — the kinds ``kanban_db`` emits for a human
  plan/deploy gate. A human gate exists to STOP an agent, so a gate event is
  **never** a wake, an agent turn, or a dispatch on any surface.

  Whether it is *notified* depends on the subscription, and the two are not the
  same claim. A surface with a passive channel — a push-capable messaging
  adapter, the Desktop/TUI poller — displays it to a person. A subscription
  with no passive channel (a non-push adapter, whose only channel is the wake
  self-post that deliberately carries no gate wording) or one that declined it
  (``delivery_mode="wake"``) **cannot notify anyone at all**. There the outcome
  is explicit audited non-delivery: a :data:`GATE_UNDELIVERABLE_KIND` row
  saying nobody was told through that channel. That row is a *record*, not a
  delivery, and must never be described as one. The gate event itself always
  survives in ``task_events``, so the board remains the durable record.
* :func:`safe_display_value` — the outbound safety boundary for **every**
  event-derived value, not just free-text reasons.
* :func:`render_gate_event` — the one rendering of a gate event.

Why the safety boundary lives at render time and not at write time: these
payloads are written by ``park_for_plan_approval`` / ``release_plan_gate`` from
caller-supplied project ids, adapter/config-supplied operator labels, and
legacy rows that predate the current shape. The renderer deliberately accepts
partial and legacy payloads, so the renderer owns outbound safety.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Optional

# Emitted by ``hermes_cli.kanban_db``:
#   park_for_plan_approval()        → plan_awaiting_approval
#   release_plan_gate()             → plan_approved / plan_rejected
#   record_gate_release_refusal()   → gate_release_refused
#   _audit_gate_refusal()           → gate_release_refused
PLAN_GATE_NOTIFY_KINDS: tuple[str, ...] = (
    "plan_awaiting_approval",
    "plan_approved",
    "plan_rejected",
    "gate_release_refused",
)

# Written on a task when a gate event could NOT be passively delivered to a
# subscription: a non-push adapter has no passive channel, and a ``wake``-only
# subscriber declined the only one there is. Deliberately NOT a member of the
# notifier's TERMINAL_KINDS or WAKE_KINDS — it is an audit disposition, never
# itself a notification, so nothing claims it and it cannot recurse.
GATE_UNDELIVERABLE_KIND = "gate_notification_undeliverable"

GATE_EMOJI: dict[str, str] = {
    "plan_awaiting_approval": "⏳",
    "plan_approved": "✅",
    "plan_rejected": "🚫",
    "gate_release_refused": "⛔",
}

# Absolute local paths. Kept here (rather than imported from the gateway) so
# the TUI surface does not have to import the gateway to render safely.
#
# Two things the first version got wrong, both reproduced by review:
#   * a path segment containing a space ("/Users/me/My Secret/config.yaml")
#     matched only up to the space and leaked "Secret/config.yaml";
#   * only seven roots were covered, so /Volumes, /opt, /root, /Applications
#     and a "file://" URL survived intact.
#
# A space is consumed ONLY when a separator still follows it before the next
# delimiter. That continues a path through any number of spaced segments
# ("My Very Secret/config.yaml", "Rick Swindell\\Secret Folder\\key.txt") while
# still stopping at ordinary prose ("/Users/me/x and then the plan was
# approved"), which has no separator left to find.
#
# The first version allowed exactly ONE space, so a two-word directory leaked
# its tail — usually the sensitive half — and the Windows branch stopped dead
# at the first space.
_PATH_ROOTS = (
    "Users|home|root|Volumes|Applications|Library|System|opt|srv|mnt|media|"
    "private|tmp|var|etc|usr|workspace|data"
)
# POSIX: ordinary path characters, or a space that still has a "/" ahead.
_PATH_TAIL = r"(?:[^/\s,;'\"]|[ ](?=[^,;'\"\n]*?/))*"
# Windows: the same shape with a backslash as the separator.
_WIN_TAIL = r"(?:[^\\\s,;'\"]|[ ](?=[^,;'\"\n]*?\\))*"
_LOCAL_PATH_RE = re.compile(
    rf"(?:file://)?(?<![\w:])/(?:{_PATH_ROOTS})\b(?:/{_PATH_TAIL})*"
    rf"|[A-Za-z]:(?:\\{_WIN_TAIL})+"
)

# Redaction runs a lookahead per space, so a pathological value is quadratic
# in principle. Values here come from bounded event payloads, but the
# boundary must not depend on that: clamp before any scanning.
_MAX_SCAN_CHARS = 4096

# Everything outside printable text. C0 (minus the whitespace that
# ``str.split`` already folds), DEL, and C1 — the range that carries ANSI
# escapes, so a payload cannot repaint a terminal or a chat client.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")

# Field-appropriate bounds. An identifier is not a paragraph; a reason is.
IDENT_LIMIT = 64
REASON_LIMIT = 160

_EMPTY = ""


def safe_display_value(value: Any, *, limit: int = IDENT_LIMIT) -> str:
    """Make one event-derived value safe to send outside Hermes.

    Credential-shaped material and absolute local paths are redacted, control
    characters (ANSI escapes included) are dropped, all whitespace collapses to
    single spaces, and the result is bounded. Absent, malformed, list, mapping,
    and legacy values are all accepted — this never raises, because the caller
    is a notifier and a rendering failure must not become a delivery failure.

    The unsafe original is never returned, logged, or attached to an error.
    """
    if value is None:
        return _EMPTY
    try:
        if isinstance(value, Mapping):
            text = " ".join(
                f"{k}={v}" for k, v in list(value.items())[:8]
            )
        elif isinstance(value, (list, tuple, set, frozenset)):
            text = " ".join(str(v) for v in list(value)[:8])
        elif isinstance(value, (str, int, float, bool)):
            text = str(value)
        else:
            text = str(value)
    except Exception:
        # A __str__ that raises is a malformed value, not an outage. Say so
        # without echoing anything derived from it.
        return "[unrenderable]"

    if len(text) > _MAX_SCAN_CHARS:
        text = text[:_MAX_SCAN_CHARS]
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(
            text, force=True, redact_url_credentials=True
        )
    except Exception:
        # Redaction unavailable ⇒ nothing may leave. Fail closed: report the
        # field as withheld rather than emit an unredacted value.
        return "[redaction unavailable]"

    text = _LOCAL_PATH_RE.sub("[local path]", text)
    text = _CONTROL_RE.sub("", text)
    # Unicode format characters (Cf) carry bidi overrides and isolates
    # (U+202E, U+2066-2069) and zero-width joiners — display injection that is
    # invisible to the C0/C1 filter above. Surrogates (Cs) are worse than
    # invisible: a lone one makes the returned string raise inside an adapter's
    # UTF-8 encoder, which would turn a notification into an outage.
    if not text.isascii():
        text = "".join(
            ch for ch in text if unicodedata.category(ch) not in ("Cf", "Cs")
        )
        # Belt and braces: anything that still cannot round-trip UTF-8 is
        # replaced rather than returned, so the contract "safe to deliver
        # anywhere" holds literally.
        text = text.encode("utf-8", "replace").decode("utf-8", "replace")
    text = " ".join(text.split())
    if limit > 0 and len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def _plan_ref(payload: Mapping[str, Any]) -> str:
    """``<project> r<revision>`` for an operator, both values sanitized."""
    project = safe_display_value(payload.get("project_id"), limit=IDENT_LIMIT)
    revision = safe_display_value(payload.get("revision"), limit=16)
    if not project:
        project = "?"
    return f"{project} r{revision}" if revision else project


def render_gate_event(
    kind: str,
    payload: Optional[Mapping[str, Any]],
    *,
    task_id: str,
    board_slug: str = "",
    assignee: str = "",
    translate=None,
) -> Optional[str]:
    """One line describing a gate event, safe to deliver anywhere.

    Returns ``None`` for a kind this module does not own, so a caller can fall
    through to its own rendering. ``translate`` defaults to Hermes' ``t()``;
    it is injectable so a surface with its own catalog can supply one.
    """
    if kind not in PLAN_GATE_NOTIFY_KINDS:
        return None
    if translate is None:
        from agent.i18n import t as translate

    data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}

    if kind == "plan_awaiting_approval":
        detail = translate(
            "gateway.kanban.gate.plan_awaiting_approval",
            plan=_plan_ref(data),
        )
    elif kind == "plan_approved":
        detail = translate(
            "gateway.kanban.gate.plan_approved",
            plan=_plan_ref(data),
            operator=safe_display_value(data.get("operator")) or "an operator",
            landing=safe_display_value(data.get("landing_status")) or "todo",
        )
    elif kind == "plan_rejected":
        detail = translate(
            "gateway.kanban.gate.plan_rejected",
            plan=_plan_ref(data),
            operator=safe_display_value(data.get("operator")) or "an operator",
        )
    else:
        detail = translate(
            "gateway.kanban.gate.gate_release_refused",
            gate=safe_display_value(data.get("gate_state")) or "?",
            via=safe_display_value(data.get("via")) or "?",
        )

    reason = safe_display_value(data.get("reason"), limit=REASON_LIMIT)
    suffix = f": {reason}" if reason else ""
    board_tag = f"[{safe_display_value(board_slug)}] " if board_slug else ""
    who = safe_display_value(assignee)
    tag = f"@{who} " if who else ""
    ident = safe_display_value(task_id) or "?"
    return f"{GATE_EMOJI[kind]} {board_tag}{tag}Kanban {ident} — {detail}{suffix}"
