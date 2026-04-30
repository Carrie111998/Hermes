"""CronEventEmitter -- emits lifecycle events from the cron execution pipeline.

Hooks into the cron scheduler's tick()/run_job() cycle to emit:
  - cron_started: before job execution
  - cron_completed: after successful execution
  - cron_failed: after failed execution
  - cron_failed_consecutive: when consecutive failures reach threshold
  - agent_failure_cluster: when 3 consecutive same-type failures occur
    for a single source (Hermes Revival §6 post-hoc Critic trigger)

Domain events (job_discovered, job_scored, etc.) come from MailboxTranslator
consuming mailbox_message events — see events/subscribers/mailbox_translator.py.
"""

import json
import logging
from typing import Optional

from events.bus import EventBus
from events.cluster_detector import FailureClusterDetector
from events.paths import failure_cluster_state_path
from events.producers.agent_source_mapping import canonical_agent_source
from events.schema import EventType, Priority

logger = logging.getLogger(__name__)

CONSECUTIVE_FAILURE_THRESHOLD = 3

# Minimum chars for cron_completed output_summary to be considered
# substantive. Below this (or "[SILENT]"), keep default NORMAL priority so
# the event is batched / digest_only-gated. Above it, boost to HIGH so the
# content surfaces in Mission Control's system topic instead of being dropped.
MEANINGFUL_OUTPUT_CHAR_THRESHOLD = 120


class CronEventEmitter:
    """Emits cron lifecycle events into the EventBus."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._cluster_detector = FailureClusterDetector(
            state_path=failure_cluster_state_path(),
        )

    def on_job_started(
        self,
        job_id: str,
        job_name: str,
        schedule: str,
    ) -> str:
        """Emit cron_started event before job execution."""
        return self.bus.emit(
            event_type=EventType.CRON_STARTED,
            source=job_name,
            payload={
                "job_id": job_id,
                "job_name": job_name,
                "schedule": schedule,
            },
        )

    def on_job_skipped(
        self,
        job_id: str,
        job_name: str,
        missed_at: str,
        missed_seconds: int,
        schedule_kind: str,
        reason: str,
    ) -> str:
        """Emit cron_skipped when a recurring job's missed fire was fast-forwarded.

        Reasons:
          - "default_period_cap"   — weekly or unknown-period cron; never fires stale
          - "miss_exceeded_24h_cap" — daily cron missed for >24h
          - "skip_only"            — explicit recovery_policy="skip_only" opt-out

        Routed to the watchdog_alerts Telegram topic.
        """
        return self.bus.emit(
            event_type=EventType.CRON_SKIPPED,
            source=job_name,
            payload={
                "job_id": job_id,
                "job_name": job_name,
                "missed_at": missed_at,
                "missed_seconds": missed_seconds,
                "schedule_kind": schedule_kind,
                "reason": reason,
            },
        )

    def on_job_completed(
        self,
        job_id: str,
        job_name: str,
        success: bool,
        duration: float,
        output_summary: Optional[str] = None,
        error: Optional[str] = None,
        consecutive_errors: int = 0,
    ) -> str:
        """Emit cron_completed or cron_failed event after job execution.

        If consecutive_errors >= CONSECUTIVE_FAILURE_THRESHOLD, also emits
        cron_failed_consecutive as a separate critical event.
        """
        if success:
            # Boost priority when the output is substantive so it isn't
            # silently gated by system-topic digest_only verbosity. Keeps
            # [SILENT] and short heartbeat outputs at NORMAL (batched).
            summary = (output_summary or "").strip()
            if summary and summary != "[SILENT]" and len(summary) >= MEANINGFUL_OUTPUT_CHAR_THRESHOLD:
                completed_priority = Priority.HIGH
            else:
                completed_priority = None  # default NORMAL
            event_id = self.bus.emit(
                event_type=EventType.CRON_COMPLETED,
                source=job_name,
                priority=completed_priority,
                payload={
                    "job_id": job_id,
                    "job_name": job_name,
                    "duration": duration,
                    "output_summary": output_summary or "",
                },
            )
        else:
            event_id = self.bus.emit(
                event_type=EventType.CRON_FAILED,
                source=job_name,
                payload={
                    "job_id": job_id,
                    "job_name": job_name,
                    "duration": duration,
                    "error": error or "Unknown error",
                    "consecutive_errors": consecutive_errors,
                },
            )

            if consecutive_errors >= CONSECUTIVE_FAILURE_THRESHOLD:
                self.bus.emit(
                    event_type=EventType.CRON_FAILED_CONSECUTIVE,
                    source=job_name,
                    payload={
                        "job_id": job_id,
                        "job_name": job_name,
                        "consecutive_errors": consecutive_errors,
                        "error": error or "Unknown error",
                    },
                )

        # Feed the detector with every outcome (including successes — those
        # clear the per-source window).  Emit a focused cluster event when
        # the detector reports a same-type 3-in-a-row.  Wrapped so a
        # detector failure (e.g. corrupt state file) cannot break the emitter.
        #
        # Canonicalise the source BEFORE recording so the per-source window
        # state is shared with the parallel mailbox-translator path
        # (events/subscribers/mailbox_translator.py).  Without this, the
        # cron path keys windows under 'jobflow-applier' while the mailbox
        # path keys under 'applier' — same agent, two windows, two cluster
        # emissions.  See profiles/critic/workspace/watchdog-dedup-proposal-2026-04-29.md.
        try:
            canonical_source = canonical_agent_source(job_name)
            cluster = self._cluster_detector.record(
                source=canonical_source,
                success=success,
                error_text=error,
            )
            if cluster is not None:
                self.bus.emit(
                    event_type=EventType.AGENT_FAILURE_CLUSTER,
                    source=cluster.source,
                    payload={
                        "source": cluster.source,
                        "failure_type": cluster.failure_type,
                        "count": cluster.count,
                        "first_seen": cluster.first_seen,
                        "last_seen": cluster.last_seen,
                    },
                )
        except Exception:
            logger.exception("FailureClusterDetector record failed for %s", job_name)

        return event_id
