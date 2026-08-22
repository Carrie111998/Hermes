"""MailboxTranslator — converts mailbox_message events into typed domain events.

Subscribes to mailbox_message (produced by MailboxWatcher) and emits
typed JOB_SCORED, JOB_HIGH_SCORE, APPLICATION_SUBMITTED, STAGE_TRANSITION,
etc. based on the message_type + inner payload.

This subscriber replaces the dead regex-based output parser in
CronEventEmitter that was never producing domain events.

Failure-cluster wiring (Hermes Revival §6 post-hoc Critic trigger):
the ERROR-message branch also feeds FailureClusterDetector so that
agents reporting failures via structured mailbox messages (not via a
non-zero cron exit code) still trigger AGENT_FAILURE_CLUSTER.  This
closes a gap from the cron-emitter wiring (events/producers/cron_emitter.py)
which only saw failures that surfaced as cron exit codes.
"""

import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from agent.redact import redact_sensitive_text
from events.bus import EventBus
from events.cluster_detector import FailureClusterDetector
from events.paths import failure_cluster_state_path
from events.producers.agent_source_mapping import canonical_agent_source
from events.schema import Event, EventType, Priority
from events.subscribers.base import BaseSubscriber
from pipeline_state.manager import PIPELINE_PATH

logger = logging.getLogger(__name__)

HIGH_SCORE_THRESHOLD = 8.75

# The matcher reports every scored job through BOTH a per-job SCORE_RESULT
# file and the run's SCORE_BATCH_SUMMARY results[] (and may add a
# HIGH_SCORE_ALERT), so translating each message independently double-emits
# JOB_SCORED / JOB_HIGH_SCORE for the same job — the batch rows carry no
# `dimensions`, which is the dimensions:null twin seen on the live bus
# 2026-07-10..07-18. Either lane can also arrive alone (07-15 runs were
# SCORE_RESULT-only, 07-10 00:14 batch-only), so neither message type can
# simply be dropped: dedupe at emission by job identity + score instead.
# Window matches the notifier RepeatGuard (30 min): wide enough for the
# observed 3-minute straggler (Transamerica 07-11), narrow enough that a
# genuine rescore in a later matcher run (>= 2 h apart) still emits.
SCORE_DEDUP_WINDOW_SECONDS = 1800.0
_SCORE_DEDUP_MAX_ENTRIES = 512
_SCORE_EVENT_TYPES = (EventType.JOB_SCORED, EventType.JOB_HIGH_SCORE)
_FAILURE_DETAIL_FIELDS = (
    "error_code",
    "phase",
    "deadline_seconds",
    "exception_type",
)


def _safe_failure_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        return redact_sensitive_text(
            value,
            force=True,
            redact_url_credentials=True,
        )
    except Exception:
        return None


def _safe_failure_field(field: str, value: Any) -> Any:
    if field == "deadline_seconds":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        return None
    return _safe_failure_text(value)


def _failure_details(inner: Dict[str, Any]) -> Dict[str, Any]:
    """Extract only producer-provided diagnostics, without inference."""
    context = inner.get("context")
    context = context if isinstance(context, dict) else {}
    details: Dict[str, Any] = {}
    for field in _FAILURE_DETAIL_FIELDS:
        value = inner.get(field)
        if value is None:
            value = context.get(field)
        safe = _safe_failure_field(field, value)
        if safe is not None:
            details[field] = safe
    cause = (
        inner.get("latest_cause")
        or context.get("latest_cause")
        or inner.get("message")
        or inner.get("error")
    )
    safe_cause = _safe_failure_text(cause)
    if safe_cause:
        details["latest_cause"] = safe_cause
    return details


def _agent_error_payload(inner: Dict[str, Any]) -> Dict[str, Any]:
    """Build a diagnostic-preserving, secret-safe AGENT_ERROR payload."""
    payload: Dict[str, Any] = {}
    for field in ("message", "source_agent", "traceback"):
        safe = _safe_failure_text(inner.get(field))
        if safe is not None:
            payload[field] = safe
    payload.update(_failure_details(inner))
    return payload


