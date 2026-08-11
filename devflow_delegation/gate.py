"""Shadow-first autonomy gate for the DevFlow Delegation Plane.

The gate is a deterministic decision surface only. It has no import or call path
to Git, a PR client, lifecycle.transition, deployment tooling, cron management,
or gateway control. In particular, it never creates or changes the operator-owned
``.autonomy_enabled`` sentinel. Stage 3 records evidence now; merge and deploy
execution remain deliberately unimplemented.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from devflow_delegation.allowlist import TargetConfig
from devflow_delegation.risk import RiskAssessment, RiskInput, assess_risk


_RISK_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
_SUPPORTED_ACTIONS = frozenset({"merge", "deploy"})


@dataclass(frozen=True)
class GateRequest:
    """One requested autonomy decision, with only observed risk facts."""

    request_id: str
    action: str
    risk_input: RiskInput


@dataclass(frozen=True)
class GateDecision:
    """A non-mutating, auditable authorization result.

    ``eligible`` answers whether the proposed action passes deterministic
    preconditions. ``authorized`` is intentionally always false in this stage:
    no merge or deploy executor exists here, even when the operator sentinel is
    present. ``mode`` tells the operator whether the sentinel was observed.
    """

    request_id: str
    action: str
    mode: str
    eligible: bool
    authorized: bool
    reason: str
    risk: RiskAssessment


def _sentinel_mode(sentinel_path: Path) -> str:
    return "enabled" if Path(sentinel_path).is_file() else "shadow"


def _within_risk_ceiling(assessment: RiskAssessment, ceiling: str) -> bool:
    return _RISK_RANK.get(assessment.tier, 99) <= _RISK_RANK.get(str(ceiling).lower(), 0)


def evaluate_gate(
    target: TargetConfig,
    request: GateRequest,
    *,
    sentinel_path: Path,
    actions_used: int = 0,
    max_actions: int = 1,
) -> GateDecision:
    """Evaluate a proposed merge/deploy action without taking it.

    Decision order is fail-closed and stable for audit records: unknown action,
    live gateway target, invalid budget, exhausted budget, hard risk block, risk
    ceiling, then shadow/enabled posture. An enabled sentinel only changes the
    reporting mode; it does not grant execution authority.
    """
    assessment = assess_risk(target, request.risk_input)
    mode = _sentinel_mode(sentinel_path)

    action = str(request.action or "").strip().lower()
    if action not in _SUPPORTED_ACTIONS:
        return GateDecision(request.request_id, action, mode, False, False, "unsupported_action", assessment)
    if target.live_gateway_imports:
        return GateDecision(request.request_id, action, mode, False, False, "live_gateway_target", assessment)
    if isinstance(actions_used, bool) or isinstance(max_actions, bool) or actions_used < 0 or max_actions <= 0:
        return GateDecision(request.request_id, action, mode, False, False, "invalid_window_budget", assessment)
    if actions_used >= max_actions:
        return GateDecision(request.request_id, action, mode, False, False, "window_budget_exhausted", assessment)
    if assessment.blocked:
        return GateDecision(request.request_id, action, mode, False, False, "risk_evidence_blocked", assessment)
    if not _within_risk_ceiling(assessment, target.risk_ceiling):
        return GateDecision(request.request_id, action, mode, False, False, "risk_above_ceiling", assessment)
    if mode == "shadow":
        return GateDecision(request.request_id, action, mode, True, False, "shadow_mode", assessment)

    # Deliberate Stage-3 stop: observing the sentinel never creates authority.
    # Any future executor must have a separate user-approved design and tests.
    reason = "merge_authority_not_implemented" if action == "merge" else "deploy_authority_not_implemented"
    return GateDecision(request.request_id, action, mode, True, False, reason, assessment)


def _decision_ref(decision: GateDecision) -> str:
    """Stable, compact artifact reference for one immutable gate result."""
    canonical = json.dumps(asdict(decision), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{decision.mode}:{decision.action}:{decision.risk.tier}:{decision.reason}:{digest}"


def record_shadow_decision(ledger, decision: GateDecision):
    """Durably record a non-mutating gate decision as an idempotent artifact.

    The artifact is audit evidence only. This helper deliberately does not look
    up, mutate, or transition the request, and it never writes the sentinel.
    Callers that expose this through an operator CLI must validate request
    existence before calling it; the lightweight test seam also supports an
    isolated ledger without a seeded request.
    """
    return ledger.add_artifact(decision.request_id, "autonomy_gate", _decision_ref(decision))
