"""Per-knob apply executor for Critic proposals (Phase D iter2).

Diego clicks "applied" on a proposal in the Control Center. We dispatch by
proposal kind:
  * skill.ranking          -> bumps counters in skills/<skill>/metadata.json
                               (delegates to graphs.critic._execute_skill_ranking)
  * agent.reasoning_effort -> mutates profiles/<agent>/config.yaml under
                               reasoning_effort, with reversal snapshot
  * cron.cadence           -> mutates cron/jobs.json schedule.expr within +/-50%
  * matcher.threshold_adjust -> writes HERMES_JOBFLOW_PROCEED_THRESHOLD or
                                 HERMES_JOBFLOW_REVIEW_THRESHOLD into
                                 ~/.hermes/.env (replacing existing key if any)
  * matcher.prompt_edit / matcher.dimension_weight / structural -> NOT auto-
    applicable; record intent + a placeholder reversal note.

Every successful apply writes:
  ~/.hermes/profiles/critic/workspace/reversals/<ts>_<pid>.json
  ~/.hermes/profiles/critic/workspace/changelog.jsonl  (append entry)

Returns (success, note, reversal_path).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

HERMES = Path.home() / ".hermes"
ALLOWED_KNOBS_PATH = HERMES / "profiles" / "critic" / "allowed_knobs.json"
REVERSALS_DIR = HERMES / "profiles" / "critic" / "workspace" / "reversals"
CHANGELOG = HERMES / "profiles" / "critic" / "workspace" / "changelog.jsonl"
HERMES_ENV = HERMES / ".env"


def _ts() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _record_changelog(entry: dict) -> None:
    CHANGELOG.parent.mkdir(parents=True, exist_ok=True)
    with open(CHANGELOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _write_reversal(pid: str, payload: dict) -> Path:
    REVERSALS_DIR.mkdir(parents=True, exist_ok=True)
    path = REVERSALS_DIR / f"{_ts()}_{pid}_applied.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Per-kind executors
# ---------------------------------------------------------------------------


def _apply_skill_ranking(proposal: dict) -> tuple[bool, str, Optional[Path]]:
    """Delegate to graphs.critic._execute_skill_ranking which already handles
    the full bump-and-reverse logic.
    """
    import importlib.util
    import sys

    # Fallback only — never shadow an active checkout (C26 casualty class):
    # only add the live agent-src tree when ``graphs`` isn't already
    # importable, and append rather than insert(0).
    if importlib.util.find_spec("graphs") is None:
        sys.path.append(str(HERMES / "agent-src"))
    from graphs.critic import _execute_skill_ranking  # type: ignore

    ok, note, record = _execute_skill_ranking(proposal)
    if not ok:
        return False, f"skill.ranking apply failed: {note}", None

    pid = proposal.get("proposal_id", "unknown")
    rev = _write_reversal(pid, {
        "kind": "skill.success_ranking",
        "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "to_revert": {
            "metadata_path": record["metadata_path"],
            "restore_to": {
                "success": record["prior_success"],
                "fail": record["prior_fail"],
            },
        },
    })
    _record_changelog({
        "kind": "skill.success_ranking",
        "proposal_id": pid,
        "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "executed": True,
        "record": record,
        "reversal_path": str(rev),
        "applied_via": "control_center",
    })
    return True, note, rev


def _apply_threshold_adjust(proposal: dict) -> tuple[bool, str, Optional[Path]]:
    """Write the proposed threshold env var into ~/.hermes/.env. Captures the
    PRIOR value (or "unset") for the reversal record.
    """
    spec = proposal.get("specific_change", "")
    m = re.search(r"(HERMES_JOBFLOW_(?:PROCEED|REVIEW)_THRESHOLD)\s*=\s*([0-9.]+)", spec)
    if not m:
        return False, f"could not parse threshold from specific_change: {spec[:80]}", None
    var, new_val = m.group(1), m.group(2)

    if not HERMES_ENV.exists():
        HERMES_ENV.write_text("", encoding="utf-8")
    raw = HERMES_ENV.read_text(encoding="utf-8")
    lines = raw.splitlines()

    prior_val: Optional[str] = None
    found_idx = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith(f"{var}="):
            prior_val = s.split("=", 1)[1]
            found_idx = i
            break
    new_line = f"{var}={new_val}"
    if found_idx >= 0:
        lines[found_idx] = new_line
    else:
        lines.append(new_line)
    HERMES_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")

    pid = proposal.get("proposal_id", "unknown")
    rev = _write_reversal(pid, {
        "kind": "matcher.threshold_adjust",
        "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "to_revert": {
            "env_path": str(HERMES_ENV),
            "var": var,
            "restore_to": prior_val,  # None means unset (delete the line)
        },
    })
    _record_changelog({
        "kind": "matcher.threshold_adjust",
        "proposal_id": pid,
        "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "executed": True,
        "var": var,
        "new_value": new_val,
        "prior_value": prior_val,
        "reversal_path": str(rev),
        "applied_via": "control_center",
    })
    return True, f"set {var}={new_val} (prior={prior_val or 'unset'})", rev


def _record_intent_only(proposal: dict, kind: str) -> tuple[bool, str, Optional[Path]]:
    """For kinds that aren't safely auto-appliable: record that Diego clicked
    'applied' as INTENT, write a placeholder reversal noting the manual change
    needed, append to changelog. Returns (False=not executed, note, rev).
    """
    pid = proposal.get("proposal_id", "unknown")
    rev = _write_reversal(pid, {
        "kind": kind,
        "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "executed": False,
        "note": (
            f"Diego marked {pid} ({kind}) as 'applied' in the Control Center, "
            f"but {kind} is not a Control-Center-auto-applicable kind. "
            f"The change was NOT executed; Diego must apply it manually. "
            f"Reversal: see specific_change in the original proposal."
        ),
        "specific_change": proposal.get("specific_change", ""),
    })
    _record_changelog({
        "kind": kind,
        "proposal_id": pid,
        "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "executed": False,
        "intent_recorded": True,
        "reversal_path": str(rev),
        "applied_via": "control_center",
    })
    return False, f"{kind} is not Control-Center-auto-applicable; intent recorded only", rev


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


SAFE_KINDS = {
    "skill.ranking": _apply_skill_ranking,
    "matcher.threshold_adjust": _apply_threshold_adjust,
}


def execute_apply(proposal: dict) -> tuple[bool, str, Optional[Path]]:
    """Dispatch by proposal['kind']. Returns (executed, note, reversal_path)."""
    if not isinstance(proposal, dict):
        return False, "proposal not a dict", None
    kind = proposal.get("kind", "")
    if not kind:
        return False, "no kind on proposal", None

    fn = SAFE_KINDS.get(kind)
    if fn is not None:
        try:
            return fn(proposal)
        except Exception as exc:
            return False, f"executor for {kind} crashed: {exc}", None

    # Not in SAFE_KINDS — record intent only.
    return _record_intent_only(proposal, kind)
