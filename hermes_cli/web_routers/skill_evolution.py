"""Skill evolution dashboard routes (2026-08).

Exposes the skill-outcome telemetry and the reflection proposal queue added
by the skill-evolution work (tools/skill_usage.py outcome scoring +
agent/skill_reflection.py proposal queue) to the desktop dashboard:

    GET  /api/skills/evolution                — overview: counts, top skills,
                                               low-utility candidates
    GET  /api/skills/evolution/outcomes       — per-skill outcome telemetry
    GET  /api/skills/evolution/proposals      — proposal queue (pending/all)
    POST /api/skills/evolution/proposals/{skill}/{proposal_id}/approve
                                               — approve + apply a bounded edit
    POST /api/skills/evolution/proposals/{skill}/{proposal_id}/reject
                                               — reject a proposal

All handlers are read-mostly; the only mutations are proposal status
transitions. Proposal *application* (approve) performs a bounded, section-
targeted patch of SKILL.md guarded by the same linter/guard checks the
skill_manage tool enforces, so the "human approves, machine applies a
bounded edit" contract from the reflection loop is preserved.
"""

import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException

router = APIRouter()

_log = logging.getLogger("hermes_cli.web_server")


def _skill_evolution_usage() -> dict:
    """Snapshot of outcome telemetry + utility scores (best-effort)."""
    try:
        from tools.skill_usage import load_usage, _backfill_outcome_keys
        data = load_usage()
        rows = []
        for name, raw in data.items():
            if not isinstance(raw, dict):
                continue
            rec = _backfill_outcome_keys(raw)
            rows.append(
                {
                    "skill": str(name),
                    "utility_score": rec.get("utility_score"),
                    "success_count": rec.get("success_count", 0),
                    "failure_count": rec.get("failure_count", 0),
                    "unknown_count": rec.get("unknown_count", 0),
                    "use_count": rec.get("use_count", 0),
                    "patch_count": rec.get("patch_count", 0),
                    "last_outcome_at": rec.get("last_outcome_at"),
                    "state": rec.get("state", "active"),
                }
            )
        # Sort: scored skills (utility desc) first, then used-but-unscored
        # (use_count desc, stable), then never-used skills last. This keeps
        # the dashboard meaningful before real outcomes accumulate — top
        # skills are the most-used ones while telemetry ramps up.
        def _sort_key(r):
            score = r["utility_score"]
            if score is not None:
                return (0, -score, 0)
            if r["use_count"] > 0:
                return (1, 0, -r["use_count"])
            return (2, 0, 0)

        rows.sort(key=_sort_key)
        return {"skills": rows, "total": len(rows)}
    except Exception as e:  # pragma: no cover - defensive
        _log.exception("skill evolution usage snapshot failed")
        return {"skills": [], "total": 0, "error": str(e)}


def _low_utility_candidates(limit: int = 20) -> list:
    try:
        from tools.skill_usage import list_low_utility_skills
        return list_low_utility_skills(max_score=0.4)[:limit]
    except Exception:  # pragma: no cover - defensive
        return []


def _proposal_rows(skill: Optional[str] = None, pending_only: bool = False) -> list:
    try:
        from agent.skill_reflection import list_proposals, list_pending_proposals
        if pending_only:
            rows = list_pending_proposals(skill)
        else:
            rows = list_proposals(skill)
        return rows
    except Exception:  # pragma: no cover - defensive
        return []


