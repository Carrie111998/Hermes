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
operator sets ``HERMES_JOBFLOW_EVENT_DISPATCH``. ``shadow`` is read-only on the
ledger — it answers "would this have woken?" with the reconciler's own
``is_available`` predicate and writes nothing, so a seven-day observation window
cannot leave residue that would suppress the first real run.

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
from jobflow_dispatch.contracts import message_key as canonical_key, route_mailbox
from jobflow_dispatch.store import is_available

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

    #: Grep marker for a claim that committed but could not be handed back.
    #: Distinct from every other dispatcher failure because it is the only one
    #: that strands real work: the message is invisible to BOTH activation paths
    #: until its lease lapses. Nothing else reports it, so this string is the
    #: only trace it leaves.
    ORPHAN_MARKER = "ORPHAN_CLAIM"

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

        raw_key = payload.get("file") or getattr(event, "correlation_id", None)
        try:
            # MailboxWatcher yields OS-native separators; the reconciler builds
            # its own. Both must land on the same ledger row.
            key = canonical_key(raw_key)
        except ValueError:
            logger.warning("dispatch: message with no usable key — dropped")
            return

        correlation_id = getattr(event, "correlation_id", None)
        now = self._clock()

        for activity_id in targets:
            if self._mode == MODE_SHADOW:
                self._observe(key, activity_id, now)
                continue

            if not self.store.claim(
                key, activity_id, now=now, correlation_id=correlation_id
            ):
                continue  # already claimed or completed — at-least-once absorbed

            # From here on, any path that does NOT deliver a wake must hand the
            # claim back. A committed claim with no wake is the worst state:
            # the reconciler skips it (it looks claimed) and no worker was ever
            # woken, so the work stalls until the lease expires.
            try:
                job_id = self._resolve_job_id(activity_id)
            except Exception:
                logger.exception("dispatch: resolving %s failed", activity_id)
                self._release(key, activity_id)
                continue
            if not job_id:
                self._release(key, activity_id)
                continue

            if self._waker(job_id, caller=self.subscriber_id, reason="mailbox_message") is False:
                logger.warning(
                    "dispatch: wake channel refused %s (%s) — releasing claim",
                    job_id, activity_id,
                )
                self._release(key, activity_id)

    def _observe(self, key: str, activity_id: str, now: float) -> None:
        """Record what ``on`` would have done, without touching the ledger.

        Shadow used to claim and then hand the claim straight back. That left a
        window — the claim is committed to disk while ``_resolve_job_id`` parses
        a 130 KB jobs.json — in which a kill, or a release that failed (and
        ``_release`` swallows everything), stranded the message behind a claim
        nothing would ever wake. Shadow wakes nobody, so every such orphan was
        guaranteed lost work, hidden from the reconciler for a full lease: two
        hours since the lease bump, up from fifteen minutes.

        Reading the reconciler's own predicate instead costs a race that does
        not matter to an observer, and removes the window entirely.
        """
        if not is_available(self.store, key, activity_id, now):
            return  # the real path already holds this; shadow would not wake it
        try:
            job_id = self._resolve_job_id(activity_id)
        except Exception:
            logger.exception("dispatch: resolving %s failed", activity_id)
            return
        if not job_id:
            return
        logger.info(
            "dispatch[shadow]: would wake %s (%s) for %s", job_id, activity_id, key
        )

    def _release(self, key: str, activity_id: str) -> None:
        try:
            self.store.release(key, activity_id)
        except Exception:
            # Swallowed to keep the subscriber loop alive — but this is the ONE
            # dispatcher fault that costs work rather than latency. Everywhere
            # else a dropped wake just waits for the reconciler; here the claim
            # committed, so the reconciler SKIPS the message and nothing woke
            # it. Logged with a stable marker so the stranded work can be found
            # afterwards, since nothing else will report it.
            logger.exception(
                "dispatch: %s %s (%s) — claim committed but could not be "
                "released; invisible to the reconciler for up to %ss",
                self.ORPHAN_MARKER,
                key,
                activity_id,
                getattr(self.store, "lease_seconds", "?"),
            )
