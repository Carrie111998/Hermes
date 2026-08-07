"""Source policy — thresholds, dedup cooldowns, rolling rate limits, budgets.

Pure decision functions + optional JSON overrides (missing file = defaults).
Every source ships mode='dry_run' until rollout evidence flips it to
'queue' — the code never flips itself (operator-owned config, spec Security
invariant 1).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict

GLOBAL_MAX_PER_WINDOW = 40
GLOBAL_WINDOW_HOURS = 24
CRITICAL_BUDGET_PER_WINDOW = 3
CRITICAL_BUDGET_WINDOW_HOURS = 24

_POLICY_FIELDS = {
    "mode", "min_confidence", "min_severity", "max_per_window", "window_hours",
    "cooldown_success_hours", "cooldown_declined_hours",
}


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class SourcePolicy:
    mode: str = "dry_run"                 # "dry_run" | "queue"
    min_confidence: float = 0.0
    min_severity: str = "low"
    max_per_window: int = 10
    window_hours: int = 24
    cooldown_success_hours: int = 168     # 7 days after a successful terminal
    cooldown_declined_hours: int = 24


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


def _sp(**kw) -> SourcePolicy:
    return SourcePolicy(**kw)


# Representative defaults (spec: "Source thresholds"). Watchdog/nightly-gate/
# security-audit carry higher confidence floors because their adapters only
# forward already-verified findings.
DEFAULT_SOURCE_POLICIES: Dict[str, SourcePolicy] = {
    "explicit": _sp(min_confidence=0.0, min_severity="low", max_per_window=10),
    "critic": _sp(min_confidence=0.5, min_severity="low", max_per_window=10),
    "arch-review": _sp(min_confidence=0.5, min_severity="low", max_per_window=20),
    "watchdog": _sp(min_confidence=0.7, min_severity="high", max_per_window=6),
    "nightly-gate": _sp(min_confidence=0.7, min_severity="medium", max_per_window=6),
    "security-audit": _sp(min_confidence=0.8, min_severity="medium", max_per_window=6),
    "tracker": _sp(min_confidence=0.6, min_severity="medium", max_per_window=6),
    "curator": _sp(min_confidence=0.6, min_severity="low", max_per_window=6),
    "matcher-shadow": _sp(min_confidence=0.6, min_severity="low", max_per_window=6),
}

_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def load_policy_overrides(path: Path) -> Dict[str, dict]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError(f"policy file unreadable/malformed: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError("policy file malformed: expected a JSON object")
    for source, fields in data.items():
        if not isinstance(fields, dict):
            raise PolicyError(f"policy override for '{source}' must be an object")
        unknown = set(fields) - _POLICY_FIELDS
        if unknown:
            raise PolicyError(f"policy override for '{source}' has unknown fields: {sorted(unknown)}")
    return data


def build_policy(overrides: Dict[str, dict]) -> Dict[str, SourcePolicy]:
    merged = dict(DEFAULT_SOURCE_POLICIES)
    for source, fields in overrides.items():
        base = merged.get(source, DEFAULT_SOURCE_POLICIES["explicit"])
        merged[source] = replace(base, **fields)
    return merged


def policy_for(policy_map: Dict[str, SourcePolicy], source_kind: str) -> SourcePolicy:
    return policy_map.get(source_kind, policy_map["explicit"])


def evaluate_source_threshold(policy: SourcePolicy, *, confidence: float, severity: str) -> PolicyDecision:
    if confidence < policy.min_confidence:
        return PolicyDecision(False, "below_confidence")
    if _SEVERITY_RANK.get(severity, 0) < _SEVERITY_RANK.get(policy.min_severity, 0):
        return PolicyDecision(False, "below_severity")
    return PolicyDecision(True, "ok")


def check_rate_limits(
    policy: SourcePolicy,
    *,
    source_count: int,
    global_count: int,
    critical_used: int,
    is_critical: bool,
) -> PolicyDecision:
    """Rolling-window limits. Critical findings may bypass the SOURCE count
    limit by consuming the separate critical budget; they never bypass the
    global limit, target allowlisting, or evidence requirements."""
    if global_count >= GLOBAL_MAX_PER_WINDOW:
        return PolicyDecision(False, "rate_limit_global")
    if source_count >= policy.max_per_window:
        if is_critical and critical_used < CRITICAL_BUDGET_PER_WINDOW:
            return PolicyDecision(True, "ok_critical_budget")
        if is_critical:
            return PolicyDecision(False, "critical_budget_exhausted")
        return PolicyDecision(False, "rate_limit_source")
    return PolicyDecision(True, "ok")


def in_cooldown(*, policy: SourcePolicy, terminal_state: str, terminal_updated_at_iso: str, now: datetime) -> bool:
    """Success terminals (MERGED/AUTO_MERGED/DEPLOYED) and DECLINED rows gate
    re-opens of the same fingerprint. Other terminals (FAILED etc.) may be
    re-attempted immediately."""
    try:
        updated = datetime.fromisoformat(terminal_updated_at_iso)
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    if terminal_state in {"MERGED", "AUTO_MERGED", "DEPLOYED"}:
        return now - updated < timedelta(hours=policy.cooldown_success_hours)
    if terminal_state == "DECLINED":
        return now - updated < timedelta(hours=policy.cooldown_declined_hours)
    return False


def first_rate_limit_crossing(source_count: int, limit: int) -> bool:
    """True exactly for the FIRST request over the limit — the single request
    allowed to carry the summarized suppression alert (spec: 'emit one
    summarized alert, not one alert per dropped request')."""
    return source_count == limit
