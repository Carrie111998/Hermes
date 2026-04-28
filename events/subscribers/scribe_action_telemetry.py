"""ScribeActionTelemetry — correlates digest_generated events with subsequent
user_inbound_message and action events in a 4-hour window.

Per design spec: docs/superpowers/specs/2026-04-28-scribe-action-telemetry-and-voice-tuning-design.md

State persisted at ~/.hermes/profiles/scribe/workspace/action_telemetry.json.
Critic reads completed_digests during weekly retros.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from events.bus import EventBus
from events.schema import Event, EventType
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

STATE_PATH = Path(os.path.expanduser(
    "~/.hermes/profiles/scribe/workspace/action_telemetry.json"
))
SCHEMA_VERSION = 1
WINDOW_HOURS_DEFAULT = 4
COMPLETED_RETENTION_DAYS = 30
ACTION_EVENT_TYPES = {
    "application_submitted",
    "stage_transition",
    "tailor_completed",
}


def _window_hours() -> float:
    try:
        return float(os.environ.get("SCRIBE_ACTION_WINDOW_HOURS", WINDOW_HOURS_DEFAULT))
    except Exception:
        return WINDOW_HOURS_DEFAULT


def _load_state() -> dict:
    fresh = {
        "schema_version": SCHEMA_VERSION,
        "active_digests": [],
        "completed_digests": [],
    }
    if not STATE_PATH.exists():
        return fresh
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if data.get("schema_version") != SCHEMA_VERSION:
            logger.warning("action_telemetry: schema mismatch, resetting")
            return fresh
        for k, v in fresh.items():
            data.setdefault(k, v)
        return data
    except Exception as exc:
        logger.warning("action_telemetry: load failed (%s), resetting", exc)
        return fresh


def _save_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


class ScribeActionTelemetry(BaseSubscriber):
    subscriber_id = "scribe-action-telemetry"
    poll_interval_seconds = 60
    event_types = [
        EventType.DIGEST_GENERATED,
        EventType.USER_INBOUND_MESSAGE,
        EventType.APPLICATION_SUBMITTED,
        EventType.STAGE_TRANSITION,
        EventType.TAILOR_COMPLETED,
    ]

    def handle(self, event: Event) -> None:
        try:
            data = _load_state()
            et = event.event_type.type_string
            if et == "digest_generated":
                self._open_digest_window(data, event)
            elif et == "user_inbound_message":
                self._record_reply(data, event)
            elif et in ACTION_EVENT_TYPES:
                self._record_action(data, event)
            self._expire_old_windows(data)
            _save_state(data)
        except Exception as exc:
            logger.warning("action-telemetry: handle failed for %s: %s", event.event_id, exc)

    def _open_digest_window(self, data: dict, event: Event) -> None:
        payload = event.payload or {}
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=_window_hours())
        data["active_digests"].append({
            "digest_id": event.event_id,
            "mode": payload.get("scribe_digest_type", "unknown"),
            "fired_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "items_surfaced": payload.get("items_surfaced", []),
            "items_acted_on": [],
            "replies": [],
        })

    def _record_reply(self, data: dict, event: Event) -> None:
        payload = event.payload or {}
        text = (payload.get("text") or "").lower()
        ts = datetime.now(timezone.utc).isoformat()
        for entry in data["active_digests"]:
            for item in entry.get("items_surfaced", []):
                company = (item.get("company") or "").lower()
                title = (item.get("title") or "").lower()
                if (company and company in text) or (title and title in text):
                    entry["replies"].append({"ts": ts, "text": payload.get("text", "")})
                    break  # one reply credit per digest, not per item

    def _record_action(self, data: dict, event: Event) -> None:
        payload = event.payload or {}
        job_key = payload.get("job_key")
        if not job_key:
            return
        action_ts = payload.get("ts") or datetime.now(timezone.utc).isoformat()
        for entry in data["active_digests"]:
            for item in entry.get("items_surfaced", []):
                if item.get("job_key") == job_key:
                    entry["items_acted_on"].append({
                        "job_key": job_key,
                        "action_event_type": event.event_type.type_string,
                        "action_ts": action_ts,
                    })

    def _expire_old_windows(self, data: dict) -> None:
        now = datetime.now(timezone.utc)
        still_active = []
        for entry in data["active_digests"]:
            try:
                exp = datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
            except Exception:
                exp = now  # malformed → expire immediately
            if exp <= now:
                data["completed_digests"].append(entry)
            else:
                still_active.append(entry)
        data["active_digests"] = still_active

        # Prune very old completed entries
        cutoff = now - timedelta(days=COMPLETED_RETENTION_DAYS)
        kept = []
        for e in data["completed_digests"]:
            try:
                fired = datetime.fromisoformat(e.get("fired_at", "").replace("Z", "+00:00"))
                if fired > cutoff:
                    kept.append(e)
            except Exception:
                pass  # drop malformed entries
        data["completed_digests"] = kept