_INTERVIEW_PATTERNS = [
    re.compile(r"\binterview\s+(?:scheduled|invitation|request|invite)", re.I),
    re.compile(r"\bphone\s+screen", re.I),
    re.compile(r"\b(?:schedule|set up)\s+an?\s+interview", re.I),
]
_OFFER_PATTERNS = [
    re.compile(r"\b(?:pleased|delighted|happy)\s+to\s+offer", re.I),
    re.compile(r"\boffer\s+(?:letter|of\s+employment)", re.I),
    re.compile(r"\bextended\s+an?\s+offer", re.I),
]


def _stage_transition_payload(
    d: Dict[str, Any], from_agent: Optional[str] = None
) -> Dict[str, Any]:
    """Normalize PIPELINE_UPDATE payloads from tracker/tailor mailbox messages.

    Emits the `prior_stage` key that both the pipeline_state.manager producer
    and the TelegramNotifier formatter treat as canonical. `previous_stage` is
    also emitted as a back-compat alias for any older consumer.  `actor` is
    populated so the formatter's "(by <actor>)" clause is not "(by ?)":
    an explicit `actor` in the message wins, otherwise it defaults to the
    sending agent (mailbox `from`), canonicalised.
    """
    metadata = d.get("metadata") or {}
    job_id = d.get("job_id")
    job_key = d.get("job_key") or job_id
    prior_stage = d.get("prior_stage")
    if prior_stage is None:
        prior_stage = d.get("previous_stage")
    if prior_stage is None:
        prior_stage = d.get("from_stage")
    new_stage = d.get("new_stage")
    if new_stage is None:
        new_stage = d.get("to_stage")
    company = d.get("company") or metadata.get("company")
    title = d.get("title") or metadata.get("title")
    actor = d.get("actor")
    if actor is None and from_agent:
        actor = canonical_agent_source(from_agent)

    out: Dict[str, Any] = {}
    if job_key is not None:
        out["job_key"] = job_key
    if job_id is not None:
        out["job_id"] = job_id
    if prior_stage is not None:
        out["prior_stage"] = prior_stage
        # Back-compat alias — older consumers keyed on `previous_stage`.
        out["previous_stage"] = prior_stage
    if new_stage is not None:
        out["new_stage"] = new_stage
    if company is not None:
        out["company"] = company
    if title is not None:
        out["title"] = title
    if actor is not None:
        out["actor"] = actor
    return out