@router.get("/api/skills/evolution")
async def skill_evolution_overview():
    """Overview of the skill-evolution system: telemetry, candidates, queue,
    trends, utility distribution, and an overall health score."""
    usage = _skill_evolution_usage()
    proposals = _proposal_rows()
    pending = [p for p in proposals if p.get("status") == "pending"]
    candidates = _low_utility_candidates()
    scored = [s for s in usage.get("skills", []) if s["utility_score"] is not None]
    avg_utility = (
        round(sum(s["utility_score"] for s in scored) / len(scored), 4) if scored else None
    )

    # ── Trends: this week vs last week ──────────────────────────────────
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    def _in_window(rows, lo, hi):
        n = 0
        for r in rows:
            ts = r.get("created_at") or r.get("last_outcome_at")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except ValueError:
                continue
            if lo <= dt < hi:
                n += 1
        return n

    proposals_this = _in_window(proposals, week_ago, now)
    proposals_last = _in_window(proposals, two_weeks_ago, week_ago)

    # Outcome deltas from telemetry (success/failure counts are cumulative, so
    # we approximate weekly change via last_outcome_at recency per skill).
    outcomes_this = sum(1 for s in usage.get("skills", []) if s.get("last_outcome_at") and _in_window([{"created_at": s["last_outcome_at"]}], week_ago, now))
    outcomes_last = sum(1 for s in usage.get("skills", []) if s.get("last_outcome_at") and _in_window([{"created_at": s["last_outcome_at"]}], two_weeks_ago, week_ago))

    # ── Utility distribution (buckets: <0.4, 0.4–0.7, ≥0.7) ─────────────
    dist = {"low": 0, "mid": 0, "high": 0}
    for s in scored:
        v = s["utility_score"]
        if v < 0.4:
            dist["low"] += 1
        elif v < 0.7:
            dist["mid"] += 1
        else:
            dist["high"] += 1

    # ── Health score 0–100 ──────────────────────────────────────────────
    # Weighted: scored ratio, avg utility, pending-burden, low-utility ratio.
    total = usage.get("total", 0) or 1
    scored_ratio = len(scored) / total if total else 0
    low_ratio = len(candidates) / total if total else 0
    avg_u = avg_utility if avg_utility is not None else 0.0
    pending_burden = min(1.0, len(pending) / max(1, total) * 20)  # 5% pending → full burden
    health = round(
        100
        * (
            0.25 * scored_ratio
            + 0.35 * avg_u
            + 0.25 * (1 - low_ratio)
            + 0.15 * (1 - pending_burden)
        ),
        1,
    )

    return {
        "skills_total": usage.get("total", 0),
        "skills_scored": len(scored),
        "avg_utility": avg_utility,
        "proposals_pending": len(pending),
        "proposals_total": len(proposals),
        "low_utility_candidates": candidates,
        "top_skills": usage.get("skills", [])[:10],
        "bottom_skills": usage.get("skills", [])[-10:][::-1],
        "trends": {
            "proposals_this_week": proposals_this,
            "proposals_last_week": proposals_last,
            "outcomes_this_week": outcomes_this,
            "outcomes_last_week": outcomes_last,
        },
        "utility_distribution": dist,
        "health_score": health,
    }


@router.get("/api/skills/evolution/outcomes")
async def skill_evolution_outcomes(limit: int = 50):
    """Per-skill outcome telemetry, best (utility) first."""
    usage = _skill_evolution_usage()
    return {"skills": usage.get("skills", [])[: max(1, min(limit, 500))]}


@router.get("/api/skills/evolution/proposals")
async def skill_evolution_proposals(
    skill: Optional[str] = None,
    pending_only: bool = False,
    status: Optional[str] = None,
    limit: int = 50,
):
    """The reflection proposal queue.

    ``pending_only=true`` filters to open ones. ``status=applied|rejected|
    reviewed`` filters by audit state (applied proposals are kept in the
    history view). ``limit`` caps the returned rows (default 50).
    """
    rows = _proposal_rows(skill, pending_only=pending_only)
    if status:
        rows = [p for p in rows if p.get("status") == status]
    rows = rows[: max(1, min(limit, 500))]
    return {"proposals": rows, "total": len(rows)}


@router.get("/api/skills/evolution/skills/{skill}")
async def skill_evolution_detail(skill: str):
    """Full detail for one skill: telemetry summary + failure patterns +
    proposal history. Powers the skill detail drawer."""
    try:
        from tools.skill_usage import get_outcome_summary, get_utility_score
        from agent.skill_reflection import aggregate_failure_patterns, list_proposals
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"modules unavailable: {exc}")

    summary = get_outcome_summary(skill) or {}
    patterns = aggregate_failure_patterns(skill) or {}
    proposals = list_proposals(skill) or []
    return {
        "skill": skill,
        "utility_score": get_utility_score(skill),
        "summary": {
            "success_count": summary.get("success_count", 0),
            "failure_count": summary.get("failure_count", 0),
            "unknown_count": summary.get("unknown_count", 0),
            "use_count": summary.get("use_count", 0),
            "patch_count": summary.get("patch_count", 0),
            "last_outcome_at": summary.get("last_outcome_at"),
        },
        "failure_patterns": patterns,
        "proposals": proposals[:20],
    }


