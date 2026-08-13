"""Wisdom v1 — curator weekly share-candidate pass (PRD 1, M0).

Scores the user's own skills from usage analytics and nominates share
candidates. Dry-run only in M0: the pass produces a candidate list with
evidence lines and persists it; nothing is proposed or shared.

Design notes:
  - Scoring is deliberately simple and inspectable (one screen of constants
    and one function). V1 is a feedback-gathering exercise; a scoring model
    nobody can read defeats the purpose.
  - The pass never blocks the agent. Gate-and-swallow, matching the shape
    of ``skills_sync_client.maybe_pull_skills`` and
    ``agent.curator.maybe_run_curator``.
  - Scheduling mirrors the curator: a state file tracks the last run; the
    pass fires at most once per ``WISDOM_SHARE_INTERVAL_HOURS`` (default
    168 = 7 days) when invoked from the curator tick sites.
  - Declined candidates are persisted so the same skill is not re-nominated
    to the same owner.

State file: ``~/.hermes/skills/.wisdom_share_state`` (JSON).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scoring constants — one screen, named, inspectable.
# ---------------------------------------------------------------------------

#: A skill needs at least this many qualifying uses inside the recency
#: window to be nominated. Below the floor the signal is noise.
SCORE_FLOOR_USES = 3

#: Uses within this many days count at full weight.
RECENCY_FULL_DAYS = 14

#: Uses older than this many days count at zero weight.
RECENCY_ZERO_DAYS = 60

#: Patch count multiplier — a skill the agent keeps refining is alive.
PATCH_WEIGHT = 0.5

#: Maximum candidates surfaced per pass.
TOP_N = 3

#: Minimum hours between passes (168 = 7 days).
WISDOM_SHARE_INTERVAL_HOURS = 168


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _state_file() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "skills" / ".wisdom_share_state"


def _default_state() -> Dict[str, Any]:
    return {
        "last_run_at": None,
        "declined": [],
        "last_candidates": [],
        "run_count": 0,
    }


def load_state() -> Dict[str, Any]:
    path = _state_file()
    if not path.exists():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base = _default_state()
            base.update({k: v for k, v in data.items() if k in base})
            return base
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("wisdom_share_pass: failed to read state: %s", e)
    return _default_state()


def save_state(data: Dict[str, Any]) -> None:
    path = _state_file()
    try:
        from utils import atomic_json_write

        atomic_json_write(path, data, indent=2, sort_keys=True)
    except Exception as e:
        logger.debug("wisdom_share_pass: failed to save state: %s", e)


def decline_candidate(skill_name: str) -> None:
    """Persist a decline so the skill is not re-nominated to this owner."""
    state = load_state()
    declined = state.get("declined", [])
    if skill_name not in declined:
        declined.append(skill_name)
        state["declined"] = declined
        save_state(state)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _recency_weight(last_used_at: Optional[str], now: datetime) -> float:
    """Linear decay from 1.0 at RECENCY_FULL_DAYS to 0.0 at RECENCY_ZERO_DAYS."""
    if not last_used_at:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(last_used_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0.0
    days = (now - dt).days
    if days <= RECENCY_FULL_DAYS:
        return 1.0
    if days >= RECENCY_ZERO_DAYS:
        return 0.0
    return 1.0 - (days - RECENCY_FULL_DAYS) / (RECENCY_ZERO_DAYS - RECENCY_FULL_DAYS)


def score_skill(record: Dict[str, Any], now: datetime) -> float:
    """Score one usage record. Higher is more share-worthy.

    uses * recency_weight + patches * PATCH_WEIGHT
    """
    uses = record.get("use_count", 0) or 0
    patches = record.get("patch_count", 0) or 0
    recency = _recency_weight(record.get("last_used_at"), now)
    return uses * recency + patches * PATCH_WEIGHT


def _evidence_line(name: str, record: Dict[str, Any], score: float, now: datetime) -> str:
    """Human-readable justification for a nomination."""
    uses = record.get("use_count", 0) or 0
    patches = record.get("patch_count", 0) or 0
    last = record.get("last_used_at")
    days_str = "never"
    if last:
        try:
            dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            days = (now - dt).days
            if days == 0:
                days_str = "today"
            elif days == 1:
                days_str = "yesterday"
            else:
                days_str = f"{days} days ago"
        except (ValueError, TypeError):
            pass
    parts = [f"used {uses} times"]
    if patches:
        parts.append(f"patched {patches} time{'s' if patches != 1 else ''}")
    parts.append(f"last used {days_str}")
    return f"{name}: {', '.join(parts)}"


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def run_share_pass(*, dry_run: bool = True, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Score skills and produce share candidates. Never raises.

    Args:
        dry_run: When True (default in M0), the pass only reports — it does
            not propose anything. When False, the caller is responsible for
            what happens next (M1: present to owner for approval).
        now: Injectable clock for tests.

    Returns:
        {ok, candidates: [{name, score, evidence, uses, patches, last_used_at}],
         skipped_declined: [names], skipped_below_floor: int, run_at: iso}
    """
    try:
        if now is None:
            now = datetime.now(timezone.utc)

        from tools.skill_usage import load_usage, provenance

        data = load_usage()
        state = load_state()
        declined = set(state.get("declined", []))

        scored: List[tuple] = []
        skipped_declined: List[str] = []
        skipped_below_floor = 0

        for name, rec in data.items():
            if not isinstance(rec, dict):
                continue
            # Only agent/user-authored skills are eligible (not bundled/hub).
            if provenance(name) != "agent":
                continue
            if name in declined:
                skipped_declined.append(name)
                continue
            s = score_skill(rec, now)
            uses = rec.get("use_count", 0) or 0
            recency = _recency_weight(rec.get("last_used_at"), now)
            qualifying_uses = uses * recency
            if qualifying_uses < SCORE_FLOOR_USES:
                skipped_below_floor += 1
                continue
            scored.append((s, name, rec))

        scored.sort(key=lambda t: t[0], reverse=True)
        candidates = []
        for s, name, rec in scored[:TOP_N]:
            candidates.append(
                {
                    "name": name,
                    "score": round(s, 1),
                    "evidence": _evidence_line(name, rec, s, now),
                    "uses": rec.get("use_count", 0) or 0,
                    "patches": rec.get("patch_count", 0) or 0,
                    "last_used_at": rec.get("last_used_at"),
                }
            )

        # Persist state.
        state["last_run_at"] = now.isoformat()
        state["last_candidates"] = [c["name"] for c in candidates]
        state["run_count"] = int(state.get("run_count", 0)) + 1
        save_state(state)

        return {
            "ok": True,
            "dry_run": dry_run,
            "candidates": candidates,
            "skipped_declined": sorted(skipped_declined),
            "skipped_below_floor": skipped_below_floor,
            "run_at": now.isoformat(),
        }
    except Exception as e:
        logger.debug("wisdom_share_pass: run_share_pass failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e), "candidates": []}


def maybe_run_share_pass(*, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Best-effort: run the share pass if the interval has elapsed. Never raises.

    Called from the curator tick sites (cli.py startup + gateway housekeeping
    loop), same gate-and-swallow shape as maybe_pull_skills.
    """
    try:
        if now is None:
            now = datetime.now(timezone.utc)

        state = load_state()
        last = state.get("last_run_at")
        if last:
            try:
                last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                if (now - last_dt) < timedelta(hours=WISDOM_SHARE_INTERVAL_HOURS):
                    return None
            except (ValueError, TypeError):
                pass  # corrupt timestamp — run anyway

        return run_share_pass(dry_run=True, now=now)
    except Exception as e:
        logger.debug("wisdom_share_pass: maybe_run_share_pass failed: %s", e, exc_info=True)
        return None
