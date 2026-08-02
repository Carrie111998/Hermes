"""Critic LangGraph (Phase C of ADR-0020).

Subgraph that consumes JobFlow calibration data + the Langfuse evaluation
dataset, runs LLM-driven drift detection, generates proposals, classifies
each proposal against allowed_knobs.json, auto-applies the safe ones, and
emits the rest as PROPOSAL messages to mailbox/main/inbox + WhatsApp queue.

Flow:
  load_calibration -> detect_drift -> generate_proposals -> classify_proposals
                                                                   |
                          ┌────────────────────────────────────────┘
                          ▼
                   auto_apply_node -> emit_proposals_node -> finalize_node -> END

Phase C v1 scope:
  * Auto-apply: ONLY items that map to existing allowed_knobs.json categories.
    Threshold tweaks (PROCEED/REVIEW) currently DO NOT match any knob, so they
    land as propose_only until Diego adds an explicit knob. Skill ranking + Matcher
    temperature (under reasoning_effort umbrella) ARE auto-applicable.
  * Reflexion replay (re-score with proposed change to verify) deferred to iter2.
  * WhatsApp routing of propose-only items: writes to mailbox/main/inbox +
    appends to whatsapp_queue.jsonl. Real WA send happens via existing notifier.
"""

from __future__ import annotations

import glob
import json
import os
import time
import uuid
from pathlib import Path
from typing import List, Literal, Optional, TypedDict

from obs.oauth_llm import codex_structured_invoke
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from obs import get_tracer

from ._critic_prompts import (
    CRITIC_DRIFT_SYSTEM_PROMPT,
    CRITIC_DRIFT_USER_TEMPLATE,
    CRITIC_PROPOSAL_SYSTEM_PROMPT,
    CRITIC_PROPOSAL_USER_TEMPLATE,
    CRITIC_RESOLVER_SYSTEM_PROMPT,
    CRITIC_RESOLVER_USER_TEMPLATE,
)