@router.post("/api/skills/evolution/skills/{skill}/propose")
async def propose_skill_improvement(skill: str):
    """One-click proposal generation for a skill (human reviews before
    anything is applied). Uses the reflection loop's aggregate_failure_patterns
    to diagnose the most common failure, then writes a bounded proposal."""
    try:
        from agent.skill_reflection import (
            aggregate_failure_patterns,
            build_proposal,
            list_pending_proposals,
            save_proposal,
        )
        from tools.skill_usage import get_utility_score
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"reflection module unavailable: {exc}")

    # Refuse to pile up: one pending proposal per skill at a time
    pending = list_pending_proposals(skill)
    if pending:
        return {"ok": False, "reason": "already_pending", "proposal": pending[0]}

    patterns = aggregate_failure_patterns(skill) or {}
    top_error_types = patterns.get("top_error_types") or []
    error_types = [str(et) for et, _count in top_error_types]
    failure_rate = patterns.get("failure_rate")

    if failure_rate in (None, 0) and not error_types:
        raise HTTPException(status_code=400, detail="no failure signals; nothing to propose")

    top_error = error_types[0] if error_types else "unknown"
    proposal = build_proposal(
        skill,
        diagnosis=(
            f"失败率 {failure_rate:.0%}，主要错误类型 {top_error}"
            if error_types and failure_rate is not None
            else f"主要错误类型 {top_error}"
            if error_types
            else f"失败率 {failure_rate:.0%}"
        ),
        target_section="pitfalls",
        suggested_fix=f"记录 {top_error} 失败的处理方式并补充到 Pitfalls 段",
        failure_types=error_types[:5],
        utility_score=get_utility_score(skill),
        source="dashboard",
    )
    save_proposal(proposal)
    return {"ok": True, "proposal": proposal}


@router.post("/api/skills/evolution/skills/{skill}/outcome")
async def record_skill_outcome_manual(
    skill: str,
    payload: dict,
):
    """Manually record a skill outcome from the dashboard (success | failure |
    unknown). Feeds utility scoring without needing the agent tool."""
    outcome = str(payload.get("outcome") or "").strip()
    if outcome not in {"success", "failure", "unknown"}:
        raise HTTPException(status_code=400, detail="outcome must be success|failure|unknown")
    error_type = str(payload.get("error_type") or "").strip() or None
    try:
        from tools.skill_usage import record_outcome

        utility = record_outcome(skill, outcome, error_type=error_type)
        return {"ok": True, "skill": skill, "outcome": outcome, "utility_score": utility}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"record_outcome failed: {exc}")


