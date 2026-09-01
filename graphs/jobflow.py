"""JobFlow LangGraph (Phase B of ADR-0020).

Stage-1 graph (default — invoke()):
    load_profile -> match_score -> route_decision -> END

Stage-3 graph (invoke_full() / --full):
    load_profile -> match_score -> route_decision
                                       |
                      ┌────────────────┼────────────────┐
                      ▼                ▼                ▼
                  tailor_node      (review)         (archive)
                      │                │                │
                      ▼                │                │
                  approval_hitl        │                │
                      │                │                │
                      ▼                │                │
                  apply_node           │                │
                      │                │                │
                      ▼                ▼                ▼
                            tracker_update -> END

Checkpointing: SqliteSaver at ~/.hermes/graphs/checkpoints.db (full graph only).
Enables resume after HITL interrupts + replay for debugging.

Each node is span-wrapped via obs.get_tracer() so the entire run shows up in
Langfuse as a parent trace with child spans per node.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import List, Literal, Optional, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from hermes_constants import get_default_hermes_root
from obs import get_tracer
from obs.oauth_llm import codex_structured_invoke

from ._profile import load_profile_summary
from ._prompts import (
    MATCHER_SYSTEM_PROMPT,
    MATCHER_USER_TEMPLATE,
    TAILOR_SYSTEM_PROMPT,
    TAILOR_USER_TEMPLATE,
)


# ---------------------------------------------------------------------------
# Event-bus emit helper (Phase B iter2). Defensive — graphs can also run in
# environments without the events module on path.
# ---------------------------------------------------------------------------

def _emit_event(event_type_str: str, source: str, payload: dict, priority: Optional[str] = None) -> Optional[str]:
    """Emit an event to the Hermes event bus. Returns event_id or None on failure.

    The graph runs as a subprocess; the bus is just a SQLite file at
    ~/.hermes/events/event_bus.db so concurrent writers are fine.
    """
    try:
        from events.bus import EventBus
        from events.schema import EventType, Priority

        et = EventType.from_string(event_type_str)
        if et is None:
            return None
        bus = EventBus()
        prio = Priority.from_string(priority) if priority else None
        return bus.emit(event_type=et, source=source, payload=payload, priority=prio)
    except Exception:
        return None

_TRACER = get_tracer("hermes.jobflow")
logger = logging.getLogger(__name__)

# Diego mandate 2026-04-24: ONLY gpt-5.5 via OAuth across the whole platform.
# obs.oauth_llm.get_codex_chat_model() bridges langchain to chatgpt.com/backend-api/codex
# (Responses API). Per-graph override via HERMES_JOBFLOW_MODEL.
DEFAULT_MODEL = os.environ.get("HERMES_JOBFLOW_MODEL", "gpt-5.5")
# On-disk locations. FUNCTIONS, not module constants, and resolved through
# get_default_hermes_root() rather than Path.home() -- both halves are load-bearing:
#
#   * Path.home() BYPASSES the HERMES_HOME redirect tests/conftest.py installs, so
#     anything that reached these writes landed in the developer's real
#     ~/.hermes/graphs -- which holds live job-application records.
#   * A module-level constant is resolved at IMPORT time, and conftest sets
#     HERMES_HOME in an autouse fixture that runs AFTER import. So the correct
#     resolver in a constant would still have snapshotted the wrong root.
#
# get_default_hermes_root() and NOT get_hermes_home(): these files live at the
# ROOT (~/.hermes/graphs), while get_hermes_home() returns <root>/profiles/<name>
# under a profile-scoped HERMES_HOME -- using it would silently relocate live data
# into the profile dir. Same root-vs-profile rule as the notification layer.
# Production paths are unchanged; only tests move. Pinned by
# tests/graphs/test_jobflow_paths.py.


def _graphs_dir() -> Path:
    return get_default_hermes_root() / "graphs"


def checkpoint_db_path() -> Path:
    return _graphs_dir() / "checkpoints.db"


def approval_log_path() -> Path:
    return _graphs_dir() / "approval-log.jsonl"


def apply_log_path() -> Path:
    return _graphs_dir() / "apply-log.jsonl"


def tracker_log_path() -> Path:
    return _graphs_dir() / "tracker-log.jsonl"


def main_inbox_path() -> Path:
    """The mailbox the APPLY_PACKET is dropped into."""
    return get_default_hermes_root() / "mailbox" / "main" / "inbox"


# ---------------------------------------------------------------------------
# Pydantic score schema (LLM structured output target)
# ---------------------------------------------------------------------------


class ScoreBreakdown(BaseModel):
    title_match: float = Field(description="0-10; seniority/role shape fit", ge=0, le=10)
    skills_overlap: float = Field(description="0-10; technical+domain coverage", ge=0, le=10)
    industry_fit: float = Field(description="0-10; vs profile industries; avoid-list<=2", ge=0, le=10)
    location: float = Field(description="0-10; vs profile geography preference", ge=0, le=10)
    comp_alignment: float = Field(description="0-10; vs profile comp anchor", ge=0, le=10)
    growth: float = Field(description="0-10; trajectory + scope expansion", ge=0, le=10)
    culture: float = Field(description="0-10; team/values/impact signals", ge=0, le=10)


class MatcherScore(BaseModel):
    score: float = Field(description="Final weighted score 0-10 after penalties", ge=0, le=10)
    recommendation: Literal["PROCEED", "REVIEW", "ARCHIVE"]
    breakdown: ScoreBreakdown
    penalties_applied: List[str] = Field(default_factory=list)
    key_strengths: List[str] = Field(default_factory=list)
    gaps: List[str] = Field(default_factory=list)
    rationale: str = Field(description="2-3 sentence justification", default="")


class TailorDraft(BaseModel):
    cover_paragraph: str = Field(description="120-180 word cover-letter paragraph")
    resume_hints: List[str] = Field(description="2-3 bullet resume customization hints")
    primary_angle: str = Field(description="One phrase: strongest selling point for this role")


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------


class JobFlowState(TypedDict, total=False):
    """State passed between graph nodes.

    Fields are optional so nodes can write incrementally. LangGraph merges
    dict-returning node outputs into the running state.
    """

    # Input
    job: dict  # raw Scout-shaped JD
    job_id: str

    # After load_profile
    profile_summary: str

    # After match_score
    score: float
    recommendation: Literal["PROCEED", "REVIEW", "ARCHIVE"]
    breakdown: dict
    penalties_applied: List[str]
    key_strengths: List[str]
    gaps: List[str]
    rationale: str
    model_used: str

    # After route_decision
    decision: Literal["tailor", "review", "archive"]

    # After tailor_node (PROCEED path only)
    cover_paragraph: str
    resume_hints: List[str]
    primary_angle: str

    # After approval_hitl (PROCEED path only). Populated by Diego's WhatsApp reply
    # or auto-approved in headless test mode.
    approval: Literal["approved", "rejected", "pending"]
    approval_reason: str

    # After apply_node (only if approval=approved)
    applied: bool
    apply_receipt: dict  # {ats_url, submitted_at, dry_run, ...}

    # After tracker_update
    tracker_stage: str

    # Error path
    error: Optional[str]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def load_profile_node(state: JobFlowState) -> dict:
    """Populate profile_summary. Cheap, no LLM call. Independent of job."""
    with _TRACER.start_as_current_span("jobflow.load_profile") as span:
        summary = load_profile_summary()
        span.set_attribute("profile.chars", len(summary))
        return {"profile_summary": summary}


def match_score_node(state: JobFlowState) -> dict:
    """LLM-driven scoring against the 7-dim rubric. Structured output."""
    with _TRACER.start_as_current_span("jobflow.match_score") as span:
        job = state.get("job") or {}
        profile_summary = state.get("profile_summary") or load_profile_summary()

        user = MATCHER_USER_TEMPLATE.format(
            profile_summary=profile_summary,
            title=job.get("title", "(unknown)"),
            company=job.get("company", "(unknown)"),
            location=job.get("location", "(unknown)"),
            seniority_level=job.get("seniority_level", "(unknown)"),
            salary_range=job.get("salary_range") or "(not disclosed)",
            source_board=job.get("source_board", job.get("source", "unknown")),
            url=job.get("url") or job.get("source_url", ""),
            description=(job.get("description") or job.get("description_raw") or "")[:12000],
        )

        span.set_attribute("gen_ai.system", "openai")
        span.set_attribute("gen_ai.request.model", DEFAULT_MODEL)
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("job.id", str(state.get("job_id") or job.get("id") or ""))
        span.set_attribute("job.title", job.get("title", "")[:120])
        span.set_attribute("job.company", job.get("company", "")[:80])
        span.set_attribute("input.prompt_chars", len(MATCHER_SYSTEM_PROMPT) + len(user))

        try:
            # Direct Codex Responses API call with structured JSON output.
            # See obs/oauth_llm.py::codex_structured_invoke for shape details.
            result: MatcherScore = codex_structured_invoke(
                MatcherScore,
                instructions=MATCHER_SYSTEM_PROMPT,
                user=user,
                model=DEFAULT_MODEL,
                max_retries=2,
            )
        except Exception as exc:
            span.record_exception(exc)
            from opentelemetry.trace import Status, StatusCode
            span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            return {"error": f"match_score LLM failed: {exc}"}

        span.set_attribute("gen_ai.response.model", DEFAULT_MODEL)
        span.set_attribute("matcher.score", result.score)
        span.set_attribute("matcher.recommendation", result.recommendation)

        # iter-cross-surface: upsert job metadata into pipeline.json so the
        # dashboard + bridge + DevFlow know this job exists with its score.
        # If it's brand new, gets stage=discovered; if it already exists
        # (e.g. imported from Scout), only fills in missing fields + updates score.
        try:
            from pipeline_state import PipelineManager

            PipelineManager().upsert_metadata(
                job_id=str(state.get("job_id") or job.get("id") or ""),
                metadata={
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "location": job.get("location"),
                    "score": result.score,
                    "recommendation": result.recommendation,
                    "url": job.get("url") or job.get("source_url"),
                    "apply_url": job.get("apply_url"),
                    "source": job.get("source_board") or job.get("source"),
                },
                actor="langgraph",
                source="matcher",
            )
            span.set_attribute("pipeline_json.upserted", True)
        except Exception as exc:
            span.set_attribute("pipeline_json.upsert_error", str(exc)[:200])

        # Capture prompt + completion as span events for Langfuse's LLM UI.
        # Using span events keeps the large payloads out of the attribute table
        # but still makes them queryable inside the Langfuse UI.
        try:
            span.add_event(
                "gen_ai.content.prompt",
                {"gen_ai.prompt": (user[-8000:]) if len(user) > 8000 else user},
            )
            span.add_event(
                "gen_ai.content.completion",
                {"gen_ai.completion": result.model_dump_json()},
            )
        except Exception:
            pass

        return {
            "score": result.score,
            "recommendation": result.recommendation,
            "breakdown": result.breakdown.model_dump(),
            "penalties_applied": result.penalties_applied,
            "key_strengths": result.key_strengths,
            "gaps": result.gaps,
            "rationale": result.rationale,
            "model_used": DEFAULT_MODEL,
        }


def route_decision_node(state: JobFlowState) -> dict:
    """Deterministic branch based on score threshold.

    PROCEED  >= proceed_threshold (default 8.75) -> tailor
    REVIEW   >= review_threshold  (default 5.0)  -> review
    ARCHIVE  < review_threshold                  -> archive

    Thresholds are tunable via env for calibration experiments and Critic
    auto-apply (allowed_knobs.json includes these):
        HERMES_JOBFLOW_PROCEED_THRESHOLD  (default 8.75)
        HERMES_JOBFLOW_REVIEW_THRESHOLD   (default 5.0)
    """
    with _TRACER.start_as_current_span("jobflow.route_decision") as span:
        score = float(state.get("score") or 0.0)
        proceed = float(os.environ.get("HERMES_JOBFLOW_PROCEED_THRESHOLD", "8.75"))
        review = float(os.environ.get("HERMES_JOBFLOW_REVIEW_THRESHOLD", "5.0"))
        span.set_attribute("threshold.proceed", proceed)
        span.set_attribute("threshold.review", review)
        if state.get("error"):
            decision = "review"  # fail-safe: always surface errors to Diego
        elif score >= proceed:
            decision = "tailor"
        elif score >= review:
            decision = "review"
        else:
            decision = "archive"
        span.set_attribute("decision", decision)
        span.set_attribute("score", score)
        return {"decision": decision}


# ---------------------------------------------------------------------------
# Stage-3 nodes: tailor / approval_hitl / apply / tracker_update
# ---------------------------------------------------------------------------


def tailor_node(state: JobFlowState) -> dict:
    """LLM-driven cover-letter paragraph + resume hints for PROCEED jobs."""
    with _TRACER.start_as_current_span("jobflow.tailor") as span:
        job = state.get("job") or {}
        profile_summary = state.get("profile_summary") or load_profile_summary()

        user = TAILOR_USER_TEMPLATE.format(
            profile_summary=profile_summary,
            score=state.get("score"),
            recommendation=state.get("recommendation"),
            strengths="; ".join(state.get("key_strengths") or []),
            gaps="; ".join(state.get("gaps") or []),
            rationale=state.get("rationale") or "",
            title=job.get("title", ""),
            company=job.get("company", ""),
            location=job.get("location", ""),
            description=(job.get("description") or job.get("description_raw") or "")[:10000],
        )

        span.set_attribute("gen_ai.system", "openai")
        span.set_attribute("gen_ai.request.model", DEFAULT_MODEL)
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("job.id", str(state.get("job_id") or job.get("id") or ""))
        span.set_attribute("job.company", job.get("company", "")[:80])

        try:
            draft: TailorDraft = codex_structured_invoke(
                TailorDraft,
                instructions=TAILOR_SYSTEM_PROMPT,
                user=user,
                model=DEFAULT_MODEL,
                max_retries=2,
            )
        except Exception as exc:
            span.record_exception(exc)
            from opentelemetry.trace import Status, StatusCode
            span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            return {"error": f"tailor_node LLM failed: {exc}"}

        span.set_attribute("tailor.cover_chars", len(draft.cover_paragraph))
        span.set_attribute("tailor.resume_hints_count", len(draft.resume_hints))
        span.add_event(
            "gen_ai.content.completion",
            {"gen_ai.completion": draft.model_dump_json()},
        )

        return {
            "cover_paragraph": draft.cover_paragraph,
            "resume_hints": list(draft.resume_hints),
            "primary_angle": draft.primary_angle,
        }


def approval_hitl_node(state: JobFlowState) -> dict:
    """Human-in-the-loop gate.

    iter2: REAL LangGraph interrupt. The node:
      1. Logs the request to approval-log.jsonl (audit trail)
      2. Emits APPROVAL_REQUEST event to the bus (-> Telegram tailor_applier
         topic + WhatsApp URGENT tier per TOPIC_ROUTING + _TIER_BY_EVENT)
      3. Calls interrupt() — graph PAUSES and the caller's invoke() returns
         with __interrupt__ in result. SqliteSaver persists state by thread_id.

    Resume:
      bin/jobflow_approve.py --thread-id <tid> --approved [--reason ...]
        -> graph.invoke(Command(resume={"approval": "approved", ...}), config={...})
      The Command(resume=value) is returned in place of the interrupt() call.

    Headless test mode: HERMES_JOBFLOW_AUTO_APPROVE=1 -> skips interrupt and
    returns approved synchronously, so smoke tests don't hang.
    """
    with _TRACER.start_as_current_span("jobflow.approval_hitl") as span:
        job = state.get("job") or {}
        jid = state.get("job_id") or job.get("id") or "unknown"
        auto = os.environ.get("HERMES_JOBFLOW_AUTO_APPROVE") == "1"

        req = {
            "event": "approval_requested",
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "job_id": jid,
            "job_title": job.get("title"),
            "job_company": job.get("company"),
            "score": state.get("score"),
            "primary_angle": state.get("primary_angle"),
            "cover_preview": (state.get("cover_paragraph") or "")[:200],
            "auto_resolved": auto,
        }
        approval_log = approval_log_path()
        approval_log.parent.mkdir(parents=True, exist_ok=True)
        with open(approval_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(req, default=str) + "\n")

        if auto:
            span.set_attribute("approval.mode", "auto")
            span.set_attribute("approval.result", "approved")
            return {
                "approval": "approved",
                "approval_reason": "HERMES_JOBFLOW_AUTO_APPROVE=1 (headless test mode)",
            }

        # iter2: emit APPROVAL_REQUEST event so notifier + WhatsApp escalator pick it up.
        event_id = _emit_event(
            "approval_request",
            source="jobflow.graph",
            payload={
                "job_id": jid,
                "job_title": job.get("title"),
                "job_company": job.get("company"),
                "score": state.get("score"),
                "primary_angle": state.get("primary_angle"),
                "cover_paragraph": state.get("cover_paragraph"),
                "resume_hints": state.get("resume_hints"),
                "apply_url": job.get("apply_url") or job.get("ats_url"),
                "approve_cli": (
                    f"python ~/.hermes/bin/jobflow_approve.py "
                    f"--thread-id job-{jid} --approved [--reason ...]"
                ),
            },
        )
        span.set_attribute("approval.mode", "interrupt")
        span.set_attribute("approval.event_id", event_id or "none")

        # interrupt() pauses the graph. The value passed here surfaces as
        # state["__interrupt__"] when the caller's invoke() returns.
        # When Diego runs the approve CLI, Command(resume={...}) provides a
        # dict that returns from the interrupt() call here.
        resume_payload = interrupt(
            {
                "kind": "approval_request",
                "job_id": jid,
                "job_title": job.get("title"),
                "job_company": job.get("company"),
                "score": state.get("score"),
                "approve_with": (
                    f"python ~/.hermes/bin/jobflow_approve.py "
                    f"--thread-id job-{jid} --approved [--reason ...]"
                ),
                "reject_with": (
                    f"python ~/.hermes/bin/jobflow_approve.py "
                    f"--thread-id job-{jid} --rejected [--reason ...]"
                ),
            }
        )

        # Resume payload arrives here. Expected shape:
        # {"approval": "approved"|"rejected", "approval_reason": "..."}
        if isinstance(resume_payload, dict):
            approval = resume_payload.get("approval", "rejected")
            reason = resume_payload.get("approval_reason") or ""
        elif isinstance(resume_payload, str):
            # Tolerate bare-string resume for human convenience.
            approval = "approved" if resume_payload.lower().startswith("appr") else "rejected"
            reason = resume_payload
        else:
            approval = "rejected"
            reason = "unparseable resume payload"

        span.set_attribute("approval.result", approval)
        return {"approval": approval, "approval_reason": reason}


def apply_node(state: JobFlowState) -> dict:
    """Dry-run applier. NEVER submits. Builds an APPLY_PACKET with everything
    Diego needs to do the final manual click on the ATS portal:
      - cover-letter paragraph
      - resume customization hints
      - primary-angle hook
      - direct ATS apply URL

    iter2 changes:
      * Writes the packet as a MAILBOX_MESSAGE to mailbox/main/inbox/ so it
        threads alongside other tracker outputs.
      * Emits an APPLY_PACKET event to the bus -> Telegram tailor_applier
        topic + WhatsApp IMPORTANT tier (Diego sees a digest entry, not a
        ping-storm).
      * Logs to apply-log.jsonl for audit.

    Live submission stays explicitly manual: dry_run=True ALWAYS in the graph;
    real submit lives behind a separate operator-driven script that consumes
    APPLY_PACKET events. This keeps the graph from ever auto-submitting.
    """
    with _TRACER.start_as_current_span("jobflow.apply") as span:
        job = state.get("job") or {}
        jid = state.get("job_id") or job.get("id") or "unknown"

        approved = state.get("approval") == "approved"
        span.set_attribute("approval.result", str(state.get("approval") or ""))

        receipt: dict = {
            "job_id": jid,
            "title": job.get("title"),
            "company": job.get("company"),
            "ats_url": job.get("ats_url"),
            "apply_url": job.get("apply_url"),
            "ats_platform": job.get("ats_platform"),
            "submitted_at": None,
            "dry_run": True,
            "would_submit": approved,
            "cover_paragraph": state.get("cover_paragraph"),
            "resume_hints": state.get("resume_hints"),
            "primary_angle": state.get("primary_angle"),
            "logged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        apply_log = apply_log_path()
        apply_log.parent.mkdir(parents=True, exist_ok=True)
        with open(apply_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(receipt, default=str) + "\n")

        # iter2: write APPLY_PACKET to mailbox/main/inbox + emit event.
        if approved:
            try:
                main_inbox = main_inbox_path()
                main_inbox.mkdir(parents=True, exist_ok=True)
                ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                msg = {
                    "type": "APPLY_PACKET",
                    "from": "jobflow.graph",
                    "to": "main",
                    "job_id": jid,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "correlation_id": str(uuid.uuid4()),
                    "payload": receipt,
                }
                packet_path = main_inbox / f"{ts}_{jid}_APPLY_PACKET_jobflow.json"
                packet_path.write_text(json.dumps(msg, indent=2, default=str), encoding="utf-8")
                event_id = _emit_event(
                    "apply_packet",
                    source="jobflow.graph",
                    payload={
                        "job_id": jid,
                        "title": job.get("title"),
                        "company": job.get("company"),
                        "apply_url": job.get("apply_url") or job.get("ats_url"),
                        "ats_platform": job.get("ats_platform"),
                        "cover_paragraph": state.get("cover_paragraph"),
                        "primary_angle": state.get("primary_angle"),
                        "packet_path": str(packet_path),
                    },
                )
                span.set_attribute("apply.packet_path", str(packet_path))
                span.set_attribute("apply.event_id", event_id or "none")
            except Exception as exc:
                span.record_exception(exc)
                span.set_attribute("apply.emit_error", str(exc)[:200])

        span.set_attribute("apply.dry_run", True)
        span.set_attribute("apply.would_submit", approved)

        return {"applied": approved, "apply_receipt": receipt}


def tracker_update_node(state: JobFlowState) -> dict:
    """Update pipeline state.

    iter2: emits a STAGE_TRANSITION event to the bus. The hermes_to_devflow.py
    bridge (cron `*/15 *`) consumes it and projects into the DevFlow Postgres
    workflow_runs table, surfacing the run in Mission Control. Also still
    writes the local tracker-log.jsonl for audit.

    2026-04-25 cross-surface unification: ALSO writes to pipeline.json via
    `pipeline_state.PipelineManager.update_stage()` so the :3001 Pipeline
    Dashboard + bridge→DevFlow Postgres + any Telegram digest see the same
    state transition that landed in tracker-log.jsonl.
    """
    with _TRACER.start_as_current_span("jobflow.tracker_update") as span:
        job = state.get("job") or {}
        jid = state.get("job_id") or job.get("id") or "unknown"
        decision = state.get("decision") or "review"
        approval = state.get("approval") or "n/a"
        applied = bool(state.get("applied"))

        # Map to canonical pipeline stage
        if applied:
            stage = "submitted"
        elif decision == "tailor" and approval == "approved":
            stage = "ready_to_submit"
        elif decision == "tailor" and approval == "pending":
            stage = "awaiting_approval"
        elif decision == "tailor" and approval == "rejected":
            stage = "rejected_by_user"
        elif decision == "review":
            stage = "review"
        else:
            stage = "archived"

        event = {
            "event": "pipeline_stage_transition",
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "job_id": jid,
            "job_title": job.get("title"),
            "job_company": job.get("company"),
            "new_stage": stage,
            "score": state.get("score"),
            "recommendation": state.get("recommendation"),
            "decision": decision,
            "approval": approval,
            "applied": applied,
        }
        tracker_log = tracker_log_path()
        tracker_log.parent.mkdir(parents=True, exist_ok=True)
        with open(tracker_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")

        # iter2: emit STAGE_TRANSITION event to event bus. The DevFlow bridge
        # picks this up via the standard subscriber poll loop. TOPIC_ROUTING
        # already maps "stage_transition" -> "tracker" Telegram topic.
        event_id = _emit_event(
            "stage_transition",
            source="jobflow.graph",
            payload={
                "job_id": jid,
                "job_title": job.get("title"),
                "job_company": job.get("company"),
                "new_stage": stage,
                "score": state.get("score"),
                "recommendation": state.get("recommendation"),
                "decision": decision,
                "approval": approval,
                "applied": applied,
                "graph_thread_id": state.get("job_id"),
            },
        )
        span.set_attribute("tracker.stage", stage)
        span.set_attribute("tracker.event_id", event_id or "none")

        # iter-cross-surface: write to pipeline.json (canonical source of truth
        # for Dashboard :3001 + bridge→DevFlow + agents). Defensive import so
        # the graph still runs in test environments without pipeline_state on
        # path.
        try:
            from pipeline_state import PipelineManager

            mgr = PipelineManager()
            actor = "langgraph"
            source = "langgraph"
            # When Diego approved via Control Center, the approval_reason field
            # captures that; surface it as the notes so pipeline.json history
            # shows his actual note.
            reason = state.get("approval_reason") or ""
            note_parts = [f"decision={decision}", f"approval={approval}"]
            if reason:
                note_parts.append(f"reason={reason}")
            if state.get("score"):
                note_parts.append(f"score={state.get('score')}")
            mgr.update_stage(
                job_id=jid,
                new_stage=stage,
                actor=actor,
                source=source,
                notes="; ".join(note_parts),
                metadata={
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "location": job.get("location"),
                    "score": state.get("score"),
                    "url": job.get("url") or job.get("source_url"),
                    "apply_url": job.get("apply_url"),
                    "source": job.get("source_board") or job.get("source"),
                    "recommendation": state.get("recommendation"),
                },
            )
            span.set_attribute("pipeline_json.updated", True)
        except Exception as exc:
            span.set_attribute("pipeline_json.error", str(exc)[:200])
            logger.warning("tracker_update_node: pipeline.json write failed: %s", exc)

        return {"tracker_stage": stage}


# ---------------------------------------------------------------------------
# Conditional router
# ---------------------------------------------------------------------------


def _after_route_decision(state: JobFlowState) -> str:
    """Map decision -> next node name."""
    d = state.get("decision") or "review"
    if d == "tailor":
        return "tailor"
    return "tracker_update"  # review + archive both just update state + END


def _after_approval(state: JobFlowState) -> str:
    """Only apply if explicitly approved; otherwise skip to tracker_update."""
    if state.get("approval") == "approved":
        return "apply"
    return "tracker_update"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_jobflow_graph():
    """Stage-1 graph (Matcher-only). Stateless. No checkpointing."""
    g = StateGraph(JobFlowState)
    g.add_node("load_profile", load_profile_node)
    g.add_node("match_score", match_score_node)
    g.add_node("route_decision", route_decision_node)

    g.set_entry_point("load_profile")
    g.add_edge("load_profile", "match_score")
    g.add_edge("match_score", "route_decision")
    g.add_edge("route_decision", END)

    return g.compile()


def build_full_graph(use_checkpointer: bool = True):
    """Stage-3 graph (Matcher + Tailor + HITL + Apply + Tracker). Checkpointed.

    Flow:
        load_profile -> match_score -> route_decision
                                          |
                          tailor -----------+------ tracker_update (review/archive)
                          |
                          approval_hitl
                          |
                          apply (if approved) -> tracker_update
                          |
                          tracker_update (if not approved, skip apply)
    """
    g = StateGraph(JobFlowState)
    g.add_node("load_profile", load_profile_node)
    g.add_node("match_score", match_score_node)
    g.add_node("route_decision", route_decision_node)
    g.add_node("tailor", tailor_node)
    g.add_node("approval_hitl", approval_hitl_node)
    g.add_node("apply", apply_node)
    g.add_node("tracker_update", tracker_update_node)

    g.set_entry_point("load_profile")
    g.add_edge("load_profile", "match_score")
    g.add_edge("match_score", "route_decision")
    # Conditional: tailor if PROCEED, else straight to tracker_update
    g.add_conditional_edges(
        "route_decision",
        _after_route_decision,
        {"tailor": "tailor", "tracker_update": "tracker_update"},
    )
    g.add_edge("tailor", "approval_hitl")
    # Conditional: apply if approved, else skip to tracker_update
    g.add_conditional_edges(
        "approval_hitl",
        _after_approval,
        {"apply": "apply", "tracker_update": "tracker_update"},
    )
    g.add_edge("apply", "tracker_update")
    g.add_edge("tracker_update", END)

    if use_checkpointer:
        # Defensive import: SqliteSaver ships with langgraph-checkpoint-sqlite.
        # If the ext package isn't installed, fall back to no checkpointer
        # rather than breaking the graph build.
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver

            checkpoint_db = checkpoint_db_path()
            checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
            # SqliteSaver.from_conn_string returns a CM; for library-use we want a
            # plain object with __call__-able methods. The v1.x API:
            #   with SqliteSaver.from_conn_string(":memory:") as saver: ...
            # For a persistent file, we manage the connection ourselves.
            import sqlite3

            conn = sqlite3.connect(str(checkpoint_db), check_same_thread=False)
            saver = SqliteSaver(conn)
            return g.compile(checkpointer=saver)
        except Exception:  # pragma: no cover
            return g.compile()

    return g.compile()


# ---------------------------------------------------------------------------
# Convenience runner
# ---------------------------------------------------------------------------


def invoke(job: dict, job_id: Optional[str] = None) -> JobFlowState:
    """Stage-1 entry point: Matcher-only graph. Stateless.

    Wraps the whole run in a parent span so Langfuse groups the child node
    spans under a single trace.
    """
    jid = job_id or job.get("id") or job.get("url") or "unknown"
    with _TRACER.start_as_current_span(f"jobflow.run:{jid}") as parent:
        parent.set_attribute("job.id", str(jid))
        parent.set_attribute("job.title", (job.get("title") or "")[:120])
        parent.set_attribute("job.company", (job.get("company") or "")[:80])
        parent.set_attribute("graph.variant", "matcher-only")
        graph = build_jobflow_graph()
        initial: JobFlowState = {"job": job, "job_id": jid}
        result = graph.invoke(initial)
        parent.set_attribute("final.score", float(result.get("score") or 0.0))
        parent.set_attribute("final.decision", str(result.get("decision") or ""))
        return result  # type: ignore[return-value]


def invoke_full(
    job: dict,
    job_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> JobFlowState:
    """Stage-3 entry point: full graph with Tailor/HITL/Apply/Tracker + checkpointing.

    Returns either:
      * Final JobFlowState if the run completed (no HITL pause needed, e.g.
        decision=review/archive, OR HERMES_JOBFLOW_AUTO_APPROVE=1).
      * A state dict with __interrupt__ key if the graph paused at HITL. The
        thread_id is in state['_thread_id'] so the resume CLI can find it.
    """
    jid = job_id or job.get("id") or job.get("url") or "unknown"
    tid = thread_id or f"job-{jid}"
    with _TRACER.start_as_current_span(f"jobflow.run_full:{jid}") as parent:
        parent.set_attribute("job.id", str(jid))
        parent.set_attribute("job.title", (job.get("title") or "")[:120])
        parent.set_attribute("job.company", (job.get("company") or "")[:80])
        parent.set_attribute("graph.variant", "full")
        parent.set_attribute("thread.id", tid)
        graph = build_full_graph(use_checkpointer=True)
        initial: JobFlowState = {"job": job, "job_id": jid}
        config = {"configurable": {"thread_id": tid}}
        result = graph.invoke(initial, config=config)
        # Stamp thread_id so callers can resume even if state is opaque.
        if isinstance(result, dict):
            result["_thread_id"] = tid
        parent.set_attribute("final.score", float(result.get("score") or 0.0))
        parent.set_attribute("final.decision", str(result.get("decision") or ""))
        parent.set_attribute("final.approval", str(result.get("approval") or ""))
        parent.set_attribute("final.applied", bool(result.get("applied")))
        parent.set_attribute("final.tracker_stage", str(result.get("tracker_stage") or ""))
        parent.set_attribute("final.interrupted", "__interrupt__" in result)
        return result  # type: ignore[return-value]


def resume_full(thread_id: str, resume_payload: dict) -> JobFlowState:
    """Resume a paused full graph run by feeding the resume_payload into the
    interrupted node. Uses the SqliteSaver-backed thread_id to restore state.
    """
    with _TRACER.start_as_current_span(f"jobflow.resume_full:{thread_id}") as parent:
        parent.set_attribute("thread.id", thread_id)
        parent.set_attribute("resume.approval", str(resume_payload.get("approval") or ""))
        graph = build_full_graph(use_checkpointer=True)
        config = {"configurable": {"thread_id": thread_id}}
        result = graph.invoke(Command(resume=resume_payload), config=config)
        if isinstance(result, dict):
            result["_thread_id"] = thread_id
        parent.set_attribute("final.score", float(result.get("score") or 0.0))
        parent.set_attribute("final.decision", str(result.get("decision") or ""))
        parent.set_attribute("final.approval", str(result.get("approval") or ""))
        parent.set_attribute("final.applied", bool(result.get("applied")))
        parent.set_attribute("final.tracker_stage", str(result.get("tracker_stage") or ""))
        return result  # type: ignore[return-value]
