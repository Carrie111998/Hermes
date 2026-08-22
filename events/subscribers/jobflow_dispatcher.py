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

Known coverage gap: ``RESEARCH_REQUEST`` and ``QUESTION_ANSWER`` are not in the
mailbox watcher's ``MIRRORED_MESSAGE_TYPES``, so no event is ever emitted for
either and both can only be activated by reconciliation. Widening that set would
also change notification delivery, which is out of scope here.

Only the first of those is a latent defect. ``RESEARCH_REQUEST`` is
machine-produced, so real work starts silently missing the event path the moment
research volume resumes. ``QUESTION_ANSWER`` is a *human reply* — the applier
emits ``BLOCKED_QUESTION`` when a form field defeats it and waits for a person to
answer — so having no automated producer is its design, not an omission, and its
consumer is live (``profiles/applier/workspace/tmp_ready_sweep_cron.py`` in the
hermes repo re-runs the dry-run with the answer attached). A grep of this repo
alone makes it look like dead code; it is not. Do not delete that route on the
grounds that nothing emits it.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional

from events.schema import Event, EventType
from events.subscribers.base import BaseSubscriber
from jobflow_dispatch.activate import resolve_job_id_for_activity
from jobflow_dispatch.contracts import message_key as canonical_key, route_mailbox
from jobflow_dispatch.store import is_available
from jobflow_dispatch.quarantine_control import default_control_store

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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if bus is not None:
            super().__init__(bus)
        else:  # unit tests exercise handle() without a live bus
            self.bus = None
        self.store = store
        self._resolve_job_id = resolve_job_id
        self._mode = resolve_mode(mode)
        self._clock = clock
        self._sleep = sleep
        if waker is None:
            from cron.wake_channel import request_wake

            waker = request_wake
        self._waker = waker

    def startup(self) -> None:
        """Replay committed claim-to-wake handoffs before event polling starts."""
        if self._mode != MODE_ON:
            return
        try:
            self._recover_wake_outbox()
        except Exception:
            # Keep startup live so the regular idle poll can retry the durable
            # outbox even when the wake store is transiently unavailable.
            logger.exception("jobflow dispatch outbox startup recovery failed")

    def poll(self) -> int:
        """Retry durable handoffs on every poll, including completely idle polls."""
        if self._mode == MODE_ON:
            try:
                self._recover_wake_outbox()
            except Exception:
                logger.exception("jobflow dispatch outbox idle recovery failed")
        return super().poll()

    def handle(self, event: Event) -> None:
        if self._mode == MODE_OFF:
            return
        try:
            if self._mode == MODE_ON:
                self._recover_wake_outbox()
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

            # This exact retained section spans the first durable claim through
            # the durable wake handoff (or claim release), so a barrier cannot
            # observe and fence the system in the middle of claim-through-wake.
            with default_control_store().dispatch_section(
                boundary="jobflow-dispatcher"
            ):
                # Resolve before claiming so an unresolvable mapping never creates
                # ledger state. The retained dispatch admission prevents a fence from
                # landing between this resolve and the atomic claim+outbox commit.
                try:
                    job_id = self._resolve_job_id(activity_id)
                except Exception:
                    logger.exception("dispatch: resolving %s failed", activity_id)
                    continue
                if not job_id:
                    continue

                outbox = self.store.claim_for_wake(
                    key,
                    activity_id,
                    job_id=job_id,
                    caller=self.subscriber_id,
                    reason="mailbox_message",
                    now=now,
                    correlation_id=correlation_id,
                )
                if outbox is None:
                    continue  # already claimed or completed — at-least-once absorbed

                try:
                    self._deliver_outbox(outbox)
                except Exception:
                    logger.exception("dispatch: durable wake for %s failed", job_id)
                    # Keep both the claim and its durable outbox. A later event or a
                    # restarted subscriber replays it without waiting for lease expiry.
                    continue

    def _deliver_outbox(self, outbox: Any) -> None:
        woke = self._waker(
            outbox.job_id,
            caller=outbox.caller,
            reason=outbox.reason,
        )
        # False means this exact job already has a durable wake. That existing
        # wake satisfies the handoff; keeping the per-message outbox would replay
        # forever while the queue intentionally collapses duplicates by job ID.
        if woke is False:
            logger.info(
                "dispatch: %s (%s) already queued — collapsing durable outbox",
                outbox.job_id,
                outbox.activity_id,
            )
        if not self.store.ack_wake_outbox(outbox):
            raise RuntimeError("activation wake outbox changed before acknowledgement")

    def _recover_wake_outbox(self) -> None:
        pending = getattr(self.store, "pending_wake_outbox", None)
        if pending is None:
            return
        with default_control_store().dispatch_section(
            boundary="jobflow-dispatcher-outbox-recovery"
        ):
            for outbox in pending():
                self._deliver_outbox(outbox)

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