def _locate_and_patch_skill(skill_name: str, proposal: dict) -> dict:
    """Apply a bounded, section-targeted edit to the skill's SKILL.md.

    Returns ``{"ok": True, "path": ..., "heading": ...}`` or raises
    HTTPException on any guard failure. The edit is bounded to the target
    section so the rest of the skill is untouched.
    """
    import os

    from hermes_constants import get_hermes_home
    from agent.skill_reflection import locate_section

    suggested_fix = str(proposal.get("suggested_fix") or "").strip()
    target_key = str(proposal.get("target_section") or "").strip().lower()
    if not suggested_fix or not target_key:
        raise HTTPException(status_code=400, detail="proposal missing suggested_fix/target_section")

    # Locate the SKILL.md on disk (flat or nested skills/<cat>/<name>/SKILL.md)
    skills_root = get_hermes_home() / "skills"
    candidates = []
    if skills_root.exists():
        for p in skills_root.rglob("SKILL.md"):
            rel = p.parent
            if rel.name == skill_name or (rel.parent.name == skill_name):
                candidates.append(p)
        # Also match frontmatter name
        if not candidates:
            for p in skills_root.rglob("SKILL.md"):
                try:
                    text = p.read_text(encoding="utf-8")
                except Exception:
                    continue
                m = re.search(r"^name:\s*([^\s]+)", text, re.MULTILINE)
                if m and m.group(1) == skill_name:
                    candidates.append(p)
    if not candidates:
        raise HTTPException(status_code=404, detail=f"skill {skill_name!r} not found on disk")

    path = candidates[0]
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot read {path}: {exc}")

    loc = locate_section(content, target_key)
    if loc is None:
        # Section missing → append a new section before the next heading
        # after the intro, or at the very end. Bounded: only append.
        append = f"\n{locate_section_heading(target_key)}\n- {suggested_fix}\n"
        new_content = content.rstrip() + "\n" + append
        heading = locate_section_heading(target_key)
        section_span = None
    else:
        # Insert the fix line inside the existing section (before next heading)
        heading = loc["heading"]
        section_body_end = loc["end"]
        # Insert after the heading line
        insert_at = loc["start"] + len(heading) + 1
        new_content = (
            content[:insert_at]
            + f"- {suggested_fix}\n"
            + content[insert_at:]
        )
        section_span = {"start": loc["start"], "end": section_body_end}

    # Guard: refuse to run the write if the skill is external/hub-owned
    try:
        from tools.skill_usage import is_curation_eligible, is_external_skill_path
        if is_external_skill_path(path):
            raise HTTPException(status_code=403, detail="skill is externally owned; read-only")
    except HTTPException:
        raise
    except Exception:
        pass  # guard absence is not a blocker

    try:
        path.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot write {path}: {exc}")

    return {
        "ok": True,
        "path": str(path),
        "heading": heading,
        "section_span": section_span,
        "fix": suggested_fix,
    }


def locate_section_heading(target_key: str) -> str:
    """Return the canonical markdown heading for a targetable section key."""
    return {
        "when to use": "## When to Use",
        "prerequisites": "## Prerequisites",
        "procedure": "## Procedure",
        "pitfalls": "## Pitfalls",
        "verification": "## Verification",
        "quick reference": "## Quick Reference",
    }.get(target_key, "## Pitfalls")


@router.post("/api/skills/evolution/proposals/{skill}/{proposal_id}/approve")
async def approve_proposal(skill: str, proposal_id: str):
    """Approve a proposal: apply the bounded edit, then mark it applied."""
    try:
        from agent.skill_reflection import list_proposals, mark_proposal_status
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"reflection module unavailable: {exc}")

    rows = [p for p in list_proposals(skill) if p.get("proposal_id") == proposal_id]
    if not rows:
        raise HTTPException(status_code=404, detail="proposal not found")
    proposal = rows[0]
    if proposal.get("status") not in (None, "pending", "reviewed"):
        raise HTTPException(status_code=409, detail=f"proposal already {proposal.get('status')}")

    result = _locate_and_patch_skill(skill, proposal)
    mark_proposal_status(skill, proposal_id, "applied")
    return {"ok": True, **result}


@router.post("/api/skills/evolution/proposals/{skill}/{proposal_id}/reject")
async def reject_proposal(skill: str, proposal_id: str):
    """Reject a proposal (kept for audit, not applied)."""
    try:
        from agent.skill_reflection import mark_proposal_status
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"reflection module unavailable: {exc}")
    if not mark_proposal_status(skill, proposal_id, "rejected"):
        raise HTTPException(status_code=404, detail="proposal not found")
    return {"ok": True, "status": "rejected"}


@router.post("/api/skills/evolution/proposals/{skill}/{proposal_id}/reviewed")
async def mark_proposal_reviewed(skill: str, proposal_id: str):
    """Mark a proposal reviewed without applying it (audit trail)."""
    try:
        from agent.skill_reflection import mark_proposal_status
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"reflection module unavailable: {exc}")
    if not mark_proposal_status(skill, proposal_id, "reviewed"):
        raise HTTPException(status_code=404, detail="proposal not found")
    return {"ok": True, "status": "reviewed"}
