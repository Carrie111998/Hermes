"""Failure-driven skill reflection loop (2026-08).

Implements the "挖掘失败 → 有界编辑 → 验证门控" pattern from the
Self-Harness literature, adapted to Hermes' skill system:

  1.  Candidates: skills with a low utility score (failure-heavy) are
      surfaced via :func:`tools.skill_usage.list_low_utility_skills`.
  2.  Diagnosis: for each candidate, aggregate the recorded error types
      from the outcome telemetry and locate the relevant SKILL.md section.
  3.  Proposal: generate a *bounded* improvement proposal — a targeted
      patch to a specific section (Pitfalls / Verification / Procedure)
      rather than a full rewrite. Bounded edits keep the change auditable
      and reversible, mirroring Self-Harness' constraint that an agent
      only proposes changes it can validate.
  4.  Gate: proposals are *never* applied automatically. They are written
      to a proposals queue (~/.hermes/skills/.reflection_proposals/) that
      the Curator or the user reviews — the same "human in the loop"
      guarantee Hermes already enforces for skill_manage writes.

This module is deliberately deterministic and offline-friendly: it does
NOT call an LLM by itself. The caller (an agent turn, a cron pass, or the
Curator) supplies the diagnosis text; this module handles persistence,
section targeting, and the proposals queue. Keeping the LLM out of this
module makes it unit-testable and safe to run in constrained contexts.
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# Proposals live under the skills dir so they are visible to the Curator's
# existing scans but excluded from skill discovery (dot-prefixed).
_PROPOSALS_DIRNAME = ".reflection_proposals"

# SKILL.md sections that bounded edits may target.
_TARGETABLE_SECTIONS = {
    "when to use": "## When to Use",
    "prerequisites": "## Prerequisites",
    "procedure": "## Procedure",
    "pitfalls": "## Pitfalls",
    "verification": "## Verification",
    "quick reference": "## Quick Reference",
}

MAX_PROPOSALS_PER_SKILL = 20  # bound the queue per skill (incl. history)


def _proposals_dir() -> Path:
    return get_hermes_home() / "skills" / _PROPOSALS_DIRNAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _proposal_path(skill_name: str, proposal_id: str) -> Path:
    return _proposals_dir() / f"{skill_name}__{proposal_id}.json"


def _safe_proposal_id() -> str:
    return _now_iso().replace(":", "").replace("+", "").replace("-", "")[:17]


def locate_section(skill_md: str, target: str) -> Optional[Dict[str, Any]]:
    """Locate a targetable section in SKILL.md content.

    Returns ``{"heading": "...", "start": int, "end": int}`` (character
    offsets) or None when the section does not exist. Offsets let the
    caller patch precisely without touching the rest of the skill.
    """
    key = (target or "").strip().lower()
    heading = _TARGETABLE_SECTIONS.get(key)
    if not heading or not skill_md:
        return None
    idx = skill_md.find(heading)
    if idx < 0:
        return None
    start = idx
    # Section ends at the next markdown heading (## or #) or EOF
    rest = skill_md[idx + len(heading):]
    end = start + len(heading)
    for match in ("\n## ", "\n# "):
        rel = rest.find(match)
        if rel >= 0:
            cand = start + len(heading) + rel
            if cand > end:
                end = cand
                break
    return {"heading": heading, "start": start, "end": end}


def build_proposal(
    skill_name: str,
    *,
    diagnosis: str,
    target_section: str,
    suggested_fix: str,
    failure_types: Optional[List[str]] = None,
    utility_score: Optional[float] = None,
    source: str = "reflection-loop",
) -> Dict[str, Any]:
    """Build a bounded improvement proposal dict (no I/O).

    ``target_section`` must be one of the targetable section keys
    ("when to use", "prerequisites", "procedure", "pitfalls",
    "verification", "quick reference"). ``diagnosis`` explains WHY the
    skill fails; ``suggested_fix`` is the concrete bounded edit.
    """
    target_key = (target_section or "").strip().lower()
    if target_key not in _TARGETABLE_SECTIONS:
        raise ValueError(
            f"target_section must be one of {sorted(_TARGETABLE_SECTIONS)}, got {target_section!r}"
        )
    return {
        "skill": skill_name,
        "proposal_id": _safe_proposal_id(),
        "created_at": _now_iso(),
        "source": source,
        "target_section": target_key,
        "heading": _TARGETABLE_SECTIONS[target_key],
        "diagnosis": diagnosis,
        "suggested_fix": suggested_fix,
        "failure_types": failure_types or [],
        "utility_score": utility_score,
        "status": "pending",  # pending → reviewed → applied | rejected
    }


def save_proposal(proposal: Dict[str, Any]) -> Optional[Path]:
    """Persist a proposal to the queue (atomic write). Returns path or None."""
    skill_name = str(proposal.get("skill") or "").strip()
    proposal_id = str(proposal.get("proposal_id") or "").strip()
    if not skill_name or not proposal_id:
        return None
    # Bound the queue per skill
    existing = list_proposals(skill_name)
    if len(existing) >= MAX_PROPOSALS_PER_SKILL:
        # Drop the oldest pending proposal to keep the queue bounded
        existing.sort(key=lambda p: p.get("created_at", ""))
        for old in existing:
            if old.get("status") == "pending":
                _delete_proposal_file(skill_name, str(old.get("proposal_id")))
                break
    path = _proposal_path(skill_name, proposal_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".prop_", suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(proposal, f, indent=2, sort_keys=True, ensure_ascii=False)
                f.flush()
                import os
                os.fsync(f.fileno())
            import os as _os
            _os.replace(tmp, path)
            return path
        except BaseException:
            try:
                import os as _os
                _os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.debug("save_proposal(%s) failed: %s", skill_name, e, exc_info=True)
        return None


def _delete_proposal_file(skill_name: str, proposal_id: str) -> bool:
    try:
        path = _proposal_path(skill_name, proposal_id)
        if path.exists():
            path.unlink()
            return True
    except OSError:
        pass
    return False


def list_proposals(skill_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """List proposals in the queue, newest first. Optionally filter by skill."""
    base = _proposals_dir()
    if not base.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for path in sorted(base.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if skill_name and data.get("skill") != skill_name:
            continue
        rows.append(data)
    return rows


def list_pending_proposals(skill_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """List only pending (unreviewed) proposals, newest first."""
    return [p for p in list_proposals(skill_name) if p.get("status") == "pending"]


def mark_proposal_status(skill_name: str, proposal_id: str, status: str) -> bool:
    """Update a proposal's status: 'reviewed' | 'applied' | 'rejected'.

    All statuses keep the proposal file for audit + history views; the
    queue is bounded per skill via ``MAX_PROPOSALS_PER_SKILL`` so the
    history can't grow unboundedly. (Applied proposals are *not* deleted —
    the dashboard's applied/rejected tabs rely on them staying visible.)
    """
    if status not in {"reviewed", "applied", "rejected"}:
        return False
    path = _proposal_path(skill_name, proposal_id)
    try:
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = status
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".prop_", suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
                f.flush()
            import os as _os
            _os.replace(tmp, path)
            return True
        except BaseException:
            try:
                import os as _os
                _os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.debug("mark_proposal_status(%s, %s) failed: %s", skill_name, proposal_id, e, exc_info=True)
        return False


def aggregate_failure_patterns(skill_name: str) -> Dict[str, Any]:
    """Aggregate failure signals from the outcome telemetry for a skill.

    Returns a dict with the most common error types and the failure rate.
    Used by the reflection loop to decide WHAT to diagnose.
    """
    try:
        from tools.skill_usage import get_outcome_summary
        summary = get_outcome_summary(skill_name)
    except Exception:
        summary = {}
    outcomes = summary.get("outcomes") or []
    error_counts: Dict[str, int] = {}
    for o in outcomes:
        et = o.get("error_type")
        if et:
            error_counts[str(et)] = error_counts.get(str(et), 0) + 1
    failures = summary.get("failure_count", 0)
    successes = summary.get("success_count", 0)
    total = failures + successes
    return {
        "skill": skill_name,
        "utility_score": summary.get("utility_score"),
        "failure_count": failures,
        "success_count": successes,
        "failure_rate": round(failures / total, 3) if total else None,
        "top_error_types": sorted(error_counts.items(), key=lambda kv: -kv[1])[:5],
        "last_outcome_at": summary.get("last_outcome_at"),
    }


def reflection_candidates(min_score: float = 0.4, limit: int = 10) -> List[Dict[str, Any]]:
    """Return the skills most in need of improvement, with failure patterns.

    Combines :func:`tools.skill_usage.list_low_utility_skills` with
    :func:`aggregate_failure_patterns` so a caller can immediately see
    *which* skills are failing and *why*.
    """
    try:
        from tools.skill_usage import list_low_utility_skills
        low = list_low_utility_skills(max_score=min_score)
    except Exception as e:
        logger.debug("reflection_candidates: %s", e)
        low = []
    rows = []
    for item in low[:limit]:
        name = item.get("skill", "")
        patterns = aggregate_failure_patterns(name)
        rows.append({**item, **patterns})
    return rows
