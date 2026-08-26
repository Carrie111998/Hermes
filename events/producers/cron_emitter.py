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

import logging
from typing import Any, Dict, Optional

from agent.redact import redact_sensitive_text
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

        Distinct from on_job_skipped_duplicate below: this is the gateway-
        downtime miss path; that is the concurrency-guard reject path.
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

    def on_job_skipped_duplicate(
        self,
        job_id: str,
        job_name: str,
        prior_cron_started_event_id: Optional[str],
        prior_elapsed_seconds: Optional[float],
        reason: str,
    ) -> str:
        """Emit cron_skipped_duplicate when the in-flight guard rejects a fire.

        Triggered by the same-job concurrency guard in cron/scheduler.py
        (Guard #3, added 2026-04-30 to close the sentinel-vip-morning
        triple-fire -- canonical event_id 4edcb4b1-aa07-4dbb-b799-8af167d4f92e).

        ``reason`` is one of:
          * ``"concurrent_fire_blocked"`` -- prior fire still healthy and running
          * ``"prior_fire_exceeded_timeout"`` -- prior fire wedged-but-tracked
          * ``"cross_process_fire_blocked"`` -- prior fire is live in ANOTHER
            process (Guard #5, 2026-08-25), proved from the execution ledger.
            ``prior_cron_started_event_id`` is always None for this reason:
            that id lives in the owning process's ``_InFlightRecord`` and is
            never written to the ledger, so it is unavailable by construction.
        """
        return self.bus.emit(
            event_type=EventType.CRON_SKIPPED_DUPLICATE,
            source=job_name,
            payload={
                "job_id": job_id,
                "job_name": job_name,
                "prior_cron_started_event_id": prior_cron_started_event_id,
                "prior_elapsed_seconds": prior_elapsed_seconds,
                "reason": reason,
            },
        )

    def on_job_skipped_min_interval(
        self,
        job_id: str,
        job_name: str,
        last_run_at: str,
        elapsed_since_last_seconds: float,
        min_seconds_between_fires: int,
    ) -> str:
        """Emit cron_skipped_min_interval when Guard #4 rejects a fire.

        Triggered by the min-seconds-between-fires guard in cron/scheduler.py
        (Guard #4, added 2026-04-30 follow-up to close the SEQUENTIAL-burst
        gap left by Guard #3).  Guard #3 only catches concurrent re-fires;
        Guard #4 catches sequential-but-too-soon fires after a prior fire
        has fully completed.  See sentinel-vip-burst-rc-2026-04-30.md §6.
        """
        return self.bus.emit(
            event_type=EventType.CRON_SKIPPED_MIN_INTERVAL,
            source=job_name,
            payload={
                "job_id": job_id,
                "job_name": job_name,
                "last_run_at": last_run_at,
                "elapsed_since_last_seconds": elapsed_since_last_seconds,
                "min_seconds_between_fires": min_seconds_between_fires,
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
        *,
        failure_details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Emit cron_completed or cron_failed event after job execution.

        If consecutive_errors >= CONSECUTIVE_FAILURE_THRESHOLD, also emits
        cron_failed_consecutive as a separate critical event.
        """
        safe_error = error
        if error:
            try:
                safe_error = redact_sensitive_text(
                    error,
                    force=True,
                    redact_url_credentials=True,
                )
            except Exception:
                # Error text crosses persistence + notification boundaries.
                # If mandatory redaction fails, retain only a safe sentinel.
                safe_error = "Error details unavailable (redaction failed)"

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
                    "error": safe_error or "Unknown error",
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
                        "error": safe_error or "Unknown error",
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
            details = dict(failure_details or {})
            if safe_error:
                details.setdefault("latest_cause", safe_error)
            cluster = self._cluster_detector.record(
                source=canonical_source,
                success=success,
                error_text=safe_error,
                details=details,
            )
            if cluster is not None:
                payload = {
                    "source": cluster.source,
                    "failure_type": cluster.failure_type,
                    "count": cluster.count,
                    "first_seen": cluster.first_seen,
                    "last_seen": cluster.last_seen,
                }
                payload.update(cluster.last_details)
                self.bus.emit(
                    event_type=EventType.AGENT_FAILURE_CLUSTER,
                    source=cluster.source,
                    payload=payload,
                )
        except Exception:
            logger.exception("FailureClusterDetector record failed for %s", job_name)

        return event_id
