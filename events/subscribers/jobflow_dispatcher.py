"""Activate JobFlow workers from mailbox events instead of polling.

A worker that polls every 15 minutes pays a model call to discover an empty
inbox. This subscriber turns an actionable mailbox message into an in-memory
wake so the worker runs when work exists and not otherwise.

Three properties, each closing a specific hazard:

* **No cron-store write.** Activation goes through ``cron.wake_channel``, not
  ``jobs.trigger_job``. The latter rewrites ``jobs.json`` on every call and
  sets ``enabled: True`` — it would contend with the scheduler's own rewrite
  and silently revive a worker an operator disabled.
* **Exactly once per logical work item.** The activation ledger claims
  ``(message_key, activity_id)``. EventBus delivery is at-least-once, so the
  same message is seen repeatedly; without the claim each redelivery would be
  another model call.
* **Fail closed.** An unroutable message, an unresolvable activity, a missing
  key, or any unexpected error results in no activation. The deterministic
  reconciler is the safety net, so dropping a wake costs latency, never work.

Default mode is ``off``: registering the subscriber changes nothing until an
operator sets ``HERMES_JOBFLOW_EVENT_DISPATCH``.

Known coverage gap: ``RESEARCH_REQUEST`` is not in the mailbox watcher's
``MIRRORED_MESSAGE_TYPES``, so no event is ever emitted for it and the
researcher can only be activated by reconciliation. Widening that set would
also change notification delivery, which is out of scope here.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional

from events.schema import Event, EventType
from events.subscribers.base import BaseSubscriber
from jobflow_dispatch.contracts import route_mailbox

logger = logging.getLogger(__name__)

MODE_ENV = "HERMES_JOBFLOW_EVENT_DISPATCH"
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"
VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)


def resolve_mode(raw: Optional[str] = None) -> str:
    """Read the dispatch mode, defaulting to off for anything unrecognised."""
    value = (raw if raw is not None else os.getenv(MODE_ENV, "")).strip().lower()
    return value if value in VALID_MODES else MODE_OFF


def resolve_job_id_for_activity(activity_id: str) -> Optional[str]:
    """Map a policy activity ID to exactly one enabled cron job ID.

    Fails closed on zero or multiple matches: activating the wrong worker is
    worse than not activating one, because the reconciler will catch the miss.
    """
    from activity_policy.registry import ActivityRegistry
    from cron.jobs import load_jobs

    registry = ActivityRegistry.load_default()
    policy = registry.policies.get(activity_id)
    if policy is None or not policy.aliases:
        logger.warning("dispatch: no policy/alias for activity %s", activity_id)
        return None

    names = {alias for alias in policy.aliases}
    matches = [
        job for job in load_jobs()
        if job.get("name") in names and job.get("enabled")
    ]
    if len(matches) != 1:
        logger.warning(
            "dispatch: activity %s resolved %d enabled jobs — refusing to guess",
            activity_id, len(matches),
        )
        return None
    return matches[0].get("id")


class JobFlowDispatcher(BaseSubscriber):
    subscriber_id = "jobflow-dispatcher"
    poll_interval_seconds = 5
    event_types = [EventType.MAILBOX_MESSAGE]

    def __init__(
        self,
        bus: Any,
        store: Any,
        *,
        resolve_job_id: Callable[[str], Optional[str]] = resolve_job_id_for_activity,
        waker: Optional[Callable[..., bool]] = None,
        mode: Optional[str] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if bus is not None:
            super().__init__(bus)
        else:  # unit tests exercise handle() without a live bus
            self.bus = None
        self.store = store
        self._resolve_job_id = resolve_job_id
        self._mode = resolve_mode(mode)
        self._clock = clock
        if waker is None:
            from cron.wake_channel import request_wake

            waker = request_wake
        self._waker = waker

    def handle(self, event: Event) -> None:
        if self._mode == MODE_OFF:
            return
        try:
            self._dispatch(event)
        except Exception:
            # A dispatcher fault must never stall the subscriber loop; the
            # reconciler still covers whatever this drop missed.
            logger.exception("jobflow dispatch failed for event %s", getattr(event, "event_id", "?"))

    def _dispatch(self, event: Event) -> None:
        payload = getattr(event, "payload", None) or {}
        targets = route_mailbox(payload.get("message_type"), payload.get("to"), payload)
        if not targets:
            return

        message_key = payload.get("file") or getattr(event, "correlation_id", None)
        if not isinstance(message_key, str) or not message_key.strip():
            logger.warning("dispatch: message with no usable key — dropped")
            return

        correlation_id = getattr(event, "correlation_id", None)
        now = self._clock()

        for activity_id in targets:
            if not self.store.claim(
                message_key, activity_id, now=now, correlation_id=correlation_id
            ):
                continue  # already claimed or completed — at-least-once absorbed

            try:
                job_id = self._resolve_job_id(activity_id)
            except Exception:
                logger.exception("dispatch: resolving %s failed", activity_id)
                continue
            if not job_id:
                continue

            if self._mode == MODE_SHADOW:
                logger.info(
                    "dispatch[shadow]: would wake %s (%s) for %s",
                    job_id, activity_id, message_key,
                )
                continue

            self._waker(job_id, caller=self.subscriber_id, reason="mailbox_message")