class MailboxTranslator(BaseSubscriber):
    subscriber_id = "mailbox-translator"
    poll_interval_seconds = 5
    event_types = [EventType.MAILBOX_MESSAGE]

    def __init__(self, bus: EventBus):
        super().__init__(bus)
        # Shares the same canonical state file as CronEventEmitter — both
        # producers funnel into one detector window so a mix of cron-exit-
        # code failures and structured ERROR mailbox messages from the same
        # agent still cluster correctly.
        self._cluster_detector = FailureClusterDetector(
            state_path=failure_cluster_state_path(),
        )
        # mtime-keyed cache for pipeline-state title/company enrichment so a
        # burst of PIPELINE_UPDATE events (matcher scores/archives many jobs at
        # once) rebuilds the index at most once per pipeline.json write.
        self._pipeline_index_cache: Optional[Tuple[float, Dict[str, Dict[str, str]]]] = None
        # (event_type, job identity, score) -> last-emit monotonic time.
        # First emission within the window wins; in-memory only (duplicates
        # arrive seconds-to-minutes apart, so restart loss is harmless).
        self._recent_score_emissions: "OrderedDict[Tuple[str, str, str], float]" = OrderedDict()

    def _pipeline_metadata(self, job_ref: Optional[str]) -> Dict[str, str]:
        """Best-effort {title, company} lookup by job id/key from pipeline
        state. Returns {} on any miss or error — enrichment must never break
        event emission. Cached and invalidated by pipeline.json mtime.
        """
        if not job_ref:
            return {}
        try:
            path = PIPELINE_PATH
            mtime = path.stat().st_mtime
            cache = self._pipeline_index_cache
            if cache is None or cache[0] != mtime:
                raw = path.read_bytes()
                if raw.startswith(b"\xef\xbb\xbf"):  # strip BOM (Windows-authored)
                    raw = raw[3:]
                data = json.loads(raw.decode("utf-8"))
                jobs = data.get("jobs", [])
                # On this host Tracker projects `jobs` as a dict keyed by
                # job_id; older/other producers use a list. Support both —
                # iterating a dict yields keys (strings), which silently
                # emptied this index before (every j.get() raised and the
                # broad except returned {}), so title/company backfill never
                # populated the Telegram STAGE_TRANSITION head.
                job_records = jobs.values() if isinstance(jobs, dict) else jobs
                index: Dict[str, Dict[str, str]] = {}
                for j in job_records:
                    if not isinstance(j, dict):
                        continue
                    jid = j.get("job_id") or j.get("id")
                    if not jid:
                        continue
                    meta = {}
                    if j.get("title"):
                        meta["title"] = j["title"]
                    if j.get("company"):
                        meta["company"] = j["company"]
                    index[jid] = meta
                cache = (mtime, index)
                self._pipeline_index_cache = cache
            return dict(cache[1].get(job_ref, {}))
        except Exception:
            return {}

    def _backfill_company_title(
        self, payload: Dict[str, Any], *job_refs: Optional[str]
    ) -> Dict[str, Any]:
        """Fill missing title/company from pipeline state. Mutates and returns.

        Best-effort by construction: `_pipeline_metadata` swallows every error,
        and a producer-supplied value is never overwritten.
        """
        if payload.get("title") and payload.get("company"):
            return payload
        for ref in job_refs:
            meta = self._pipeline_metadata(ref)
            for k in ("title", "company"):
                if not payload.get(k) and meta.get(k):
                    payload[k] = meta[k]
            if payload.get("title") and payload.get("company"):
                break
        return payload

    def _identified_payload(
        self, inner: Dict[str, Any], fields: List[str]
    ) -> Dict[str, Any]:
        """`_copy_fields` plus the aliases the mailbox producers actually speak.

        The agents say `job_id`/`api_job_id` and `files`/`screenshots`; these
        contracts ask for `job_key` and `artifacts`, and `_copy_fields` drops
        anything absent. That vocabulary gap is why `application_ready` and
        `followup_due` landed as `{}` and `tailor_completed` carried only
        company/title on the live bus.

        `job_key` matters beyond rendering: `handle` keys the bus `job_id`
        column off it, so without it an event cannot be correlated to its job.

        Anything the producer supplies wins; this only fills gaps.
        """
        payload = _copy_fields(inner, fields)

        if "job_key" in fields and not payload.get("job_key"):
            job_ref = inner.get("job_id") or inner.get("api_job_id")
            if job_ref:
                payload["job_key"] = job_ref

        if "artifacts" in fields and not payload.get("artifacts"):
            evidence = inner.get("files") or inner.get("screenshots")
            if evidence:
                payload["artifacts"] = evidence

        if "company" in fields or "title" in fields:
            self._backfill_company_title(
                payload,
                payload.get("job_key"),
                inner.get("job_id"),
                inner.get("api_job_id"),
            )
        return payload

    def _blocked_question_payload(self, inner: Dict[str, Any]) -> Dict[str, Any]:
        """APPLICATION_BLOCKED payload, tolerant of the applier's envelope.

        Two producers emit BLOCKED_QUESTION. The notifier-bridge
        (services/notifier-bridge/src/transport.ts `buildMailboxEnvelope`)
        satisfies the company/title/job_key/question contract directly. The
        applier writes its own envelope straight into the mailbox with
        job_id/api_job_id/questions/failureMessage instead — a plain
        `_copy_fields` of the four contract keys dropped all of them, so every
        `application_blocked` event on the live bus from 2026-07-20 to
        2026-08-19 landed as `{}`.

        Anything the producer supplies wins; this only fills the gaps, so a
        fixed producer passes through untouched.
        """
        payload = self._identified_payload(
            inner, ["company", "title", "job_key", "question"])

        # Always present: both the WhatsApp CRITICAL page and
        # MailboxWatcher._summarize key on `question`, and a missing key is what
        # rendered as "needs your input".
        if not payload.get("question"):
            payload["question"] = _blocked_question_text(inner)

        # The ATS's own option labels, carried in their own key so they survive
        # `question`'s 200-char summary budget. Preserve the producer's three
        # states: absent means the popup was never observed, [] means it was
        # observed and offered nothing, and a non-empty list is the exact set of
        # labels. Renderers may present the first two alike, but downstream code
        # must still be able to tell those claims apart.
        options = _blocked_question_options(inner)
        if options is not None:
            payload["options"] = options

        return payload

    def _submit_result_emissions(
        self, inner: Dict[str, Any]
    ) -> List[Tuple[EventType, Dict[str, Any], None]]:
        """Translate the applier's report of a REAL submission.

        SUBMIT_RESULT (tmp_ready_sweep_cron.py:811,834) is the applier's only
        statement that a submission actually happened, and it had no branch at
        all — so the sole `application_submitted` event ever on the bus was a
        synthetic Mission-Control drill. The branch that did exist,
        SUBMIT_CONFIRM, was Diego's go-ahead — an authorization, not an
        outcome — and has since been removed, leaving this the one producer.

        Success and failure share the message type and differ only in
        `status`, so they must not collapse: APPLICATION_SUBMITTED is a
        _SUCCESS_EVENT_TYPE while APPLICATION_FAILED is CRITICAL
        (events/outcomes.py), and booking a failed submit as success is worse
        than not reporting it.
        """
        if inner.get("status") == "submitted":
            payload = self._identified_payload(
                inner, ["company", "title", "job_key", "submission_id",
                        "artifacts"])
            if not payload.get("submission_id") and inner.get("confirmation_id"):
                payload["submission_id"] = inner["confirmation_id"]
            return [(EventType.APPLICATION_SUBMITTED, payload, None)]

        payload = self._identified_payload(
            inner, ["company", "title", "job_key", "error", "artifacts"])
        return [(EventType.APPLICATION_FAILED, payload, None)]

    def _followup_emissions(
        self, inner: Dict[str, Any]
    ) -> List[Tuple[EventType, Dict[str, Any], None]]:
        """Fan a FOLLOWUP_ALERT batch out into one FOLLOWUP_DUE per job.

        Unlike the other contract mismatches this one is STRUCTURAL: the
        tracker sends a `jobs` (2026-08-07, 08-09) or `candidates` (08-08) list
        plus a count, while the branch expected one job per message. Aliasing
        cannot fix that — every key was dropped and a single
        "Follow-up due for ? — 14+ days" page was emitted per SCAN instead of
        one per job.
        """
        batch: Any = inner.get("jobs")
        if not isinstance(batch, list):
            batch = inner.get("candidates")
        threshold = inner.get("threshold_days") or inner.get("cutoff_days")

        if not isinstance(batch, list):
            # A producer speaking neither key: keep the single-event behaviour
            # rather than going silent on a shape nobody has audited.
            payload = self._identified_payload(
                inner, ["company", "title", "job_key", "days_since_application"])
            _apply_followup_days(payload, inner, threshold)
            return [(EventType.FOLLOWUP_DUE, payload, None)]

        emissions = []
        for job in batch:
            if not isinstance(job, dict):
                continue
            payload = self._identified_payload(
                job, ["company", "title", "job_key", "days_since_application"])
            _apply_followup_days(payload, job, threshold)
            emissions.append((EventType.FOLLOWUP_DUE, payload, None))
        # An explicitly empty batch means nothing is due — do not page.
        return emissions

    def handle(self, event: Event) -> None:
        payload = event.payload or {}
        message_type = payload.get("message_type", "")
        inner = payload.get("inner_payload") or payload.get("payload") or {}
        correlation_id = event.correlation_id

        emissions = self._translate(message_type, inner, payload.get("from"))
        for et, out_payload, priority in emissions:
            if et in _SCORE_EVENT_TYPES and self._is_duplicate_score_emission(
                et, out_payload
            ):
                logger.info(
                    "MailboxTranslator: suppressed duplicate %s for %s (%s @ %s)",
                    et.type_string,
                    out_payload.get("job_id") or out_payload.get("job_key"),
                    out_payload.get("title"),
                    out_payload.get("company"),
                )
                continue
            try:
                self.bus.emit(
                    event_type=et,
                    source=f"mailbox:{payload.get('from', 'unknown')}",
                    payload=out_payload,
                    priority=priority,
                    correlation_id=correlation_id,
                    job_id=out_payload.get("job_key") or out_payload.get("job_id"),
                )
            except Exception:
                logger.exception("MailboxTranslator: failed to emit %s", et.type_string)

        # ERROR branch: feed the cluster detector so structured mailbox
        # error messages contribute to the same cluster signal as cron-
        # exit-code failures.  Source attribution = the failing agent
        # (inner.source_agent or the mailbox 'from'), NOT the bus-event
        # 'source' (which is the transport label).
        if message_type == "ERROR":
            self._record_error_for_clustering(payload, inner, correlation_id)

    def _is_duplicate_score_emission(
        self,
        et: EventType,
        payload: Dict[str, Any],
        now: Optional[float] = None,
    ) -> bool:
        """Record-and-decide (RepeatGuard shape): True → suppress.

        Identity is job_id/job_key when the message carries one, else
        company|title. Score is part of the key so a changed score is
        never suppressed — only literal repeats of the same fact are.
        The window slides on repeats (a fact that keeps arriving stays
        suppressed); a re-emission after the window is a fresh fact.
        """
        now = time.monotonic() if now is None else now
        ident = payload.get("job_id") or payload.get("job_key")
        if not ident:
            ident = f"{payload.get('company')}|{payload.get('title')}"
        key = (et.type_string, str(ident), str(payload.get("score")))
        seen = self._recent_score_emissions
        last = seen.get(key)
        duplicate = last is not None and (now - last) < SCORE_DEDUP_WINDOW_SECONDS
        seen[key] = now
        seen.move_to_end(key)
        while len(seen) > _SCORE_DEDUP_MAX_ENTRIES:
            seen.popitem(last=False)
        return duplicate

    def _record_error_for_clustering(
        self,
        outer_payload: Dict[str, Any],
        inner: Dict[str, Any],
        correlation_id: Optional[str],
    ) -> None:
        """Record an ERROR mailbox message into the FailureClusterDetector
        and emit AGENT_FAILURE_CLUSTER if the threshold is crossed.

        Wrapped in a broad try/except so a corrupt state file or detector
        bug never blocks AGENT_ERROR emission or the rest of the poll loop.
        """
        try:
            raw_source_agent = (
                inner.get("source_agent")
                or outer_payload.get("from")
                or "unknown"
            )
            # Canonicalise BEFORE recording so the per-source window state
            # is shared with the parallel cron-emitter path
            # (events/producers/cron_emitter.py).  Without this, an inner
            # payload reporting 'jobflow-applier' would key into a
            # separate window from the cron path's already-canonical
            # 'applier', defeating dedup.  See
            # profiles/critic/workspace/watchdog-dedup-proposal-2026-04-29.md.
            source_agent = canonical_agent_source(raw_source_agent)
            error_text = inner.get("message") or inner.get("error") or ""
            cluster = self._cluster_detector.record(
                source=source_agent,
                success=False,
                error_text=error_text,
                details=_failure_details(inner),
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
                    correlation_id=correlation_id,
                )
        except Exception:
            logger.exception(
                "MailboxTranslator: cluster detector record failed"
            )

    def _translate(
        self,
        message_type: str,
        inner: Dict[str, Any],
        from_agent: Optional[str] = None,
    ) -> List[Tuple[EventType, Dict[str, Any], Optional[Priority]]]:
        """Return a list of (event_type, payload, priority_override_or_None)."""
        results: List[Tuple[EventType, Dict[str, Any], Optional[Priority]]] = []

        if message_type == "SCORE_RESULT":
            p = _score_payload(inner)
            results.append((EventType.JOB_SCORED, p, None))
            if p.get("score", 0) >= HIGH_SCORE_THRESHOLD:
                results.append((EventType.JOB_HIGH_SCORE, p, None))

        elif message_type == "SCORE_BATCH_SUMMARY":
            # Real protocol field is `results`; tolerate `scored_jobs` as alias.
            batch = inner.get("results") or inner.get("scored_jobs", [])
            for job in batch:
                p = _score_payload(job)
                results.append((EventType.JOB_SCORED, p, None))
                if p.get("score", 0) >= HIGH_SCORE_THRESHOLD:
                    results.append((EventType.JOB_HIGH_SCORE, p, None))

        elif message_type == "SCOUT_DISCOVERY":
            for job in inner.get("jobs", []):
                p = _job_payload(job)
                results.append((EventType.JOB_DISCOVERED, p, None))

        elif message_type == "TAILOR_COMPLETE":
            results.append((EventType.TAILOR_COMPLETED, self._identified_payload(
                inner, ["company", "title", "job_key", "artifacts"]), None))

        # SUBMIT_REQUEST deliberately absent (2026-08-19). It is a COMMAND
        # main -> applier — `mode: dry_run`, `submission_authorized: false`,
        # an `applier-dry-run:` idempotency key (jobflow_dispatch/contracts.py
        # :50-53) — so translating it fired the HIGH ACT/ACTION_REQUIRED page
        # "Dry-run complete for X. Approve submission?" at the moment the dry
        # run was REQUESTED, before the applier had run anything. Naming the
        # job in that payload only made a false claim well-named. It also
        # booked the job as _PENDING (events/outcomes.py:77) — "waiting on
        # Diego" — when it was waiting on the applier. The message still
        # reaches the bus as `mailbox_message` for audit, and still wakes the
        # applier via the dispatch route, which is a separate table.
        elif message_type == "DRY_RUN_COMPLETE":
            results.append((EventType.APPLICATION_READY, self._identified_payload(
                inner, ["company", "title", "job_key", "artifacts"]), None))

        elif message_type == "SUBMIT_RESULT":
            results.extend(self._submit_result_emissions(inner))

        # SUBMIT_CONFIRM deliberately absent (2026-08-19). It is Diego's
        # AUTHORIZATION, not an outcome: "Dry-run -> Jaum review ->
        # SUBMIT_CONFIRM -> actual submit" (curator/orchestrator.py:151)
        # places it strictly BEFORE the applier acts, and protocol.md:49
        # gives its entire payload as `{job_id, approved: true}`. Mapping it
        # to APPLICATION_SUBMITTED -- a _SUCCESS_EVENT_TYPE
        # (events/outcomes.py:89) -- booked the job as a completed success
        # the instant Diego said yes, and the consequences outlive the
        # notification: memory_writer.py:186 appends "Application submitted
        # for <title> at <company>" to the job's GBrain timeline, which is
        # append-only, and digest_composer.py:371 counts it in the daily
        # "Applier: N submitted" line. The copied fields were a fiction too
        # -- none of company/title/job_key/submission_id is in that payload,
        # so the event landed as `{}`, and a submission_id cannot exist
        # before the submission does.
        #
        # Since SUBMIT_RESULT became the truthful producer of this event
        # type, keeping the branch made one authorized application emit
        # APPLICATION_SUBMITTED twice -- once falsely at the go-ahead, once
        # truthfully at the report.
        #
        # Dropped rather than repointed at an "authorization received" type:
        # Diego AUTHORS this message (SOUL.md:58 "approve X" / "yes"; he
        # clicks Approve -- profiles/applier/workspace/WORKFLOW.md:73), so
        # such an event would page him about his own click, and every other
        # event in this lane tells him something a machine learned. The
        # message still reaches the bus as `mailbox_message` for audit, with
        # its own `_summarize` case (mailbox_watcher.py:282), and the
        # dispatch route that wakes the applier is a separate table
        # (jobflow_dispatch/contracts.py:55).

        elif message_type == "BLOCKED_QUESTION":
            results.append(
                (EventType.APPLICATION_BLOCKED,
                 self._blocked_question_payload(inner), None))

        elif message_type == "PIPELINE_UPDATE":
            transition = _stage_transition_payload(inner, from_agent)
            prev = transition.get("prior_stage")
            new = transition.get("new_stage")
            # Emit on any real transition — including first assignment where prev is None.
            if new and new != prev:
                # Backfill title/company from pipeline state when the message
                # omits them (matcher batch updates carry only job_key + stage),
                # so the Telegram head is "<title> at <company>" not a bare UUID.
                if not transition.get("title") or not transition.get("company"):
                    meta = self._pipeline_metadata(
                        transition.get("job_id") or transition.get("job_key")
                    )
                    for k in ("title", "company"):
                        if not transition.get(k) and meta.get(k):
                            transition[k] = meta[k]
                results.append((EventType.STAGE_TRANSITION, transition, None))

        elif message_type == "FOLLOWUP_ALERT":
            results.extend(self._followup_emissions(inner))

        elif message_type == "VIP_DISCOVERY":
            p = _job_payload(inner)
            p.setdefault("source", "linkedin-saved")
            results.append((EventType.JOB_VIP_DISCOVERED, p, None))

        elif message_type == "HIGH_SCORE_ALERT":
            results.append((EventType.JOB_HIGH_SCORE, _score_payload(inner), None))

        elif message_type == "ERROR":
            results.append((EventType.AGENT_ERROR, _agent_error_payload(inner), None))

        elif message_type == "NOTIFICATION":
            body = str(inner.get("body", "")) + " " + str(inner.get("summary", ""))
            if any(p.search(body) for p in _INTERVIEW_PATTERNS):
                results.append((EventType.INTERVIEW_SIGNAL, _copy_fields(
                    inner, ["company", "title", "job_key", "body"]), None))
            elif any(p.search(body) for p in _OFFER_PATTERNS):
                results.append((EventType.OFFER_SIGNAL, _copy_fields(
                    inner, ["company", "title", "job_key", "body"]), None))

        return results


