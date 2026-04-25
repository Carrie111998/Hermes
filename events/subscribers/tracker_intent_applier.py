"""Gateway subscriber wrapping IntentApplier (Task 7).

Adapts the filesystem-driven ``IntentApplier`` (Task 6) to the gateway's
subscriber lifecycle. Polls the tracker mailbox inbox at a short cadence
(default 1 s) and applies new intent files. The applier itself is the
unit of business logic; this file only adapts subscriber lifecycle
(poll/handle/startup/shutdown) to it.

Adaptation choice — Option A (override ``poll()``):
    ``BaseSubscriber.poll()`` is a regular method (not ``@final``), and
    the gateway poll loop in ``events/gateway_integration.py`` discards
    its ``int`` return value. This subscriber is filesystem-driven, not
    event-bus-driven, so we replace ``poll()`` entirely rather than
    consuming-and-ignoring events from the bus (which would still cost
    a SQL query per tick and pollute the per-subscriber cursor).

    The base class circuit breaker only fires inside the bus-event loop,
    so overriding ``poll()`` bypasses it — but ``IntentApplier`` has its
    own circuit breaker (Task 4) that wraps JobOps writes, so coverage is
    preserved at a more useful layer.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict

from events.bus import EventBus
from events.schema import Event
from events.subscribers.base import BaseSubscriber

from intent_applier import IdempotencyTracker, IntentApplier, JobOpsClient
from pipeline_state import PipelineManager

logger = logging.getLogger(__name__)


def _hermes_root() -> Path:
    return Path(os.environ.get("HERMES_ROOT", str(Path.home() / ".hermes")))


def _tracker_mailbox(root: Path) -> Dict[str, Path]:
    base = root / "mailbox" / "tracker"
    return {
        "inbox": base / "inbox",
        "processed": base / "processed",
        "partial": base / "partial",
        "dead_letter": base / "dead-letter",
    }


class TrackerIntentApplierSubscriber(BaseSubscriber):
    """Filesystem-driven subscriber that drains the tracker intent inbox.

    See module docstring for why ``poll()`` is fully overridden rather
    than chained through ``BaseSubscriber.poll()``.
    """

    subscriber_id = "tracker-intent-applier"
    poll_interval_seconds = 1
    # Filesystem-driven, not event-bus-driven. ``poll()`` is overridden
    # below, so these inherited filters are never consulted — leaving
    # them at the base-class default keeps typing clean (the base types
    # ``event_types`` as ``Optional[List[EventType]]``, not ``list[str]``).
    event_types = None
    min_priority = None

    def __init__(self, bus: EventBus):
        super().__init__(bus)
        root = _hermes_root()
        self._mailbox = _tracker_mailbox(root)
        self._state_db = root / "events" / "applier_state.db"
        self._jobops_url = os.environ.get(
            "HERMES_JOBOPS_URL", "http://127.0.0.1:4100"
        )
        self._applier: IntentApplier | None = None

    def startup(self) -> None:
        """Build the applier with rehydrated idempotency state."""
        idempotency = IdempotencyTracker(self._state_db)
        # Replay processed/ into the idempotency DB so a fresh DB after a
        # gateway restart doesn't re-apply intents we already handled.
        idempotency.rehydrate_from_processed(self._mailbox["processed"])

        # ``resume_full`` is optional — graphs.jobflow may not be present
        # in every deployment (e.g. minimal CI installs). Failing soft
        # here keeps the subscriber registerable in those environments.
        try:
            from graphs.jobflow import resume_full as _resume_full
        except ImportError:
            _resume_full = None
            logger.info(
                "tracker-intent-applier: graphs.jobflow not available; "
                "thread-resume disabled"
            )

        self._applier = IntentApplier(
            inbox_dir=self._mailbox["inbox"],
            processed_dir=self._mailbox["processed"],
            partial_dir=self._mailbox["partial"],
            dead_letter_dir=self._mailbox["dead_letter"],
            pipeline_manager=PipelineManager(),
            jobops_client=JobOpsClient(base_url=self._jobops_url),
            idempotency=idempotency,
            resume_full=_resume_full,
        )
        logger.info(
            "tracker-intent-applier: ready (inbox=%s, jobops=%s)",
            self._mailbox["inbox"],
            self._jobops_url,
        )

    def handle(self, event: Event) -> None:
        """No-op: this subscriber is filesystem-driven, not event-bus-driven.

        ``BaseSubscriber.handle`` is ``@abstractmethod`` so we must define
        it, but ``poll()`` is overridden below and never invokes it.
        """
        return None

    def poll(self) -> int:  # override BaseSubscriber.poll
        """Drain the inbox once. Returns count of files processed.

        The gateway poll loop discards this return value, but we honour
        the base-class ``int`` contract so the subscriber remains a
        drop-in replacement for any future caller that does inspect it.
        """
        if self._applier is None:
            # startup() not yet called — defensive no-op.
            return 0

        outcomes = self._applier.scan_inbox()
        if outcomes:
            applied = sum(1 for v in outcomes.values() if v == "applied")
            partial = sum(1 for v in outcomes.values() if v == "partial")
            dead = sum(1 for v in outcomes.values() if v == "dead_lettered")
            skipped = sum(
                1 for v in outcomes.values() if v == "skipped_idempotent"
            )
            logger.info(
                "tracker-intent-applier: tick processed=%d "
                "(applied=%d partial=%d dead=%d skipped=%d)",
                len(outcomes), applied, partial, dead, skipped,
            )
        return len(outcomes)