def _emit_event(event_type_str: str, source: str, payload: dict, priority: Optional[str] = None) -> Optional[str]:
    """Emit an event to the Hermes event bus. Defensive — Critic runs as a
    subprocess; the bus is just SQLite at ~/.hermes/events/event_bus.db.
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

_TRACER = get_tracer("hermes.critic")

# Diego mandate 2026-04-24: ONLY gpt-5.5 via OAuth across the whole platform.
DEFAULT_MODEL = os.environ.get("HERMES_CRITIC_MODEL", "gpt-5.5")
HERMES = Path.home() / ".hermes"
DIFF_REPORTS_DIR = HERMES / "profiles" / "matcher-shadow" / "workspace" / "diff-reports"
ALLOWED_KNOBS_PATH = HERMES / "profiles" / "critic" / "allowed_knobs.json"
CHANGELOG_PATH = HERMES / "profiles" / "critic" / "workspace" / "changelog.jsonl"
REVERSALS_DIR = HERMES / "profiles" / "critic" / "workspace" / "reversals"
RETROS_DIR = HERMES / "profiles" / "critic" / "workspace" / "retros"
WHATSAPP_QUEUE = HERMES / "profiles" / "critic" / "workspace" / "whatsapp_queue.jsonl"
PROPOSAL_MAILBOX = HERMES / "mailbox" / "main" / "inbox"


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class DriftCluster(BaseModel):
    pattern_name: str
    description: str
    evidence_job_ids: List[str] = Field(min_length=2)
    affected_dimensions: List[str] = Field(default_factory=list)
    direction: Literal["shadow_higher", "shadow_lower", "shadow_inconsistent"]
    mean_delta: float
    severity: Literal["high", "medium", "low"]
    hypothesized_root_cause: str


class DriftClusterList(BaseModel):
    clusters: List[DriftCluster] = Field(default_factory=list)


class Proposal(BaseModel):
    proposal_id: str
    cluster_pattern_name: str
    kind: Literal[
        "matcher.threshold_adjust",
        "matcher.dimension_weight",
        "matcher.prompt_edit",
        "matcher.temperature",
        "matcher.add_penalty",
        "agent.reasoning_effort",
        "cron.cadence",
        "skill.ranking",
        "structural",
    ]
    summary: str
    specific_change: str
    rationale: str
    expected_effect: str
    risk: Literal["low", "medium", "high"]


class ProposalList(BaseModel):
    proposals: List[Proposal] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class CriticState(TypedDict, total=False):
    # Inputs
    diff_report_window_days: int
    dataset_name: str

    # Calibration loader output
    paired_jobs: list
    dataset_items: list
    paired_count: int
    mean_abs_score_delta: float
    rec_agreement: int
    dim_stats: dict
    diff_reports_used: list

    # Drift detection output
    clusters: list  # serialized DriftCluster

    # Proposal generation
    proposals_raw: list  # serialized Proposal

    # Classification output
    proposals_classified: list  # each gets {classification: auto_apply|propose_only, allowed_knob_match}

    # Auto-apply output
    auto_applied: list  # {proposal_id, action_taken, reversal_script_path}
    auto_apply_errors: list

    # Emit output
    emitted: list  # {proposal_id, mailbox_path, whatsapp_queued}

    # iter5: contradiction resolver output
    contradiction_resolutions: list  # [{replaced: [...], with: [...]}]

    # Finalize
    retro_path: str
    run_id: str
    changelog_appended: bool  # False when the run had empty input (no entry written)

    error: Optional[str]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def _load_diff_reports(window_days: int) -> tuple[list, dict, list]:
    """Aggregate paired jobs from the most recent N days of diff reports.
    Returns (paired_jobs, summary_stats, files_used)."""
    if not DIFF_REPORTS_DIR.exists():
        return [], {}, []
    cutoff = time.time() - (window_days * 86400)
    files = sorted(
        [p for p in DIFF_REPORTS_DIR.glob("*.json") if p.stat().st_mtime >= cutoff],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    seen_jobs: dict[str, dict] = {}
    summary: dict = {}
    files_used: list[str] = []
    for fp in files:
        try:
            payload = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        files_used.append(str(fp.name))
        for pair in payload.get("pairs", []):
            jid = pair.get("job_id")
            if jid and jid not in seen_jobs:
                seen_jobs[jid] = pair
        if not summary and payload.get("summary"):
            summary = payload["summary"]
    return list(seen_jobs.values()), summary, files_used


def _load_dataset_items(name: str) -> list:
    try:
        from langfuse import Langfuse

        c = Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=os.environ["LANGFUSE_HOST"],
        )
        ds = c.get_dataset(name)
        out = []
        for it in ds.items:
            out.append(
                {
                    "id": it.id,
                    "input": it.input,
                    "expected_output": it.expected_output,
                    "metadata": it.metadata,
                }
            )
        return out
    except Exception:
        return []


def load_calibration_node(state: CriticState) -> dict:
    """Aggregate diff reports + Langfuse dataset into a calibration corpus."""
    with _TRACER.start_as_current_span("critic.load_calibration") as span:
        window = int(state.get("diff_report_window_days") or 7)
        ds_name = state.get("dataset_name") or "hermes-jobs-v1"

        pairs, summary, files = _load_diff_reports(window)
        dataset = _load_dataset_items(ds_name)

        span.set_attribute("calibration.window_days", window)
        span.set_attribute("calibration.diff_reports_count", len(files))
        span.set_attribute("calibration.paired_jobs", len(pairs))
        span.set_attribute("calibration.dataset_items", len(dataset))
        span.set_attribute("calibration.dataset_name", ds_name)

        return {
            "paired_jobs": pairs,
            "dataset_items": dataset,
            "paired_count": summary.get("paired_count", len(pairs)),
            "mean_abs_score_delta": summary.get("mean_abs_score_delta", 0.0),
            "rec_agreement": summary.get("recommendation_agreement", 0),
            "dim_stats": summary.get("dimension_stats") or {},
            "diff_reports_used": files,
        }


def detect_drift_node(state: CriticState) -> dict:
    """LLM-driven cluster detection over paired jobs."""
    with _TRACER.start_as_current_span("critic.detect_drift") as span:
        pairs = state.get("paired_jobs") or []
        if len(pairs) < 2:
            span.set_attribute("drift.skipped", "n<2")
            return {"clusters": []}

        # Build compact paired table (one line per job)
        lines = []
        for p in pairs[:50]:  # cap for token budget
            jid = p.get("job_id", "?")[:14]
            title = (p.get("title") or "")[:35]
            ps = p.get("prod_score")
            ss = p.get("shadow_score")
            d = p.get("score_delta")
            agrees = "yes" if p.get("rec_agrees") else "no"
            dd = p.get("dimension_deltas") or {}
            dd_short = {k: v for k, v in dd.items() if isinstance(v, (int, float)) and abs(v) >= 0.5}
            lines.append(
                f"  {jid} | {title:35s} | prod={ps} shadow={ss} delta={d} agrees={agrees} | dim_deltas_>=0.5={dd_short}"
            )
        paired_table = "\n".join(lines) or "(no paired jobs)"

        ds_lines = []
        for it in (state.get("dataset_items") or [])[:20]:
            eo = it.get("expected_output") or {}
            tier = eo.get("relevance_tier")
            score = eo.get("relevance_score")
            apr = eo.get("approve_for_tailor")
            ds_lines.append(f"  {it.get('id')[:18]}: expected_tier={tier} expected_score={score} approve={apr}")
        dataset_table = "\n".join(ds_lines) or "(no dataset items annotated)"

        user = CRITIC_DRIFT_USER_TEMPLATE.format(
            paired_table=paired_table,
            dataset_table=dataset_table,
            paired_count=len(pairs),
            mean_abs_score_delta=state.get("mean_abs_score_delta", 0.0),
            rec_agreement=state.get("rec_agreement", 0),
            dim_stats=json.dumps(state.get("dim_stats") or {}, indent=0)[:1500],
        )

        span.set_attribute("gen_ai.system", "openai")
        span.set_attribute("gen_ai.request.model", DEFAULT_MODEL)
        span.set_attribute("drift.input_pairs", len(pairs))

        try:
            # Direct Codex Responses API call with structured JSON output
            # (see obs/oauth_llm.py::codex_structured_invoke).
            result: DriftClusterList = codex_structured_invoke(
                DriftClusterList,
                instructions=CRITIC_DRIFT_SYSTEM_PROMPT,
                user=user,
                model=DEFAULT_MODEL,
                max_retries=2,
            )
        except Exception as exc:
            span.record_exception(exc)
            from opentelemetry.trace import Status, StatusCode
            span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            return {"clusters": [], "error": f"detect_drift LLM failed: {exc}"}

        clusters_raw = [c.model_dump() for c in result.clusters]
        span.set_attribute("drift.clusters_found", len(clusters_raw))
        span.add_event(
            "gen_ai.content.completion",
            {"gen_ai.completion": result.model_dump_json()},
        )
        return {"clusters": clusters_raw}


def generate_proposals_node(state: CriticState) -> dict:
    """LLM generates concrete proposals tied to each cluster."""
    with _TRACER.start_as_current_span("critic.generate_proposals") as span:
        clusters = state.get("clusters") or []
        if not clusters:
            span.set_attribute("proposals.skipped", "no_clusters")
            return {"proposals_raw": []}

        try:
            allowed_knobs = json.loads(ALLOWED_KNOBS_PATH.read_text(encoding="utf-8"))
        except Exception:
            allowed_knobs = {"knobs": [], "propose_only": []}

        user = CRITIC_PROPOSAL_USER_TEMPLATE.format(
            clusters_json=json.dumps(clusters, indent=2),
            allowed_knobs_json=json.dumps(allowed_knobs, indent=2),
        )

        span.set_attribute("gen_ai.system", "openai")
        span.set_attribute("gen_ai.request.model", DEFAULT_MODEL)
        span.set_attribute("proposals.input_clusters", len(clusters))

        try:
            result: ProposalList = codex_structured_invoke(
                ProposalList,
                instructions=CRITIC_PROPOSAL_SYSTEM_PROMPT,
                user=user,
                model=DEFAULT_MODEL,
                max_retries=2,
            )
        except Exception as exc:
            span.record_exception(exc)
            from opentelemetry.trace import Status, StatusCode
            span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            return {"proposals_raw": [], "error": f"generate_proposals LLM failed: {exc}"}

        raw = [p.model_dump() for p in result.proposals]
        span.set_attribute("proposals.generated", len(raw))
        return {"proposals_raw": raw}


# Mapping from proposal kind -> allowed_knobs name (when classifiable as auto-apply)
KIND_TO_KNOB = {
    "skill.ranking": "skill.success_ranking",
    "agent.reasoning_effort": "agent.reasoning_effort",
    "cron.cadence": "cron.cadence_within_50pct",  # only when within ±50%; checked at apply-time
}


# ---------------------------------------------------------------------------
# Phase C iter5: contradiction resolver node
# ---------------------------------------------------------------------------


def resolve_contradictions_node(state: CriticState) -> dict:
    """When reflexion_replay flags >= 2 proposals as `contradiction_detected`
    (e.g. two threshold proposals pulling in opposite directions), invoke an
    LLM resolver to replace them with 0-2 unified proposals.

    Strategies preferred (per CRITIC_RESOLVER_SYSTEM_PROMPT):
      * switch matcher.threshold_adjust -> matcher.prompt_edit (rubric refinement)
      * switch to matcher.dimension_weight (re-weight the drifting dimension)
      * return EMPTY if the data genuinely doesn't support a directional change

    Replaces the original contradicting proposals; non-contradicting proposals
    in proposals_classified are kept as-is. The new proposals get an
    `_resolved_from` audit field listing which originals they replace.

    Single-pass: we don't re-run replay on the resolved proposals here (would
    require LangGraph cycles). If the resolver itself produces a fresh
    contradiction, Diego sees it in the retro and can take it from there.
    """
    with _TRACER.start_as_current_span("critic.resolve_contradictions") as span:
        proposals = state.get("proposals_classified") or []
        contradicting = [
            p for p in proposals
            if (p.get("replay") or {}).get("status") == "contradiction_detected"
        ]
        if not contradicting:
            span.set_attribute("contradictions.skipped", "none_detected")
            return {}

        span.set_attribute("contradictions.input_count", len(contradicting))

        # Find the cluster context (all contradictions today are intra-cluster)
        cluster_name = contradicting[0].get("cluster_pattern_name") or ""
        target_cluster = next(
            (c for c in (state.get("clusters") or []) if c.get("pattern_name") == cluster_name),
            None,
        )

        user = CRITIC_RESOLVER_USER_TEMPLATE.format(
            contradicting_json=json.dumps(
                [
                    {
                        "proposal_id": p.get("proposal_id"),
                        "kind": p.get("kind"),
                        "summary": p.get("summary"),
                        "specific_change": p.get("specific_change"),
                        "rationale": p.get("rationale"),
                        "risk": p.get("risk"),
                        "replay_status": (p.get("replay") or {}).get("status"),
                    }
                    for p in contradicting
                ],
                indent=2,
            ),
            cluster_json=json.dumps(target_cluster or {"pattern_name": cluster_name}, indent=2),
        )

        try:
            from obs.oauth_llm import codex_structured_invoke

            result: ProposalList = codex_structured_invoke(
                ProposalList,
                instructions=CRITIC_RESOLVER_SYSTEM_PROMPT,
                user=user,
                max_retries=1,
            )
        except Exception as exc:
            span.record_exception(exc)
            from opentelemetry.trace import Status, StatusCode
            span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            # On resolver failure, leave the contradicting proposals as-is so
            # Diego still sees them.
            return {}

        contradicting_ids = {p.get("proposal_id") for p in contradicting}
        keep = [p for p in proposals if p.get("proposal_id") not in contradicting_ids]
        resolved_raw = [p.model_dump() for p in result.proposals]
        for rp in resolved_raw:
            rp["_resolved_from"] = sorted(contradicting_ids)

        # Re-run classification on the resolved set so they pick up the
        # auto_apply / propose_only tags + allowed_knob_match.
        for rp in resolved_raw:
            knob = KIND_TO_KNOB.get(rp.get("kind", ""))
            risk = rp.get("risk", "high")
            rp["classification"] = "auto_apply" if (knob and risk != "high") else "propose_only"
            rp["allowed_knob_match"] = knob
            # No replay on resolver output (single-pass design)
            rp["replay"] = {
                "supported": False,
                "status": "post_resolution_unreplayed",
                "notes": "Generated by resolve_contradictions_node; re-run reflexion manually if needed.",
            }

        out = keep + resolved_raw
        span.set_attribute("contradictions.replaced", len(contradicting))
        span.set_attribute("contradictions.resolutions_emitted", len(resolved_raw))
        return {
            "proposals_classified": out,
            "contradiction_resolutions": [
                {"replaced": sorted(contradicting_ids), "with": [r.get("proposal_id") for r in resolved_raw]}
            ],
        }


# ---------------------------------------------------------------------------
# Phase C iter2: Reflexion replay node
# ---------------------------------------------------------------------------


def _llm_prompt_edit_replay(state: CriticState, proposal: dict, replay: dict) -> dict:
    """iter4: replay a matcher.prompt_edit proposal by re-invoking the matcher
    LLM with the proposed prompt addition, on each evidence_job in the cluster.

    Calls codex_structured_invoke per job (~5s each), so a 4-job cluster costs
    ~20s of LLM time. Gated by HERMES_CRITIC_LLM_REPLAY env to keep the daily
    Critic run cheap.

    Determines status:
      * "would_close_drift"  — new scores move toward production by >=50% of
        the original gap on at least 2/3 of evidence jobs
      * "no_effect"          — new scores within 0.3 of shadow baseline
      * "made_drift_worse"   — new scores moved AWAY from production
      * "mixed_effect"       — direction inconsistent across jobs
      * "evidence_not_resolvable" — couldn't fetch any job_data
      * "replay_error"       — LLM call failed
    """
    from ._job_data import get_job_data
    from ._prompts import MATCHER_SYSTEM_PROMPT, MATCHER_USER_TEMPLATE
    from ._profile import load_profile_summary
    from .jobflow import MatcherScore  # the same Pydantic schema matcher uses
    from obs.oauth_llm import codex_structured_invoke

    # Map the cluster's evidence to paired_jobs entries (so we know baseline scores)
    pairs_by_id: dict[str, dict] = {}
    for pr in (state.get("paired_jobs") or []):
        jid = str(pr.get("job_id", ""))
        pairs_by_id[jid] = pr
        # Also index by truncated prefix (Critic sometimes outputs ids with
        # trailing dashes from prefix-truncation in retro rendering)
        for trunc in (jid[:14], jid[:13], jid[:12]):
            if trunc and trunc not in pairs_by_id:
                pairs_by_id[trunc] = pr

    # Find the cluster this proposal references to get evidence_job_ids
    cluster_name = proposal.get("cluster_pattern_name") or ""
    target_cluster = next(
        (c for c in (state.get("clusters") or []) if c.get("pattern_name") == cluster_name),
        None,
    )
    if not target_cluster:
        replay["status"] = "evidence_not_resolvable"
        replay["notes"] = f"could not find cluster {cluster_name!r} in state"
        return replay

    evidence_ids = target_cluster.get("evidence_job_ids") or []
    if not evidence_ids:
        replay["status"] = "evidence_not_resolvable"
        replay["notes"] = "cluster has no evidence_job_ids"
        return replay

    # Build the modified system prompt by appending the specific_change to the
    # rubric. The Critic typically outputs text like '"Score X based on..."'
    # already wrapped in quotes; we append verbatim.
    spec = proposal.get("specific_change", "")
    modified_system = (
        MATCHER_SYSTEM_PROMPT.rstrip()
        + "\n\n## Additional rubric guidance (proposed; under reflexion replay)\n"
        + spec.strip()
    )

    profile_summary = load_profile_summary()

    per_job: list[dict] = []
    successes = 0
    closer_to_prod = 0
    farther_from_prod = 0
    mean_abs_delta_pre = 0.0
    mean_abs_delta_post = 0.0
    n = 0

    for ev_id in evidence_ids[:6]:  # cap at 6 to bound LLM cost
        # Resolve the paired-jobs row + the full job data
        pair = pairs_by_id.get(ev_id) or pairs_by_id.get(ev_id.rstrip("-"))
        job_data = get_job_data(ev_id)
        if not job_data:
            per_job.append({"job_id": ev_id, "status": "job_data_not_found"})
            continue

        prod_score = (pair or {}).get("prod_score")
        shadow_score = (pair or {}).get("shadow_score")

        user_msg = MATCHER_USER_TEMPLATE.format(
            profile_summary=profile_summary,
            title=job_data.get("title", "(unknown)"),
            company=job_data.get("company", "(unknown)"),
            location=job_data.get("location", "(unknown)"),
            seniority_level=job_data.get("seniority_level", "(unknown)"),
            salary_range=job_data.get("salary_range") or "(not disclosed)",
            source_board=job_data.get("source_board") or job_data.get("source", "unknown"),
            url=job_data.get("url") or job_data.get("source_url", ""),
            description=(job_data.get("description") or job_data.get("description_raw") or "")[:12000],
        )
        try:
            new_result: MatcherScore = codex_structured_invoke(
                MatcherScore,
                instructions=modified_system,
                user=user_msg,
                max_retries=1,
            )
        except Exception as exc:
            per_job.append({"job_id": ev_id, "status": "replay_failed", "error": str(exc)[:120]})
            continue

        new_score = float(new_result.score)
        successes += 1

        if isinstance(prod_score, (int, float)) and isinstance(shadow_score, (int, float)):
            pre_gap = shadow_score - prod_score
            post_gap = new_score - prod_score
            mean_abs_delta_pre += abs(pre_gap)
            mean_abs_delta_post += abs(post_gap)
            n += 1
            # Closer = post_gap has same sign but smaller magnitude; farther = larger.
            if abs(post_gap) <= abs(pre_gap) * 0.6:
                closer_to_prod += 1
            elif abs(post_gap) > abs(pre_gap) * 1.1:
                farther_from_prod += 1

        per_job.append(
            {
                "job_id": ev_id,
                "status": "ok",
                "prod_score": prod_score,
                "shadow_baseline": shadow_score,
                "shadow_with_proposed_prompt": new_score,
                "new_recommendation": new_result.recommendation,
            }
        )

    if not successes:
        replay["status"] = "evidence_not_resolvable"
        replay["notes"] = (
            f"could not resolve job_data for any evidence id "
            f"(checked: {', '.join(evidence_ids[:3])})"
        )
        replay["per_job"] = per_job
        return replay

    # Verdict
    if closer_to_prod >= max(2, successes - 1):
        status = "would_close_drift"
        notes = (
            f"Proposed prompt addition closes drift on {closer_to_prod}/{successes} "
            f"evidence jobs. Mean |delta-from-prod| went {mean_abs_delta_pre/max(n,1):.2f} -> "
            f"{mean_abs_delta_post/max(n,1):.2f}."
        )
    elif farther_from_prod >= 1 and closer_to_prod == 0:
        status = "made_drift_worse"
        notes = (
            f"Proposed prompt addition INCREASED drift on {farther_from_prod} "
            f"evidence jobs and didn't close any. Don't apply."
        )
    elif n > 0 and (mean_abs_delta_pre / n) - (mean_abs_delta_post / n) < 0.2:
        status = "no_effect"
        notes = (
            f"Proposed prompt addition does not meaningfully change scores "
            f"(mean |delta-from-prod| {mean_abs_delta_pre/n:.2f} -> {mean_abs_delta_post/n:.2f})."
        )
    else:
        status = "mixed_effect"
        notes = (
            f"Mixed: {closer_to_prod} closer, {farther_from_prod} farther, "
            f"{successes - closer_to_prod - farther_from_prod} similar. Inspect per_job."
        )

    replay["status"] = status
    replay["notes"] = notes
    replay["per_job"] = per_job
    replay["mean_abs_delta_before"] = round(mean_abs_delta_pre / max(n, 1), 2) if n else None
    replay["mean_abs_delta_after"] = round(mean_abs_delta_post / max(n, 1), 2) if n else None
    return replay


def reflexion_replay_node(state: CriticState) -> dict:
    """Validate each proposal by simulating its effect on the paired jobs.

    For threshold-adjust proposals, we deterministically replay: apply the
    proposed threshold to each paired job's existing score, recompute the
    decision (PROCEED/REVIEW/ARCHIVE), compare to status quo. Also detect
    intra-batch CONTRADICTIONS (two proposals pulling the threshold opposite
    directions on the same dimension cluster).

    For non-threshold proposals (prompt edits, dimension-weight changes), v1
    just attaches a `replay_supported: false` note. Real LLM-backed replay
    of those changes is iter3 work.

    Output: each proposal in proposals_classified gains a `replay` dict with:
        {supported, recommendation_flips, agreement_after, status, notes}
    """
    with _TRACER.start_as_current_span("critic.reflexion_replay") as span:
        proposals = state.get("proposals_classified") or []
        pairs = state.get("paired_jobs") or []
        if not proposals or not pairs:
            span.set_attribute("replay.skipped", "no_proposals_or_pairs")
            return {"proposals_classified": proposals}

        # Pull existing thresholds for status quo replay.
        existing_proceed = float(os.environ.get("HERMES_JOBFLOW_PROCEED_THRESHOLD", "8.75"))
        existing_review = float(os.environ.get("HERMES_JOBFLOW_REVIEW_THRESHOLD", "5.0"))

        def _decision_for(score: float, proceed: float, review: float) -> str:
            if score is None:
                return "review"
            if score >= proceed:
                return "tailor"
            if score >= review:
                return "review"
            return "archive"

        # Status-quo agreement (using shadow scores under existing thresholds)
        sq_decisions: dict[str, str] = {}
        for p in pairs:
            jid = p.get("job_id", "")
            shadow_score = p.get("shadow_score")
            sq_decisions[jid] = _decision_for(
                shadow_score if isinstance(shadow_score, (int, float)) else 0.0,
                existing_proceed,
                existing_review,
            )

        # Index proposals by direction for contradiction detection. Parse the
        # NEW value out of "set HERMES_JOBFLOW_PROCEED_THRESHOLD=8.50, was 8.75"
        # (or REVIEW_THRESHOLD) and compare to the current env value.
        threshold_proposals = [p for p in proposals if p.get("kind") == "matcher.threshold_adjust"]
        import re as _re
        directions = []
        for p in threshold_proposals:
            ch = p.get("specific_change") or ""
            # Try PROCEED first, then REVIEW
            for env_var, base in (
                ("HERMES_JOBFLOW_PROCEED_THRESHOLD", existing_proceed),
                ("HERMES_JOBFLOW_REVIEW_THRESHOLD", existing_review),
            ):
                m = _re.search(rf"{env_var}\s*=\s*([0-9]+(?:\.[0-9]+)?)", ch)
                if m:
                    new_val = float(m.group(1))
                    if new_val > base:
                        directions.append("up")
                    elif new_val < base:
                        directions.append("down")
                    break
        contradiction = ("up" in directions) and ("down" in directions)

        for p in proposals:
            kind = p.get("kind", "")
            replay: dict = {
                "supported": False,
                "recommendation_flips": [],
                "agreement_after": None,
                "status": "skipped",
                "notes": "",
            }

            if kind == "matcher.threshold_adjust":
                # Parse the new threshold from specific_change ("set HERMES_JOBFLOW_PROCEED_THRESHOLD=8.50, was 8.75")
                ch = p.get("specific_change", "")
                new_proceed = existing_proceed
                new_review = existing_review
                try:
                    if "PROCEED_THRESHOLD=" in ch:
                        target = ch.split("PROCEED_THRESHOLD=", 1)[1].split(",", 1)[0].strip()
                        new_proceed = float(target)
                    if "REVIEW_THRESHOLD=" in ch:
                        target = ch.split("REVIEW_THRESHOLD=", 1)[1].split(",", 1)[0].strip()
                        new_review = float(target)
                except Exception:
                    pass

                flips = []
                for pr in pairs:
                    jid = pr.get("job_id", "")
                    shadow_score = pr.get("shadow_score")
                    if not isinstance(shadow_score, (int, float)):
                        continue
                    new_decision = _decision_for(shadow_score, new_proceed, new_review)
                    old_decision = sq_decisions.get(jid)
                    if new_decision != old_decision:
                        flips.append(
                            {
                                "job_id": jid[:14],
                                "score": shadow_score,
                                "old": old_decision,
                                "new": new_decision,
                            }
                        )
                replay["supported"] = True
                replay["recommendation_flips"] = flips
                # Agreement is computed against PROD scores — if our threshold
                # change moves SHADOW recommendations toward PROD's, agreement improves.
                # Here we approximate: count how many shadow recommendations now
                # match prod's recommendation (kept from pair["prod_rec"]).
                agreed = 0
                total = 0
                for pr in pairs:
                    shadow_score = pr.get("shadow_score")
                    if not isinstance(shadow_score, (int, float)):
                        continue
                    total += 1
                    new_decision = _decision_for(shadow_score, new_proceed, new_review)
                    # Map decision -> recommendation for comparison vs prod_rec
                    rec_map = {"tailor": "PROCEED", "review": "REVIEW", "archive": "ARCHIVE"}
                    if rec_map.get(new_decision) == pr.get("prod_rec"):
                        agreed += 1
                replay["agreement_after"] = f"{agreed}/{total}" if total else "n/a"
                if contradiction:
                    replay["status"] = "contradiction_detected"
                    replay["notes"] = (
                        "Multiple threshold proposals pull in opposite directions in the "
                        "same Critic run. Review side-by-side before applying any."
                    )
                elif flips:
                    replay["status"] = "would_change_decisions"
                    replay["notes"] = f"Would flip {len(flips)} recommendation(s). Verify before applying."
                else:
                    replay["status"] = "no_effect"
                    replay["notes"] = "Threshold change does not flip any recommendation in the current paired set."
            elif kind == "matcher.prompt_edit" and os.environ.get("HERMES_CRITIC_LLM_REPLAY") == "1":
                # iter4: re-invoke matcher with the proposed prompt addition
                # against each evidence_job, diff scores against shadow baseline.
                # Costs ~5s LLM call per evidence job; gated by env flag.
                replay = _llm_prompt_edit_replay(state, p, replay)
            else:
                replay["status"] = "kind_not_replayable"
                replay["notes"] = (
                    f"Replay for kind={kind!r} requires LLM-driven re-scoring. "
                    f"Set HERMES_CRITIC_LLM_REPLAY=1 to enable for matcher.prompt_edit."
                )

            p["replay"] = replay

        span.set_attribute("replay.proposals_with_flips", sum(1 for p in proposals if p.get("replay", {}).get("recommendation_flips")))
        span.set_attribute("replay.contradiction_detected", contradiction)
        return {"proposals_classified": proposals}


def classify_proposals_node(state: CriticState) -> dict:
    """Tag each proposal as auto_apply or propose_only.

    Conservative: only proposals matching an explicit allowed_knobs entry
    AND with risk != "high" can be auto-applied. Everything else is propose-only.
    """
    with _TRACER.start_as_current_span("critic.classify_proposals") as span:
        out = []
        auto_count = 0
        propose_count = 0
        for p in state.get("proposals_raw") or []:
            kind = p.get("kind", "")
            risk = p.get("risk", "high")
            knob_match = KIND_TO_KNOB.get(kind)
            if knob_match and risk != "high":
                classification = "auto_apply"
                auto_count += 1
            else:
                classification = "propose_only"
                propose_count += 1
            p2 = dict(p)
            p2["classification"] = classification
            p2["allowed_knob_match"] = knob_match
            out.append(p2)
        span.set_attribute("classify.auto_apply", auto_count)
        span.set_attribute("classify.propose_only", propose_count)
        return {"proposals_classified": out}


def _execute_skill_ranking(p: dict) -> tuple[bool, str, dict]:
    """iter2 real auto-apply for skill.success_ranking.

    Expected proposal payload conventions (specific_change can be JSON or
    structured text; we accept either):
      JSON form:  '{"skill": "score_calibration_senior_pm", "success_delta": 1, "fail_delta": 0}'
      Text form:  'bump score_calibration_senior_pm: success+1 fail+0'

    Effects: opens skills/<skill>/metadata.json, bumps success/fail counters,
    recomputes confidence = success / (success + fail), writes back.
    Refuses to act if metadata.json doesn't exist (only ranks pre-existing skills).
    Writes a reversal SHELL script that subtracts the deltas.

    Returns: (executed, note, applied_record)
    """
    spec = p.get("specific_change") or ""
    skill = None
    success_delta = 0
    fail_delta = 0
    try:
        if spec.strip().startswith("{"):
            parsed = json.loads(spec)
            skill = parsed.get("skill")
            success_delta = int(parsed.get("success_delta", 0))
            fail_delta = int(parsed.get("fail_delta", 0))
        else:
            import re as _re
            m = _re.search(r"bump\s+([a-z0-9_\-]+).*?success([+-]\d+).*?fail([+-]\d+)", spec, _re.I)
            if m:
                skill = m.group(1)
                success_delta = int(m.group(2))
                fail_delta = int(m.group(3))
    except Exception:
        return False, f"could not parse specific_change for skill ranking: {spec!r}", {}

    if not skill:
        return False, "no skill name parsed from specific_change", {}

    metadata_path = Path.home() / ".hermes" / "skills" / skill / "metadata.json"
    if not metadata_path.exists():
        return False, f"skill metadata not found at {metadata_path}", {}

    try:
        prior = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"could not read prior metadata: {e}", {}

    new_meta = dict(prior)
    new_meta["success"] = int(prior.get("success", 0)) + success_delta
    new_meta["fail"] = int(prior.get("fail", 0)) + fail_delta
    total = new_meta["success"] + new_meta["fail"]
    new_meta["confidence"] = round(new_meta["success"] / total, 3) if total else 0.0
    new_meta["last_critic_bump_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    metadata_path.write_text(json.dumps(new_meta, indent=2) + "\n", encoding="utf-8")
    return (
        True,
        f"bumped {skill}: success {prior.get('success',0)}->{new_meta['success']}, fail {prior.get('fail',0)}->{new_meta['fail']}, conf {new_meta['confidence']}",
        {
            "skill": skill,
            "prior_success": prior.get("success", 0),
            "prior_fail": prior.get("fail", 0),
            "new_success": new_meta["success"],
            "new_fail": new_meta["fail"],
            "new_confidence": new_meta["confidence"],
            "metadata_path": str(metadata_path),
        },
    )


def auto_apply_node(state: CriticState) -> dict:
    """Execute auto-apply proposals + write reversal scripts.

    iter2 scope:
      * `skill.success_ranking` -> REAL counter bump in skills/<skill>/metadata.json
      * Other knob types (reasoning_effort, cron.cadence) still reclassify to
        propose_only with a placeholder reversal until iter3.

    Reversals: every executed change writes a shell-style reversal at
    workspace/reversals/<ts>_<proposal_id>.{sh|json}. The .json variant
    captures the prior values so a future revert script can read them.
    """
    with _TRACER.start_as_current_span("critic.auto_apply") as span:
        applied = []
        errors = []
        REVERSALS_DIR.mkdir(parents=True, exist_ok=True)
        for p in state.get("proposals_classified") or []:
            if p.get("classification") != "auto_apply":
                continue
            knob = p.get("allowed_knob_match")
            ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            reversal_path = REVERSALS_DIR / f"{ts}_{p['proposal_id']}.json"
            try:
                if knob == "skill.success_ranking":
                    executed, note, record = _execute_skill_ranking(p)
                    if executed:
                        reversal_path.write_text(
                            json.dumps(
                                {
                                    "kind": "skill.success_ranking",
                                    "proposal_id": p["proposal_id"],
                                    "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                    "to_revert": {
                                        "metadata_path": record["metadata_path"],
                                        "restore_to": {
                                            "success": record["prior_success"],
                                            "fail": record["prior_fail"],
                                        },
                                    },
                                },
                                indent=2,
                            ),
                            encoding="utf-8",
                        )
                        p["_executed"] = True
                        p["_record"] = record
                        applied.append(
                            {
                                "proposal_id": p["proposal_id"],
                                "knob": knob,
                                "executed": True,
                                "reversal_script": str(reversal_path),
                                "note": note,
                                "record": record,
                            }
                        )
                    else:
                        reversal_path.write_text(
                            f"# Skipped — could not execute skill ranking auto-apply\n# reason: {note}\n",
                            encoding="utf-8",
                        )
                        p["_executed"] = False
                        p["_v1_note"] = f"skill.ranking auto-apply skipped: {note}"
                        p["classification"] = "propose_only"
                        applied.append(
                            {
                                "proposal_id": p["proposal_id"],
                                "knob": knob,
                                "executed": False,
                                "reversal_script": str(reversal_path),
                                "note": p["_v1_note"],
                            }
                        )
                else:
                    # iter2 still defers reasoning_effort + cron.cadence to iter3.
                    reversal_path.write_text(
                        json.dumps(
                            {
                                "kind": p.get("kind"),
                                "knob": knob,
                                "proposal_id": p["proposal_id"],
                                "note": "auto-apply implementation deferred to Critic iter3",
                                "specific_change": p.get("specific_change"),
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    p["_executed"] = False
                    p["_v1_note"] = f"{knob} auto-apply implementation deferred to Critic iter3"
                    p["classification"] = "propose_only"
                    applied.append(
                        {
                            "proposal_id": p["proposal_id"],
                            "knob": knob,
                            "executed": False,
                            "reversal_script": str(reversal_path),
                            "note": p["_v1_note"],
                        }
                    )
            except Exception as exc:
                errors.append({"proposal_id": p["proposal_id"], "error": str(exc)})
        span.set_attribute("auto_apply.applied", len(applied))
        span.set_attribute("auto_apply.executed", sum(1 for a in applied if a.get("executed")))
        span.set_attribute("auto_apply.errors", len(errors))
        return {
            "auto_applied": applied,
            "auto_apply_errors": errors,
            "proposals_classified": state.get("proposals_classified") or [],
        }


def emit_proposals_node(state: CriticState) -> dict:
    """Write propose-only proposals to mailbox/main/inbox + whatsapp_queue.jsonl
    AND emit CRITIC_PROPOSAL events to the bus so the existing telegram_notifier
    + whatsapp_escalator pick them up automatically (Phase C iter2).

    iter2: replay evidence + contradiction flags surface in both the mailbox
    message and the event payload so Diego sees the validation context.
    """
    with _TRACER.start_as_current_span("critic.emit_proposals") as span:
        emitted = []
        events_emitted = 0
        run_id = state.get("run_id") or uuid.uuid4().hex[:12]
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        PROPOSAL_MAILBOX.mkdir(parents=True, exist_ok=True)
        WHATSAPP_QUEUE.parent.mkdir(parents=True, exist_ok=True)
        for p in state.get("proposals_classified") or []:
            if p.get("classification") != "propose_only":
                continue
            replay = p.get("replay") or {}
            payload = {
                "proposal_id": p["proposal_id"],
                "kind": p["kind"],
                "summary": p["summary"],
                "specific_change": p["specific_change"],
                "rationale": p["rationale"],
                "expected_effect": p["expected_effect"],
                "risk": p["risk"],
                "cluster_pattern_name": p.get("cluster_pattern_name"),
                "v1_note": p.get("_v1_note"),
                "replay": replay,
                "decision_required": True,
            }
            msg = {
                "type": "CRITIC_PROPOSAL",
                "from": "critic",
                "to": "main",
                "job_id": None,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "correlation_id": f"critic-{run_id}",
                "payload": payload,
            }
            mailbox_path = PROPOSAL_MAILBOX / f"{ts}_{p['proposal_id']}_CRITIC_PROPOSAL_critic.json"
            mailbox_path.write_text(json.dumps(msg, indent=2, default=str), encoding="utf-8")

            wa_entry = {
                "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "run_id": run_id,
                "proposal_id": p["proposal_id"],
                "kind": p["kind"],
                "summary": p["summary"],
                "risk": p["risk"],
                "mailbox_path": str(mailbox_path),
                "replay_status": replay.get("status"),
            }
            with open(WHATSAPP_QUEUE, "a", encoding="utf-8") as f:
                f.write(json.dumps(wa_entry, default=str) + "\n")

            # iter2: emit CRITIC_PROPOSAL event to the bus. Telegram notifier
            # routes to "system" topic; WhatsApp escalator queues at IMPORTANT
            # tier (flushes in next digest, not URGENT — Critic is advisory).
            event_id = _emit_event(
                "critic_proposal",
                source="critic.graph",
                payload={**payload, "mailbox_path": str(mailbox_path), "run_id": run_id},
            )
            if event_id:
                events_emitted += 1

            emitted.append(
                {
                    "proposal_id": p["proposal_id"],
                    "mailbox_path": str(mailbox_path),
                    "event_id": event_id,
                }
            )
        span.set_attribute("emit.count", len(emitted))
        span.set_attribute("emit.bus_events", events_emitted)
        return {"emitted": emitted, "run_id": run_id}


def finalize_node(state: CriticState) -> dict:
    """Append run summary to changelog.jsonl + write a retro markdown.

    Empty-input guard (2026-06-20): when the run had NOTHING to analyze
    (diff_reports_used == [] AND paired_jobs == []) it produced no clusters,
    no proposals, and took no side-effectful action — so it must NOT append a
    changelog entry. Two consumers depend on this:

      * bin/critic_run.py:already_ran_today() keys idempotency off the last
        REAL changelog entry. An empty entry dated today would make every
        same-day Task Scheduler repetition (07:30->12:30 PT30M) no-op, stranding
        same-day recovery once a fresh diff-report finally appears — the
        multi-day-downtime case (laptop off 6/13-6/19).
      * the laptop-monitor ">30h staleness alarm" watches changelog.jsonl's
        MTIME. Skipping the write means an empty fire does NOT refresh that
        mtime, so the alarm still trips when no real proposal generation has
        landed — an empty run can never falsely satisfy it.

    The retro markdown is still written (harmless, and documents "nothing to
    do"); only the changelog append is gated.
    """
    with _TRACER.start_as_current_span("critic.finalize") as span:
        run_id = state.get("run_id") or uuid.uuid4().hex[:12]
        clusters = state.get("clusters") or []
        proposals = state.get("proposals_classified") or []
        emitted = state.get("emitted") or []
        applied = state.get("auto_applied") or []

        diff_reports = state.get("diff_reports_used") or []
        paired = state.get("paired_jobs") or []
        empty_input = not diff_reports and not paired

        if empty_input:
            span.set_attribute("finalize.empty_input", True)
        else:
            CHANGELOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            log_entry = {
                "run_id": run_id,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "diff_reports_used": diff_reports,
                "paired_jobs": len(paired),
                "dataset_items": len(state.get("dataset_items") or []),
                "clusters_found": len(clusters),
                "proposals_generated": len(proposals),
                "auto_applied_count": sum(1 for a in applied if a.get("executed")),
                "auto_apply_deferred_count": sum(1 for a in applied if not a.get("executed")),
                "propose_only_count": len(emitted),
            }
            with open(CHANGELOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, default=str) + "\n")

        # Retro markdown
        RETROS_DIR.mkdir(parents=True, exist_ok=True)
        retro_path = RETROS_DIR / f"{time.strftime('%Y-%m-%d')}_critic-graph_{run_id}.md"
        lines: list[str] = [
            f"# Critic retro (graph) — run {run_id}",
            "",
            f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
            f"- Diff reports consumed: {len(state.get('diff_reports_used') or [])}",
            f"- Paired jobs analyzed: {len(state.get('paired_jobs') or [])}",
            f"- Dataset items: {len(state.get('dataset_items') or [])}",
            f"- Mean |score delta|: {state.get('mean_abs_score_delta')}",
            f"- Recommendation agreement: {state.get('rec_agreement')}/{state.get('paired_count')}",
            "",
            f"## Drift clusters ({len(clusters)})",
            "",
        ]
        if not clusters:
            lines.append("_No systematic drift detected. Sample may be too small or the graph and mailbox Matchers are well-aligned._")
        for c in clusters:
            lines += [
                f"### {c.get('pattern_name')}",
                f"- Severity: **{c.get('severity')}** / Direction: {c.get('direction')} / Mean Δ: {c.get('mean_delta')}",
                f"- Affected dimensions: {', '.join(c.get('affected_dimensions') or []) or '(none)'}",
                f"- Description: {c.get('description')}",
                f"- Hypothesized cause: {c.get('hypothesized_root_cause')}",
                f"- Evidence (job ids): {', '.join((c.get('evidence_job_ids') or [])[:6])}",
                "",
            ]
        lines += [f"## Proposals ({len(proposals)})", ""]
        for p in proposals:
            lines += [
                f"### {p.get('proposal_id')} — {p.get('kind')}",
                f"- Classification: **{p.get('classification')}** / Risk: {p.get('risk')}",
                f"- Targets cluster: `{p.get('cluster_pattern_name')}`",
                f"- Summary: {p.get('summary')}",
                f"- Specific change: `{p.get('specific_change')}`",
                f"- Rationale: {p.get('rationale')}",
                f"- Expected effect: {p.get('expected_effect')}",
            ]
            replay = p.get("replay") or {}
            if replay:
                lines.append(f"- Reflexion replay: **{replay.get('status')}**")
                if replay.get("notes"):
                    lines.append(f"  - {replay['notes']}")
                if replay.get("agreement_after"):
                    lines.append(f"  - Agreement after replay: {replay['agreement_after']}")
                flips = replay.get("recommendation_flips") or []
                if flips:
                    lines.append(f"  - Would flip {len(flips)} recommendation(s):")
                    for f in flips[:6]:
                        lines.append(f"    - `{f.get('job_id')}` (score {f.get('score')}): {f.get('old')} -> {f.get('new')}")
            if p.get("_v1_note"):
                lines.append(f"- v1 note: {p['_v1_note']}")
            if p.get("_record"):
                lines.append(f"- Auto-applied record: {p['_record']}")
            lines.append("")
        lines += [
            "## Outputs",
            "",
            f"- Auto-applied (executed): {sum(1 for a in applied if a.get('executed'))}",
            f"- Auto-apply deferred (logged + reclassified): {sum(1 for a in applied if not a.get('executed'))}",
            f"- Propose-only emitted to mailbox/main/inbox: {len(emitted)}",
            "",
            f"_Run id: {run_id} • model: {DEFAULT_MODEL}_",
        ]
        retro_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        span.set_attribute("finalize.changelog_appended", not empty_input)
        span.set_attribute("finalize.retro_path", str(retro_path))
        return {
            "retro_path": str(retro_path),
            "run_id": run_id,
            "changelog_appended": not empty_input,
        }


# ---------------------------------------------------------------------------
# Graph builder + entrypoint
# ---------------------------------------------------------------------------


def build_critic_graph():
    """Compile the Critic LangGraph (Phase C iter2).

    Pipeline:
      load_calibration -> detect_drift -> generate_proposals
                       -> classify_proposals -> reflexion_replay
                       -> auto_apply -> emit_proposals -> finalize
    """
    g = StateGraph(CriticState)
    g.add_node("load_calibration", load_calibration_node)
    g.add_node("detect_drift", detect_drift_node)
    g.add_node("generate_proposals", generate_proposals_node)
    g.add_node("classify_proposals", classify_proposals_node)
    g.add_node("reflexion_replay", reflexion_replay_node)
    # iter5: resolve_contradictions sits AFTER replay so it sees the
    # contradiction_detected flag, BEFORE auto_apply so resolved proposals get
    # the same downstream treatment as direct ones.
    g.add_node("resolve_contradictions", resolve_contradictions_node)
    g.add_node("auto_apply", auto_apply_node)
    g.add_node("emit_proposals", emit_proposals_node)
    g.add_node("finalize", finalize_node)

    g.set_entry_point("load_calibration")
    g.add_edge("load_calibration", "detect_drift")
    g.add_edge("detect_drift", "generate_proposals")
    g.add_edge("generate_proposals", "classify_proposals")
    g.add_edge("classify_proposals", "reflexion_replay")
    g.add_edge("reflexion_replay", "resolve_contradictions")
    g.add_edge("resolve_contradictions", "auto_apply")
    g.add_edge("auto_apply", "emit_proposals")
    g.add_edge("emit_proposals", "finalize")
    g.add_edge("finalize", END)

    return g.compile()


def invoke_critic(window_days: int = 7, dataset_name: str = "hermes-jobs-v1") -> CriticState:
    """Run the Critic graph end-to-end. Returns final state.

    Wraps everything in a top-level span so Langfuse groups child spans under one trace.
    """
    run_id = uuid.uuid4().hex[:12]
    with _TRACER.start_as_current_span(f"critic.run:{run_id}") as parent:
        parent.set_attribute("critic.run_id", run_id)
        parent.set_attribute("critic.window_days", window_days)
        parent.set_attribute("critic.dataset", dataset_name)
        graph = build_critic_graph()
        initial: CriticState = {
            "diff_report_window_days": window_days,
            "dataset_name": dataset_name,
            "run_id": run_id,
        }
        result = graph.invoke(initial)
        parent.set_attribute("critic.clusters_found", len(result.get("clusters") or []))
        parent.set_attribute("critic.proposals_emitted", len(result.get("emitted") or []))
        parent.set_attribute("critic.auto_applied", len(result.get("auto_applied") or []))
        return result  # type: ignore[return-value]
