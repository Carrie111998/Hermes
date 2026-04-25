"""IntentApplier -- orchestration of the intent flow.

For each intent file in the inbox:
  1. Parse (corrupt JSON -> dead-letter immediately)
  2. Idempotency check (already applied -> skip + move to processed)
  3. Pipeline.json write via PipelineManager (canonical-first)
  4. JobOps API write via JobOpsClient (Postgres mirror)
     - Transient failure: leave pipeline.json done, move file to partial/
     - Permanent failure: dead-letter
  5. Mark idempotent + move file to processed/
  6. Optionally call resume_full if metadata.thread_id is present

Pipeline.json is canonical: if step 3 succeeds, the operation is logically
successful. Step 4 failures are recorded in a partial queue for later
reconciliation but do not block subsequent intents.
"""
from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Callable, Optional

from pipeline_state import PipelineManager

from .circuit_breaker import CircuitBreakerOpen, SimpleCircuitBreaker
from .dead_letter import write_dead_letter
from .idempotency import IdempotencyTracker
from .jobops_client import (
    JobOpsClient,
    JobOpsClientPermanentError,
    JobOpsClientTransientError,
)
from .parser import IntentMessage, IntentParseError, parse_intent_file


logger = logging.getLogger(__name__)


# Intents whose protected stages must be mirrored to Postgres via tracker_only-allowed source.
PROTECTED_STAGES = {"approved", "final_submission", "applied"}


class IntentApplier:
    """Single-writer orchestrator for tracker intent messages.

    Composes parser, idempotency tracker, JobOps client, circuit breaker, and
    dead-letter helper into the canonical-first dual-write flow.

    Concurrency: single-threaded by design. The intent that the inbox is drained
    sequentially by one caller (a gateway subscriber poll loop or a one-off
    `scan_inbox()` call). If concurrent invocation is ever needed, add a Lock
    around `apply_one` -- currently `is_applied`/`mark_applied` is not race-free
    against concurrent callers, and `_move_to` would race on the same file.
    """

    def __init__(
        self,
        *,
        inbox_dir: Path,
        processed_dir: Path,
        partial_dir: Path,
        dead_letter_dir: Path,
        pipeline_manager: PipelineManager,
        jobops_client: JobOpsClient,
        idempotency: IdempotencyTracker,
        circuit_breaker: Optional[SimpleCircuitBreaker] = None,
        resume_full: Optional[Callable[[str, dict], object]] = None,
    ):
        self.inbox_dir = Path(inbox_dir)
        self.processed_dir = Path(processed_dir)
        self.partial_dir = Path(partial_dir)
        self.dead_letter_dir = Path(dead_letter_dir)
        self.pipeline_manager = pipeline_manager
        self.jobops_client = jobops_client
        self.idempotency = idempotency
        self.circuit_breaker = circuit_breaker or SimpleCircuitBreaker(
            failure_threshold=5, reset_timeout_seconds=300.0,
        )
        self.resume_full = resume_full
        for d in (self.inbox_dir, self.processed_dir, self.partial_dir, self.dead_letter_dir):
            d.mkdir(parents=True, exist_ok=True)

    def scan_inbox(self) -> dict[str, str]:
        """Process every *.json file in the inbox once. Returns {filename: outcome}."""
        results: dict[str, str] = {}
        for path in sorted(self.inbox_dir.glob("*.json")):
            results[path.name] = self.apply_one(path)
        return results

    def apply_one(self, intent_path: Path) -> str:
        """Apply a single intent. Returns one of:
        'applied' | 'skipped_idempotent' | 'partial' | 'dead_lettered'.
        """
        # Step 1: parse
        try:
            msg = parse_intent_file(intent_path)
        except IntentParseError as exc:
            write_dead_letter(
                intent_path, dead_letter_dir=self.dead_letter_dir,
                error_class="IntentParseError",
                error_message=str(exc),
                stack_trace=traceback.format_exc(),
                retry_count=0,
            )
            return "dead_lettered"

        # Step 2: idempotency
        if self.idempotency.is_applied(msg.idempotency_key):
            logger.info(
                "intent-applier: skipping already-applied key=%s file=%s",
                msg.idempotency_key, intent_path.name,
            )
            self._move_to(intent_path, self.processed_dir)
            return "skipped_idempotent"

        # Step 3: pipeline.json (canonical-first)
        original_source = msg.source
        notes_with_source = msg.notes if msg.notes else f"intent: {original_source}"
        merged_metadata = {**msg.metadata, "original_source": original_source}
        try:
            self.pipeline_manager.update_stage(
                job_id=msg.job_id,
                new_stage=msg.requested_stage,
                actor=msg.actor_id,
                source="tracker_mailbox",
                notes=notes_with_source,
                metadata=merged_metadata,
            )
        except Exception as exc:
            write_dead_letter(
                intent_path, dead_letter_dir=self.dead_letter_dir,
                error_class=exc.__class__.__name__,
                error_message=f"PipelineManager.update_stage failed: {exc}",
                stack_trace=traceback.format_exc(),
                retry_count=0,
            )
            return "dead_lettered"

        # Step 4: JobOps API (Postgres mirror)
        try:
            self.circuit_breaker.guard()
            self.jobops_client.post_legacy_stage(
                job_id=msg.job_id,
                stage=msg.requested_stage,
                actor_id="tracker",
                source="tracker_mailbox",
                notes=msg.notes,
            )
        except CircuitBreakerOpen:
            logger.warning(
                "intent-applier: JobOps circuit-breaker open; pipeline.json updated but Postgres skipped for %s",
                msg.job_id,
            )
            self._move_to(intent_path, self.partial_dir)
            self.idempotency.mark_applied(msg.idempotency_key, message_id=msg.message_id)
            return "partial"
        except JobOpsClientTransientError as exc:
            logger.warning("intent-applier: JobOps transient error for %s: %s", msg.job_id, exc)
            self.circuit_breaker.record_failure()
            self._move_to(intent_path, self.partial_dir)
            self.idempotency.mark_applied(msg.idempotency_key, message_id=msg.message_id)
            return "partial"
        except JobOpsClientPermanentError as exc:
            logger.error("intent-applier: JobOps permanent error for %s: %s", msg.job_id, exc)
            write_dead_letter(
                intent_path, dead_letter_dir=self.dead_letter_dir,
                error_class="JobOpsClientPermanentError",
                error_message=str(exc),
                stack_trace=traceback.format_exc(),
                retry_count=0,
            )
            return "dead_lettered"

        # Both writes succeeded
        self.circuit_breaker.record_success()

        # Step 5: optional HITL resume
        if self.resume_full is not None and "thread_id" in msg.metadata:
            thread_id = msg.metadata["thread_id"]
            approval = "yes" if msg.requested_stage == "approved" else "no"
            try:
                self.resume_full(thread_id, {"approval": approval})
                logger.info("intent-applier: resumed thread %s approval=%s", thread_id, approval)
            except Exception as exc:
                logger.info(
                    "intent-applier: resume_full(%s) skipped (%s)",
                    thread_id, exc.__class__.__name__,
                )

        # Step 6: mark idempotent + move to processed
        self.idempotency.mark_applied(msg.idempotency_key, message_id=msg.message_id)
        self._move_to(intent_path, self.processed_dir)
        logger.info(
            "intent-applier: applied job=%s stage=%s actor=%s original_source=%s",
            msg.job_id, msg.requested_stage, msg.actor_id, original_source,
        )
        return "applied"

    def _move_to(self, src: Path, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        src.replace(dest)
        return dest
