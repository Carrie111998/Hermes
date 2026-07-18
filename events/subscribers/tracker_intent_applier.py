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

from intent_applier import (
    IdempotencyTracker,
    IntentApplier,
    JobOpsClient,
    build_default_reader,
)
from pipeline_state import PipelineManager

logger = logging.getLogger(__name__)


_TRUTHY = {"1", "true", "yes", "on"}


def _redrive_enabled_from_env() -> bool:
    """The HARD-GATE feature flag. Default OFF until :4100 runs 8d7b5f5's dist.

    Auto-re-drive must stay disabled until jobflow-api :4100 is restarted with
    the idempotent no-op guard + lock/statement timeouts LIVE, or re-driving an
    already-applied intent could double-write / re-fire notifications.
    """
    return os.environ.get(
        "TRACKER_APPLIER_REDRIVE_ENABLED", "0"
    ).strip().lower() in _TRUTHY


def _redrive_config_from_env() -> dict:
    """Backoff/attempt tuning for IntentApplier.redrive_partials(), env-overridable."""
    def _f(name: str, default: float) -> float:
        try:
            return float(os.environ[name])
        except (KeyError, ValueError):
            return default

    def _i(name: str, default: int) -> int:
        try:
            return int(os.environ[name])
        except (KeyError, ValueError):
            return default

    return {
        "redrive_base_backoff": _f("TRACKER_APPLIER_REDRIVE_BASE_SECONDS", 120.0),
        "redrive_multiplier": _f("TRACKER_APPLIER_REDRIVE_MULTIPLIER", 2.0),
        "redrive_max_backoff": _f("TRACKER_APPLIER_REDRIVE_MAX_BACKOFF_SECONDS", 1800.0),
        "max_redrive_attempts": _i("TRACKER_APPLIER_REDRIVE_MAX_ATTEMPTS", 5),
        # Fix B: 0 => never give up (slow-lane retry at max_backoff forever).
        # Set >0 only to restore a terminal "capped" after N attempts.
        "redrive_give_up_attempts": _i("TRACKER_APPLIER_REDRIVE_GIVE_UP_ATTEMPTS", 0),
    }


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


def tracker_partial_dir() -> Path:
    """The tracker mailbox partial/ dir — read-only counted by PartialBacklogMonitor."""
    return _tracker_mailbox(_hermes_root())["partial"]


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
        self._redrive_enabled = _redrive_enabled_from_env()
        self._redrive_config = _redrive_config_from_env()

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

        # Fix A: native-Postgres pre-flight reader (None on minimal installs
        # without a psycopg driver — pre-flight simply stays off).
        job_state_reader = build_default_reader()

        self._applier = IntentApplier(
            inbox_dir=self._mailbox["inbox"],
            processed_dir=self._mailbox["processed"],
            partial_dir=self._mailbox["partial"],
            dead_letter_dir=self._mailbox["dead_letter"],
            pipeline_manager=PipelineManager(),
            jobops_client=JobOpsClient(base_url=self._jobops_url),
            idempotency=idempotency,
            resume_full=_resume_full,
            job_state_reader=job_state_reader,
            **self._redrive_config,
        )
        logger.info(
            "tracker-intent-applier: ready (inbox=%s, jobops=%s, redrive_enabled=%s, "
            "preflight=%s, give_up_attempts=%s)",
            self._mailbox["inbox"],
            self._jobops_url,
            self._redrive_enabled,
            job_state_reader is not None,
            self._redrive_config.get("redrive_give_up_attempts"),
        )

    def handle(self, event: Event) -> None:
        """No-op: this subscriber is filesystem-driven, not event-bus-driven.

        ``BaseSubscriber.handle`` is ``@abstractmethod`` so we must define
        it, but ``poll()`` is overridden below and never invokes it.
        """
        return None

    def lag_report(self) -> int:
        # Filesystem-driven subscriber; doesn't consume bus events. Always 0.
        return 0

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
            satisfied = sum(1 for v in outcomes.values() if v == "satisfied")
            logger.info(
                "tracker-intent-applier: tick processed=%d "
                "(applied=%d partial=%d dead=%d skipped=%d satisfied=%d)",
                len(outcomes), applied, partial, dead, skipped, satisfied,
            )
        return len(outcomes)

    def redrive_partials(self) -> int:
        """Flag-gated wrapper: re-drive eligible partials iff the feature is enabled.

        The HARD GATE. Returns the number of partials re-driven this sweep (0 when
        disabled or nothing eligible). IntentApplier.redrive_partials() itself is
        pure/always-acts; THIS method is the feature flag — it must stay OFF until
        :4100 runs 8d7b5f5's dist (idempotent no-op guard live).
        """
        if not self._redrive_enabled or self._applier is None:
            return 0
        results = self._applier.redrive_partials()
        redriven = sum(1 for v in results.values() if v == "redriven")
        if redriven:
            logger.info("tracker-intent-applier: re-drove %d partial(s)", redriven)
        return redriven
