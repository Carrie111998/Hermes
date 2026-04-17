"""MailboxTranslator — converts mailbox_message events into typed domain events.

Subscribes to mailbox_message (produced by MailboxWatcher) and emits
typed JOB_SCORED, JOB_HIGH_SCORE, APPLICATION_SUBMITTED, STAGE_TRANSITION,
etc. based on the message_type + inner payload.

This subscriber replaces the dead regex-based output parser in
CronEventEmitter that was never producing domain events.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

HIGH_SCORE_THRESHOLD = 8.75


class MailboxTranslator(BaseSubscriber):
    subscriber_id = "mailbox-translator"
    poll_interval_seconds = 5
    event_types = [EventType.MAILBOX_MESSAGE]

    def handle(self, event: Event) -> None:
        payload = event.payload or {}
        message_type = payload.get("message_type", "")
        inner = payload.get("inner_payload") or payload.get("payload") or {}
        correlation_id = event.correlation_id

        emissions = self._translate(message_type, inner)
        for et, out_payload, priority in emissions:
            try:
                self.bus.emit(
                    event_type=et,
                    source=f"mailbox:{payload.get('from', 'unknown')}",
                    payload=out_payload,
                    priority=priority,
                    correlation_id=correlation_id,
                    job_id=out_payload.get("job_key") or out_payload.get("job_id"),
                )
            except Exception:
                logger.exception("MailboxTranslator: failed to emit %s", et.type_string)

    def _translate(
        self,
        message_type: str,
        inner: Dict[str, Any],
    ) -> List[Tuple[EventType, Dict[str, Any], Optional[Priority]]]:
        """Return a list of (event_type, payload, priority_override_or_None)."""
        results: List[Tuple[EventType, Dict[str, Any], Optional[Priority]]] = []

        if message_type == "SCORE_RESULT":
            p = _score_payload(inner)
            results.append((EventType.JOB_SCORED, p, None))
            if p.get("score", 0) >= HIGH_SCORE_THRESHOLD:
                results.append((EventType.JOB_HIGH_SCORE, p, None))

        elif message_type == "SCORE_BATCH_SUMMARY":
            for job in inner.get("scored_jobs", []):
                p = _score_payload(job)
                results.append((EventType.JOB_SCORED, p, None))
                if p.get("score", 0) >= HIGH_SCORE_THRESHOLD:
                    results.append((EventType.JOB_HIGH_SCORE, p, None))

        elif message_type == "SCOUT_DISCOVERY":
            for job in inner.get("jobs", []):
                p = _job_payload(job)
                results.append((EventType.JOB_DISCOVERED, p, None))

        elif message_type == "TAILOR_COMPLETE":
            results.append((EventType.TAILOR_COMPLETED, _copy_fields(
                inner, ["company", "title", "job_key", "artifacts"]), None))

        elif message_type in ("SUBMIT_REQUEST", "DRY_RUN_COMPLETE"):
            results.append((EventType.APPLICATION_READY, _copy_fields(
                inner, ["company", "title", "job_key", "artifacts"]), None))

        elif message_type == "SUBMIT_CONFIRM":
            results.append((EventType.APPLICATION_SUBMITTED, _copy_fields(
                inner, ["company", "title", "job_key", "submission_id"]), None))

        elif message_type == "BLOCKED_QUESTION":
            results.append((EventType.APPLICATION_BLOCKED, _copy_fields(
                inner, ["company", "title", "job_key", "question"]), None))

        elif message_type == "PIPELINE_UPDATE":
            prev = inner.get("previous_stage")
            new = inner.get("new_stage")
            if new and prev and new != prev:
                results.append((EventType.STAGE_TRANSITION, _copy_fields(
                    inner, ["job_key", "previous_stage", "new_stage", "company"]), None))

        elif message_type == "FOLLOWUP_ALERT":
            results.append((EventType.FOLLOWUP_DUE, _copy_fields(
                inner, ["company", "title", "job_key", "days_since_application"]), None))

        elif message_type == "VIP_DISCOVERY":
            p = _job_payload(inner)
            p.setdefault("source", "linkedin-saved")
            results.append((EventType.JOB_VIP_DISCOVERED, p, None))

        elif message_type == "HIGH_SCORE_ALERT":
            results.append((EventType.JOB_HIGH_SCORE, _score_payload(inner), None))

        elif message_type == "ERROR":
            results.append((EventType.AGENT_ERROR, _copy_fields(
                inner, ["message", "source_agent", "traceback"]), None))

        return results


def _score_payload(d: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "score": d.get("score", 0),
        "recommendation": d.get("recommendation"),
        "company": d.get("company"),
        "title": d.get("title"),
        "dimensions": d.get("dimensions"),
        "job_key": d.get("job_key"),
    }


def _job_payload(d: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "company": d.get("company"),
        "title": d.get("title"),
        "source": d.get("source"),
        "url": d.get("url"),
        "job_key": d.get("job_key"),
    }


def _copy_fields(d: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
    return {f: d.get(f) for f in fields if d.get(f) is not None}
