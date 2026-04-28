"""ScribeVoiceTuning — pattern-matches free-form replies in Scribe Daily and
adjusts brevity_target_chars + suppressed_event_types in voice_tuning.json.

Per design spec: docs/superpowers/specs/2026-04-28-scribe-action-telemetry-and-voice-tuning-design.md

scribe_digest.py reads the state file on each fire and modulates rendering.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from events.bus import EventBus
from events.schema import Event, EventType
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

STATE_PATH = Path(os.path.expanduser(
    "~/.hermes/profiles/scribe/workspace/voice_tuning.json"
))
TOPICS_PATH = Path(os.path.expanduser("~/.hermes/telegram/topics.json"))
SCHEMA_VERSION = 1
DEFAULT_BREVITY = 4000
BREVITY_MIN = 800
BREVITY_MAX = 8000
MAX_SUPPRESSED = 20
MAX_FEEDBACK_LOG = 100


def _load_state() -> dict:
    fresh = {
        "schema_version": SCHEMA_VERSION,
        "brevity_target_chars": DEFAULT_BREVITY,
        "suppressed_event_types": [],
        "last_updated": None,
        "feedback_log": [],
    }
    if not STATE_PATH.exists():
        return fresh
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if data.get("schema_version") != SCHEMA_VERSION:
            logger.warning("voice_tuning: schema mismatch, resetting")
            return fresh
        for k, v in fresh.items():
            data.setdefault(k, v)
        return data
    except Exception as exc:
        logger.warning("voice_tuning: load failed (%s), resetting", exc)
        return fresh


def _save_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _scribe_daily_thread_id() -> Optional[str]:
    """Read fresh from topics.json each call so taxonomy changes take immediate effect."""
    try:
        cfg = json.loads(TOPICS_PATH.read_text(encoding="utf-8"))
        thread = cfg.get("topics", {}).get("scribe_daily", {}).get("thread_id", "")
        return str(thread) if thread else None
    except Exception:
        return None


# Pattern-matching: ordered list of (regex, intent_label, apply_fn).
# apply_fn signature: (state_dict, regex_match) -> dict of state-keys to update.
# apply_fn=None means log-only (no state change).
_PATTERNS = [
    (
        re.compile(r"\btl;?dr\b|too long|shorter", re.IGNORECASE),
        "shrink",
        lambda s, m: {"brevity_target_chars": max(BREVITY_MIN, int(s["brevity_target_chars"] * 0.8))},
    ),
    (
        re.compile(r"\bexpand|more detail|longer\b", re.IGNORECASE),
        "grow",
        lambda s, m: {"brevity_target_chars": min(BREVITY_MAX, int(s["brevity_target_chars"] * 1.25))},
    ),
    (
        re.compile(r"\b(?:ignore|skip|silence)\s+(\w+)", re.IGNORECASE),
        "suppress",
        lambda s, m: {
            "suppressed_event_types": list(set(s["suppressed_event_types"] + [m.group(1)]))[:MAX_SUPPRESSED]
        },
    ),
    (
        re.compile(r"\b(?:unignore|unsuppress|restore)\s+(\w+)", re.IGNORECASE),
        "unsuppress",
        lambda s, m: {
            "suppressed_event_types": [t for t in s["suppressed_event_types"] if t != m.group(1)]
        },
    ),
    (
        re.compile(r"^(?:👍|🔥|thanks|good|nice)", re.IGNORECASE),
        "positive_signal",
        None,
    ),
]


class ScribeVoiceTuning(BaseSubscriber):
    subscriber_id = "scribe-voice-tuning"
    poll_interval_seconds = 60
    event_types = [EventType.USER_INBOUND_MESSAGE]

    def handle(self, event: Event) -> None:
        try:
            payload = event.payload or {}
            scribe_thread_id = _scribe_daily_thread_id()
            if not scribe_thread_id:
                return
            event_thread_id = payload.get("thread_id")
            if str(event_thread_id) != scribe_thread_id:
                return

            text = payload.get("text", "") or ""
            if not text.strip():
                return

            self._apply_patterns(text, event)
        except Exception as exc:
            logger.warning("voice-tuning: handle failed for %s: %s", event.event_id, exc)

    def _apply_patterns(self, text: str, event: Event) -> None:
        data = _load_state()
        applied_changes = {}
        intent = "unknown_feedback"

        for pattern, label, apply_fn in _PATTERNS:
            m = pattern.search(text)
            if not m:
                continue
            intent = label
            if apply_fn is not None:
                try:
                    changes = apply_fn(data, m)
                    for k, v in changes.items():
                        applied_changes[k] = f"{data.get(k)!r} → {v!r}"
                        data[k] = v
                except Exception as exc:
                    logger.warning("voice-tuning: pattern apply failed: %s", exc)
            break  # first match wins

        log_entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "text": text[:500],
            "parsed_intent": intent,
            "applied_change": applied_changes or None,
        }
        data["feedback_log"].append(log_entry)
        if len(data["feedback_log"]) > MAX_FEEDBACK_LOG:
            data["feedback_log"] = data["feedback_log"][-MAX_FEEDBACK_LOG:]
        data["last_updated"] = datetime.now(timezone.utc).isoformat()

        _save_state(data)
