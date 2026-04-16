"""CronEventEmitter -- emits lifecycle events from the cron execution pipeline.

Hooks into the cron scheduler's tick()/run_job() cycle to emit:
  - cron_started: before job execution
  - cron_completed: after successful execution (+ domain events from output)
  - cron_failed: after failed execution
  - cron_failed_consecutive: when consecutive failures reach threshold

Output parsing inspects agent output for domain signals (job discoveries,
scores, submissions, stage transitions) and emits corresponding typed events.
"""

import json
import logging
import re
from typing import Dict, List, Optional

from events.bus import EventBus
from events.schema import EventType, Priority

logger = logging.getLogger(__name__)

CONSECUTIVE_FAILURE_THRESHOLD = 3

# Patterns for extracting domain signals from agent output.
# Agents produce structured mailbox messages — these patterns detect
# common signal keywords in both structured and freeform output.
_DOMAIN_PATTERNS: List[Dict] = [
    {
        "pattern": re.compile(r"(?:found|discovered)\s+(\d+)\s+(?:new\s+)?jobs?", re.I),
        "event_type": EventType.JOB_DISCOVERED,
        "extract": lambda m, out: {"count": int(m.group(1)), "source": "cron-output"},
    },
    {
        "pattern": re.compile(r"scored?\s+(\d+(?:\.\d+)?)\s*/\s*10.*?(?:for|at|:)\s*(.+?)(?:\.|$)", re.I),
        "event_type": EventType.JOB_SCORED,
        "extract": lambda m, out: {"score": float(m.group(1)), "title": m.group(2).strip()},
    },
    {
        "pattern": re.compile(r"high.?score.*?(\d+(?:\.\d+)?)\s*/?\s*10?.*?(?:for|at|:)\s*(.+?)(?:\.|$)", re.I),
        "event_type": EventType.JOB_HIGH_SCORE,
        "extract": lambda m, out: {"score": float(m.group(1)), "title": m.group(2).strip()},
    },
    {
        "pattern": re.compile(r"(?:submitted|applied)\s+(?:to|for|at)\s+(.+?)(?:\.|$)", re.I),
        "event_type": EventType.APPLICATION_SUBMITTED,
        "extract": lambda m, out: {"company": m.group(1).strip()},
    },
    {
        "pattern": re.compile(r"(?:stage|pipeline)\s+(?:transition|moved?|changed?).*?(?:to|→)\s+(\w+)", re.I),
        "event_type": EventType.STAGE_TRANSITION,
        "extract": lambda m, out: {"new_stage": m.group(1).strip()},
    },
    {
        "pattern": re.compile(r"(?:resume|cover.?letter)\s+(?:generated|created|tailored)", re.I),
        "event_type": EventType.TAILOR_COMPLETED,
        "extract": lambda m, out: {"detail": m.group(0).strip()},
    },
    {
        "pattern": re.compile(r"(?:dry.?run|application)\s+(?:complete|ready)\s+(?:for|at)\s+(.+?)(?:\.|$)", re.I),
        "event_type": EventType.APPLICATION_READY,
        "extract": lambda m, out: {"company": m.group(1).strip()},
    },
    {
        "pattern": re.compile(r"VIP\s+(?:job|listing).*?(?:from|at|:)\s*(.+?)(?:\.|$)", re.I),
        "event_type": EventType.JOB_VIP_DISCOVERED,
        "extract": lambda m, out: {"company": m.group(1).strip()},
    },
]


class CronEventEmitter:
    """Emits cron lifecycle events into the EventBus.

    Also parses agent output for domain signals and emits corresponding
    typed events (job discoveries, scores, submissions, etc.).
    """

    def __init__(self, bus: EventBus):
        self.bus = bus

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

        On success, also parses the output for domain signals and emits
        additional typed events (job_discovered, job_scored, etc.).

        If consecutive_errors >= CONSECUTIVE_FAILURE_THRESHOLD, also emits
        cron_failed_consecutive as a separate critical event.
        """
        if success:
            event_id = self.bus.emit(
                event_type=EventType.CRON_COMPLETED,
                source=job_name,
                payload={
                    "job_id": job_id,
                    "job_name": job_name,
                    "duration": duration,
                    "output_summary": output_summary or "",
                },
            )

            # Parse output for domain events
            if output_summary:
                self._parse_output_for_domain_events(
                    job_name=job_name,
                    job_id=job_id,
                    output=output_summary,
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

        return event_id

    def _parse_output_for_domain_events(
        self,
        job_name: str,
        job_id: str,
        output: str,
    ) -> int:
        """Parse agent output for domain signals and emit typed events.

        Returns the count of domain events emitted.
        """
        emitted = 0
        seen_types = set()  # Avoid emitting duplicate types from same output

        for spec in _DOMAIN_PATTERNS:
            if spec["event_type"] in seen_types:
                continue
            match = spec["pattern"].search(output)
            if match:
                try:
                    payload = spec["extract"](match, output)
                    payload["_parsed_from"] = job_name
                    self.bus.emit(
                        event_type=spec["event_type"],
                        source=job_name,
                        payload=payload,
                        job_id=job_id,
                    )
                    seen_types.add(spec["event_type"])
                    emitted += 1
                    logger.debug(
                        "CronEventEmitter: parsed %s from %s output",
                        spec["event_type"].type_string, job_name,
                    )
                except Exception:
                    logger.exception(
                        "CronEventEmitter: failed to parse %s from output",
                        spec["event_type"].type_string,
                    )

        return emitted