def _score_payload(d: Dict[str, Any]) -> Dict[str, Any]:
    # The matcher's score messages identify jobs by `job_id`, not `job_key`
    # (every score event on the live bus had job_key=None and an empty bus
    # job_id column). Carry both, backfilling job_key, so the emit path's
    # job_id column and the dedup guard have an identity to key on.
    job_id = d.get("job_id")
    return {
        "score": d.get("score", 0),
        "recommendation": d.get("recommendation"),
        "company": d.get("company"),
        "title": d.get("title"),
        "dimensions": d.get("dimensions"),
        "job_key": d.get("job_key") or job_id,
        "job_id": job_id,
    }


def _job_payload(d: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "company": d.get("company"),
        "title": d.get("title"),
        "source": d.get("source"),
        "url": d.get("url"),
        "job_key": d.get("job_key"),
    }


def _copy_fields(d: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
    return {f: d.get(f) for f in fields if d.get(f) is not None}


# Every real FOLLOWUP_ALERT on the bus counts days under a DIFFERENT key:
# days_since (2026-08-07), days_inactive (08-08), days_since_stage (08-09).
_FOLLOWUP_DAY_FIELDS = (
    "days_since_application",
    "days_since_stage",
    "days_since",
    "days_inactive",
    "days_since_last_contact",
    "days",
)


def _followup_days(d: Dict[str, Any]) -> Optional[float]:
    for field in _FOLLOWUP_DAY_FIELDS:
        value = d.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def _apply_followup_days(
    payload: Dict[str, Any], source: Dict[str, Any], threshold: Any
) -> Dict[str, Any]:
    """Populate `days` — the key BOTH renderers actually read.

    whatsapp_escalator.py:393 and digest_composer.py:312 read
    `payload["days"]`, NOT the `days_since_application` this branch's contract
    copied, so even a producer honouring that contract rendered the bare
    default 14. Carry both.
    """
    days = _followup_days(source)
    if days is None and isinstance(threshold, (int, float)) and not isinstance(
        threshold, bool
    ):
        days = threshold
    if days is not None:
        payload["days"] = days
        payload.setdefault("days_since_application", days)
    return payload


# Wording mirrors `blockedQuestionText` in jobflow-platform
# services/notifier-bridge/src/transport.ts and `blocked_question_text` in the
# applier sweep, so all three renderings of this event type read identically.
_BLOCKED_QUESTION_PROMPT = (
    "The ATS dry run needs answers for required application questions"
)
# MailboxWatcher._summarize truncates at 200 chars; cut it here so the ellipsis
# lands on a boundary we chose.
_BLOCKED_QUESTION_MAX_CHARS = 200


def _question_labels(value: Any) -> List[str]:
    """Human labels of the fields the ATS adapter could not answer.

    Real payloads are a list of {label,type,selector} dicts, but bare strings
    are tolerated because the browser-worker adapters are not uniform about it.
    """
    if not isinstance(value, list):
        return []
    labels: List[str] = []
    for entry in value:
        if isinstance(entry, str):
            label = entry.strip()
        elif isinstance(entry, dict):
            label = next(
                (str(entry[k]).strip()
                 for k in ("label", "question", "name") if entry.get(k)),
                "",
            )
        else:
            label = ""
        if label:
            labels.append(label)
    return labels


def _blocked_question_options(inner: Dict[str, Any]) -> Optional[List[str]]:
    """The tenant's own listbox labels for the blocked question.

    A Workday listbox answer is matched against the tenant's OWN option text,
    so a human answer that is not verbatim one of these labels is never
    clicked -- the run re-stalls looking exactly like nobody answered. The
    applier emits them under `options`
    (profiles/applier/workspace/tmp_ready_sweep_cron.py `question_options`,
    2026-08-20) and protocol.md has always documented that key, but a key this
    translator does not copy is a key Diego never sees, so until now the only
    way through was to inline the labels in `question` itself -- where
    MailboxWatcher._summarize caps at 200 chars and the real Capital One list
    measured exactly 200 with no margin.

    The producer's flat list wins, including an explicitly empty list. The
    per-question list in `questions[i].options` is a fallback for the
    notifier-bridge envelope and for replaying an envelope written before the
    flat key existed. On a one-entry array that entry is necessarily focused.
    On a multi-entry array, only the unique entry named by top-level `question`
    may supply labels; an unmatched or ambiguous focus stays absent rather than
    borrowing another question's choices.

    The return value is deliberately three-state. ``None`` means no option
    popup was observed; ``[]`` means the focused popup was observed and offered
    nothing; a non-empty list contains that focused popup's exact labels.
    """
    def _labels(value: Any) -> List[str]:
        if not isinstance(value, (list, tuple)):
            return []
        out: List[str] = []
        for option in value:
            label = str(option).strip()
            if label and label not in out:
                out.append(label)
        return out

    if "options" in inner:
        value = inner.get("options")
        # A malformed top-level key is not authoritative. Salvage a valid
        # per-question capture if one exists; otherwise this remains absent.
        if isinstance(value, (list, tuple)):
            return _labels(value)

    questions = inner.get("questions") or inner.get("unansweredQuestions")
    if not isinstance(questions, list):
        return None
    entries = [entry for entry in questions if isinstance(entry, dict)]
    if len(entries) == 1:
        focused = entries[0]
    else:
        question = inner.get("question")
        if not isinstance(question, str) or not question.strip():
            return None
        focus = question.strip()
        matches = [
            entry for entry in entries
            if any(
                isinstance(entry.get(key), str)
                and entry[key].strip() == focus
                for key in ("label", "question", "name")
            )
        ]
        if len(matches) != 1:
            return None
        focused = matches[0]

    if "options" not in focused:
        return None
    value = focused.get("options")
    if not isinstance(value, (list, tuple)):
        return None
    return _labels(value)


def _blocked_question_text(inner: Dict[str, Any]) -> str:
    """The `question` text Diego reads on the CRITICAL page.

    Never empty: the escalator's own `payload.get("question", ...)` default is
    the "needs your input" placeholder this exists to eliminate.
    """
    detail = "; ".join(
        _question_labels(inner.get("questions") or inner.get("unansweredQuestions"))
    )
    if not detail:
        detail = str(inner.get("failureMessage") or "").strip()
    text = (
        f"{_BLOCKED_QUESTION_PROMPT}: {detail}"
        if detail
        else f"{_BLOCKED_QUESTION_PROMPT}."
    )
    if len(text) > _BLOCKED_QUESTION_MAX_CHARS:
        text = text[: _BLOCKED_QUESTION_MAX_CHARS - 1] + "…"
    return text
