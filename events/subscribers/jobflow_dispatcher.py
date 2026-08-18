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

    #: Grep marker for a claim that committed but could not be handed back.
    #: Distinct from every other dispatcher failure because it is the only one
    #: that strands real work: the message is invisible to BOTH activation paths
    #: until its lease lapses. Nothing else reports it, so this string is the
    #: only trace it leaves.
    ORPHAN_MARKER = "ORPHAN_CLAIM"

    #: Total ``store.release`` attempts before conceding an orphan.
    #:
    #: Deliberately small, because the retry is NOT the expensive part. The
    #: plausible fault is SQLITE_BUSY, and ``ActivationStore`` already sets
    #: ``PRAGMA busy_timeout=5000`` — so a contention failure has ALREADY blocked
    #: up to five seconds inside SQLite before it reaches us. Three attempts is
    #: therefore ~15s of contention coverage, against a ledger whose every write
    #: is one indexed row inside ``BEGIN IMMEDIATE`` (sub-millisecond). Anything
    #: that survives that is not transient contention, and a fourth attempt buys
    #: another five seconds of stalled event loop for no realistic gain.
    #:
    #: This is why k is 3 here and 10 in the WinError-5 precedent: there an
    #: attempt was a cheap file rename, so a high k cost nothing. Here each
    #: attempt can cost a full busy_timeout.
    RELEASE_ATTEMPTS = 3

    #: Backoff before each retry: 0.1s then 0.2s, 0.3s of added delay worst case.
    #:
    #: Kept far below the busy_timeout on purpose. It exists for the failures
    #: that come back IMMEDIATELY rather than after the timeout — notably a WAL
    #: snapshot conflict on the ``BEGIN IMMEDIATE`` upgrade, which SQLite does not
    #: retry for us — where a bare re-issue would just lose the same race again.
    #: Where the timeout did fire, it has already done all the waiting needed.
    RELEASE_BACKOFF_SECONDS = (0.1, 0.2)

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
                # Overwhelmingly the benign case: the job is ALREADY queued, so a
                # burst of N messages collapses to one wake and the other N-1 land
                # here. That is the mechanism that makes event dispatch cheaper
                # than polling, not a fault — the first live burst logged 14 of
                # these for one run. Logged at INFO so a real problem stays
                # visible: request_wake's other False (channel full) already emits
                # its own WARNING from cron/wake_channel.py with more detail, so
                # demoting here loses no signal.
                logger.info(
                    "dispatch: %s (%s) already queued — collapsing, releasing claim",
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
        """Hand the claim back, retrying a bounded number of times.

        A failed release is the one dispatcher fault that costs WORK rather than
        latency, so it is worth spending a little of the subscriber loop's time
        to prevent one instead of merely reporting it. The plausible cause —
        SQLITE_BUSY under contention — is exactly the kind a short backoff
        clears.

        ``store.release`` DELETEs ``WHERE ... AND state != 'completed'``, so it
        is idempotent: a retry after a partial or ambiguous failure cannot
        resurrect completed work or corrupt a row a later claim installed.
        """
        for attempt in range(1, self.RELEASE_ATTEMPTS + 1):
            try:
                self.store.release(key, activity_id)
            except Exception:
                if attempt < self.RELEASE_ATTEMPTS:
                    logger.warning(
                        "dispatch: release of %s (%s) failed on attempt %d/%d — retrying",
                        key, activity_id, attempt, self.RELEASE_ATTEMPTS,
                        exc_info=True,
                    )
                    # Clamped, not indexed blind: raising RELEASE_ATTEMPTS
                    # without extending the backoff would otherwise raise
                    # IndexError from inside a fault handler, which escapes
                    # _release and skips the ORPHAN_MARKER log — losing exactly
                    # the trace this method exists to leave. A test asserts the
                    # two stay in step; this makes the failure mode degrade
                    # rather than detonate.
                    backoff = self.RELEASE_BACKOFF_SECONDS[
                        min(attempt - 1, len(self.RELEASE_BACKOFF_SECONDS) - 1)
                    ]
                    self._sleep(backoff)
                    continue
                # Still swallowed, and swallowed LAST: a release fault must
                # never stall the subscriber loop, retries included. But the
                # claim committed, so the reconciler SKIPS the message and
                # nothing woke it. Logged with a stable marker so the stranded
                # work can be found afterwards, since nothing else reports it.
                logger.exception(
                    "dispatch: %s %s (%s) — claim committed but could not be "
                    "released after %d attempts; invisible to the reconciler "
                    "for up to %ss",
                    self.ORPHAN_MARKER,
                    key,
                    activity_id,
                    self.RELEASE_ATTEMPTS,
                    getattr(self.store, "lease_seconds", "?"),
                )
                return
            else:
                if attempt > 1:
                    logger.warning(
                        "dispatch: release of %s (%s) succeeded on attempt %d — "
                        "transient ledger contention, no orphan",
                        key, activity_id, attempt,
                    )
                return
