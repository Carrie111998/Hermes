"""Curator producer — emits the nightly ``curator_daily`` event.

Mirrors the producer pattern used by ``cron_emitter.py``: thin wrapper
around ``EventBus.emit`` with a typed payload schema. Curator's nightly
delta-pass calls this once per run; Scribe consumes it for the morning
digest, and the memory-writer subscriber advances its cursor.

Spec: docs/superpowers/plans/2026-04-26-curator-backfill-and-nightly.md
Task 7.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from events.bus import EventBus
from events.schema import EventType, Priority

logger = logging.getLogger(__name__)

CURATOR_DAILY = "curator_daily"


def emit_curator_daily(bus: EventBus, payload: Dict[str, Any]) -> str:
    """Emit a single curator_daily event with the bootstrap/nightly summary.

    Args:
        bus: An ``EventBus`` instance (caller resolves which DB).
        payload: Must include keys ``mode`` (str: "backfill" | "nightly"),
            ``agents_updated`` (list[str]), ``patterns_seeded`` (int),
            ``skills_observed`` (int), ``drawers_scanned`` (int),
            ``degraded`` (bool), ``duration_s`` (float). Extra keys are
            preserved.

    Returns:
        The newly-emitted event_id (uuid).
    """
    et = EventType.from_string(CURATOR_DAILY)
    if et is None:
        # Schema not registered — emit as agent_error fallback so the
        # nightly pass still produces an audit row, then surface the
        # discrepancy upstream.
        logger.warning("CURATOR_DAILY EventType not registered; falling back to AGENT_ERROR")
        et = EventType.AGENT_ERROR

    # EventBus.emit signature: (event_type, source, payload, priority=, ...).
    # Passing an Event object is the wrong shape (caller of test_orchestrator
    # uses a fake bus that only stores whatever it receives).
    return bus.emit(
        et,
        "curator",
        dict(payload),
        priority=Priority.NORMAL,
    )
