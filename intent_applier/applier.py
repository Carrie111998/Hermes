"""IntentApplier -- orchestration of the intent flow.

For each intent file in the inbox:
  1. Parse (corrupt JSON -> dead-letter immediately)
  2. Idempotency check (already applied -> skip + move to processed)
  2b. Pre-flight state check (Fix A): if a native-Postgres reader is wired and
      the job is ALREADY at or past the requested stage, the intent is a
      redundant no-op — skip steps 3/3b/4 entirely (no legacy-projection write,
      no mirror, no congesting :4100 POST) and move to processed ("satisfied").
      Fails OPEN: any reader error / unknown stage falls through to the normal
      dual-write path, so the optimization can never block a real transition.
  3. Pipeline.json write via PipelineManager (canonical-first)
  3b. Mirror the intent as a PIPELINE_UPDATE message into the tracker mailbox
      inbox, so the tracker agent applies it to ITS canonical projection
      (profiles/tracker/workspace/pipeline.json) on the next cron cycle
  4. JobOps API write via JobOpsClient (Postgres mirror)
     - Transient failure (read-timeout / 5xx / socket) or breaker-open:
       leave pipeline.json done, move file to partial/, key NOT burned
     - Permanent failure: dead-letter
  5. Mark idempotent + move file to processed/
  6. Optionally call resume_full if metadata.thread_id is present

Pipeline.json is canonical: if step 3 succeeds, the operation is logically
successful. Step 4 failures are recorded in a partial queue for later
reconciliation but do not block subsequent intents.

The idempotency key represents "the Postgres mirror committed" and is burned
ONLY on a confirmed 2xx from ``post_legacy_stage`` (step 5). A transient /
breaker-open failure moves the intent to partial/ WITHOUT burning the key, so
it stays re-drivable — burning it there diverges the ledger from Postgres and
makes a later legitimate (job, stage) transition silently skipped_idempotent
(latent bug observed 2026-07-13, job 4de4f9fb :withdrawn).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import traceback
from datetime import datetime, timezone
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


# Fix A — "already at or past" business-state sets per requested legacy stage.
# Mirrors jobflow-api's OWN progression semantics (repository.ts ~2054-2064,
# the pipeline stage-count query): a job whose current_business_state is in the
# set for a requested legacy stage has already reached (or moved beyond) it, so
# re-applying that stage is a redundant no-op. Forward stages include every
# downstream state; terminal states (archived/withdrawn/rejected) match only
# themselves (nothing is "past" a terminal). Stages NOT listed here (e.g.
# 'scored', 'applied', 'final_submission') are deliberately omitted — the
# pre-flight will NOT short-circuit them and they flow through the normal gated
# dual-write path. Keep in sync with jobflow-api if the state machine changes.
_STAGE_SATISFIED_BY: dict[str, frozenset[str]] = {
    "approved": frozenset({
        "approved_for_tailor", "materials_ready", "approved_for_submission",
        "submitted", "responded", "interviewing", "offer",
    }),
    "materials_ready": frozenset({
        "materials_ready", "approved_for_submission",
        "submitted", "responded", "interviewing", "offer",
    }),
    "submission_ready": frozenset({
        "approved_for_submission", "submitted", "responded", "interviewing", "offer",
    }),
    "submitted": frozenset({"submitted", "responded", "interviewing", "offer"}),
    "interviewing": frozenset({"interviewing", "offer"}),
    "offer": frozenset({"offer"}),
    "archived": frozenset({"archived"}),
    "withdrawn": frozenset({"withdrawn"}),
    "rejected": frozenset({"rejected"}),
}


# Re-drive attempt marker: original intent stems never end in ``.rd<digits>``
# (they end in the poster tag, e.g. ``_main`` or a job8 hex), so a trailing
# ``.rdN`` on the stem is unambiguously the applier's own re-drive counter.
_REDRIVE_MARKER_RE = re.compile(r"\.rd(\d+)$")


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
        job_state_reader: Optional[Callable[[str], Optional[str]]] = None,
        redrive_base_backoff: float = 120.0,
        redrive_multiplier: float = 2.0,
        redrive_max_backoff: float = 1800.0,
        max_redrive_attempts: int = 5,
        redrive_give_up_attempts: int = 0,
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
        # Fix A: optional native-Postgres reader mapping job_id -> current
        # business_state (or None if unknown / read failed). None => pre-flight
        # disabled (every intent takes the normal dual-write path).
        self.job_state_reader = job_state_reader
        self.redrive_base_backoff = redrive_base_backoff
        self.redrive_multiplier = redrive_multiplier
        self.redrive_max_backoff = redrive_max_backoff
        self.max_redrive_attempts = max_redrive_attempts
        # Fix B: attempts after which a partial is truly "capped" (given up,
        # left for the PartialBacklogMonitor alert). 0 => NEVER give up — past
        # max_redrive_attempts the backoff is already pinned at redrive_max_backoff
        # (a slow lane), so transient partials keep self-healing at that cadence.
        # Everything in partial/ is transient by construction (permanent/gate
        # failures dead-letter), so a terminal cap surrenders on failures that
        # are only ever "try later".
        self.redrive_give_up_attempts = redrive_give_up_attempts
        for d in (self.inbox_dir, self.processed_dir, self.partial_dir, self.dead_letter_dir):
            d.mkdir(parents=True, exist_ok=True)

    def scan_inbox(self) -> dict[str, str]:
        """Process every intent JSON file in the inbox once. Returns {filename: outcome}.

        Filename pattern is `*_INTENT_*.json` (matches both STATE_TRANSITION_INTENT
        and APPROVAL_INTENT). The tracker inbox is shared with other producers
        (sentinel VIP_DISCOVERY, scout job_discovery, etc.) so we must NOT consume
        non-intent files — they belong to the tracker LLM cron.
        """
        results: dict[str, str] = {}
        for path in sorted(self.inbox_dir.glob("*_INTENT_*.json")):
            results[path.name] = self.apply_one(path)
        return results

    def apply_one(self, intent_path: Path) -> str:
        """Apply a single intent. Returns one of:
        'applied' | 'skipped_idempotent' | 'satisfied' | 'partial' | 'dead_lettered'.
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

        # Step 2b: pre-flight (Fix A) — suppress redundant no-op intents.
        # If native Postgres already shows the job at or past the requested
        # stage, the intent's goal is already met. Skip the whole dual-write
        # (no legacy-projection regression, no redundant mirror, no congesting
        # :4100 POST) and burn the key so the redundant intent can't recur.
        if self._already_satisfied(msg):
            logger.info(
                "intent-applier: pre-flight satisfied job=%s stage=%s "
                "(Postgres already at/past target); skipping dual-write",
                msg.job_id, msg.requested_stage,
            )
            self.idempotency.mark_applied(msg.idempotency_key, message_id=msg.message_id)
            self._move_to(intent_path, self.processed_dir)
            return "satisfied"

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
            # The legacy projection write failed, but the operator's decision
            # must still reach the tracker agent — emit before dead-lettering
            # so the next tracker cycle can apply it to the canonical store.
            self._emit_canonical_pipeline_update(
                msg, pipeline_manager_error=f"{exc.__class__.__name__}: {exc}",
            )
            write_dead_letter(
                intent_path, dead_letter_dir=self.dead_letter_dir,
                error_class=exc.__class__.__name__,
                error_message=f"PipelineManager.update_stage failed: {exc}",
                stack_trace=traceback.format_exc(),
                retry_count=0,
            )
            return "dead_lettered"

        # Step 3b: mirror into the tracker agent's mailbox lane (canonical feed)
        self._emit_canonical_pipeline_update(msg)

        # Step 4: JobOps API (Postgres mirror)
        try:
            self.circuit_breaker.guard()
            self.jobops_client.post_legacy_stage(
                job_id=msg.job_id,
                stage=msg.requested_stage,
                actor_id=msg.actor_id,
                source="tracker_mailbox",
                notes=msg.notes,
            )
        except CircuitBreakerOpen:
            logger.warning(
                "intent-applier: JobOps circuit-breaker open; pipeline.json updated but "
                "Postgres skipped for %s — idempotency key NOT burned so the intent stays "
                "re-drivable from partial/",
                msg.job_id,
            )
            # Do NOT mark_applied: the Postgres mirror never committed (JobOps
            # wasn't even attempted). Burning the key here would permanently
            # diverge the ledger from Postgres and cause a future legitimate
            # (job, stage) transition to be silently skipped_idempotent
            # (observed 2026-07-13, job 4de4f9fb :withdrawn). The key is
            # committed only on a confirmed 2xx at step 6.
            self._move_to_partial(intent_path)
            return "partial"
        except JobOpsClientTransientError as exc:
            logger.warning("intent-applier: JobOps transient error for %s: %s", msg.job_id, exc)
            self.circuit_breaker.record_failure()
            # Do NOT mark_applied: a transient failure (read-timeout / 5xx /
            # socket error) means post_legacy_stage did NOT commit to Postgres.
            # Leave the key unburned so this intent remains re-drivable from
            # partial/. The step-3b PIPELINE_UPDATE mirror is intentionally left
            # in place (not gated on Postgres success): it carries the same
            # canonical decision as the step-3 legacy projection write, dedupes
            # downstream by idempotency_key, and gating only the mirror would
            # desync the legacy vs tracker-canonical projections. See the
            # _emit_canonical_pipeline_update docstring.
            self._move_to_partial(intent_path)
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

    def _already_satisfied(self, msg: IntentMessage) -> bool:
        """True iff Postgres already shows the job at or past the requested stage.

        Fix A pre-flight. Fails OPEN in every ambiguous case so it can only ever
        SKIP redundant work, never block a real transition:
          * no reader wired               -> False (normal dual-write)
          * reader raises / returns None   -> False (Postgres unknown -> apply)
          * requested stage not in the known progression map -> False
        Only when the reader returns a concrete business_state that lives in the
        requested stage's "at or past" set do we short-circuit.
        """
        reader = self.job_state_reader
        if reader is None:
            return False
        satisfied_by = _STAGE_SATISFIED_BY.get(msg.requested_stage)
        if satisfied_by is None:
            return False
        try:
            current = reader(msg.job_id)
        except Exception:
            logger.debug(
                "intent-applier: pre-flight state read failed for job=%s; "
                "falling through to dual-write", msg.job_id, exc_info=True,
            )
            return False
        return bool(current) and current in satisfied_by

    def _emit_canonical_pipeline_update(
        self,
        msg: IntentMessage,
        *,
        pipeline_manager_error: Optional[str] = None,
    ) -> None:
        """Mirror an operator intent as a PIPELINE_UPDATE in the tracker inbox.

        PipelineManager writes the legacy ``workspaces/tracker/pipeline.json``
        projection, but the cron agents read the tracker's canonical
        ``profiles/tracker/workspace/pipeline.json`` — which only the tracker
        LLM maintains, by ingesting PIPELINE_UPDATE messages from its inbox.
        Without this mirror, dashboard/Telegram stage changes never reach the
        canonical store and postgres-sync reverts their Postgres side within
        15 minutes.

        The intent inbox and the tracker mailbox inbox are the same directory;
        ``scan_inbox`` only consumes ``*_INTENT_*.json``, and this filename
        must never contain ``_INTENT_`` (Windows globbing is case-insensitive,
        so even a lowercase ``_intent_`` infix would be re-consumed).

        Best-effort: a failure here is logged loudly but never changes the
        intent's outcome — the JobOps mirror and tracker parity sync remain
        as fallbacks.
        """
        try:
            now = datetime.now(timezone.utc)
            metadata = {
                **msg.metadata,
                "actor_id": msg.actor_id,
                "original_source": msg.source,
                "intent_type": msg.intent_type,
                "idempotency_key": msg.idempotency_key,
                "emitted_by": "tracker-intent-applier",
            }
            if msg.notes:
                metadata["notes"] = msg.notes
            if pipeline_manager_error:
                metadata["pipeline_manager_error"] = pipeline_manager_error
            body = {
                "type": "PIPELINE_UPDATE",
                "from": "operator",
                "to": "tracker",
                "job_id": msg.job_id,
                "timestamp": now.isoformat(),
                "correlation_id": msg.message_id,
                "payload": {
                    "job_id": msg.job_id,
                    "from_stage": None,
                    "to_stage": msg.requested_stage,
                    "metadata": metadata,
                },
            }
            fname = (
                f"{now.strftime('%Y%m%dT%H%M%S%fZ')}_PIPELINE_UPDATE_operator_"
                f"{str(msg.job_id)[:8]}.json"
            )
            tmp = self.inbox_dir / (fname + ".tmp")
            tmp.write_text(
                json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self.inbox_dir / fname)
            logger.info(
                "intent-applier: mirrored intent to tracker mailbox %s (job=%s stage=%s)",
                fname, msg.job_id, msg.requested_stage,
            )
        except Exception:
            logger.exception(
                "intent-applier: failed to mirror PIPELINE_UPDATE for job=%s — "
                "canonical pipeline will lag until tracker parity sync",
                msg.job_id,
            )

    def _move_to(self, src: Path, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        src.replace(dest)
        return dest

    def _move_to_partial(self, src: Path) -> Path:
        """Move an intent into partial/ and stamp its mtime to NOW.

        rename/replace PRESERVE mtime, so without this the file's mtime would be
        the intent's *creation* time and the per-attempt exponential backoff in
        redrive_partials() would never space retries. Stamping on landing makes
        partial mtime mean "when it last entered partial/".
        """
        dest = self._move_to(src, self.partial_dir)
        os.utime(dest, None)
        return dest

    def _parse_redrive_attempt(self, path: Path) -> int:
        """Parse the re-drive attempt count N from a ``.rdN`` filename marker.

        ``Path.stem`` has already stripped ``.json``. No marker => attempt 0.
        """
        m = _REDRIVE_MARKER_RE.search(path.stem)
        return int(m.group(1)) if m else 0

    def _bump_redrive_marker(self, name: str, new_n: int) -> str:
        """Return ``name`` (a ``*.json`` filename) with any ``.rdN`` marker
        replaced by ``.rd{new_n}``."""
        stem = name[:-5] if name.endswith(".json") else name
        stem = _REDRIVE_MARKER_RE.sub("", stem)
        return f"{stem}.rd{new_n}.json"

    def redrive_partials(self) -> dict[str, str]:
        """Move backoff-eligible partials back to inbox/ for reprocessing.

        Pure filesystem logic; ALWAYS acts (the feature flag lives at the
        subscriber layer). MUST be called on the single-writer applier thread —
        it shares _move_to/glob semantics with scan_inbox and is not race-free
        against a concurrent scan.

        For each ``*_INTENT_*.json`` in partial/:
          * attempt N = ``.rdN`` marker (absent => 0);
          * if redrive_give_up_attempts > 0 and N >= it => leave in place
            ("capped") for the PartialBacklogMonitor alert. Default 0 => NEVER
            give up: every partial is transient by construction (permanent/gate
            failures dead-letter, never land here), so surrendering would strand
            a "try later" failure forever. Past max_redrive_attempts the backoff
            below is already pinned at redrive_max_backoff, i.e. a slow lane that
            keeps self-healing (esp. paired with the Fix A pre-flight, which
            clears the common already-satisfied re-drive without a :4100 write);
          * else eligible iff ``now - mtime >= min(base * mult**N, max_backoff)``,
            where mtime is the "entered-partial" clock set by _move_to_partial;
          * eligible => rename to ``.rd{N+1}`` and move to inbox/; the next 1s
            scan_inbox re-runs steps 3/3b/4 (key unburned => it re-applies).

        Returns {original_filename: "redriven" | "waiting" | "capped"}.
        """
        results: dict[str, str] = {}
        now = time.time()
        for path in sorted(self.partial_dir.glob("*_INTENT_*.json")):
            n = self._parse_redrive_attempt(path)
            if self.redrive_give_up_attempts and n >= self.redrive_give_up_attempts:
                results[path.name] = "capped"
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                # File vanished mid-sweep (raced by another mover) — skip.
                continue
            backoff = min(
                self.redrive_base_backoff * (self.redrive_multiplier ** n),
                self.redrive_max_backoff,
            )
            if age < backoff:
                results[path.name] = "waiting"
                continue
            new_name = self._bump_redrive_marker(path.name, n + 1)
            self.inbox_dir.mkdir(parents=True, exist_ok=True)
            try:
                path.replace(self.inbox_dir / new_name)
            except OSError:
                logger.exception(
                    "intent-applier: failed to re-drive partial %s", path.name
                )
                continue
            results[path.name] = "redriven"
            logger.info(
                "intent-applier: re-driving partial %s -> inbox/%s (attempt %d)",
                path.name, new_name, n + 1,
            )
        return results
