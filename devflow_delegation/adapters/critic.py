"""Critic adapter — actionable, evidence-backed proposals and confirmed
defects only. Commentary and rejected evidence never delegate (spec:
"Detector adapters > Critic")."""
from devflow_delegation.emitter import DelegationResult

_ACTIONABLE = {"proposal", "defect"}


def delegate_critic_finding(emitter, finding: dict) -> DelegationResult:
    if finding.get("kind") not in _ACTIONABLE:
        return DelegationResult("declined", reason="non_actionable")
    finding_id = finding.get("proposal_id") or finding.get("finding_id")
    if not finding_id:
        return DelegationResult("declined", reason="missing_finding_id")
    safety = tuple(finding.get("safety_notes") or ())
    if finding.get("reversal_path"):
        safety = safety + (f"Reversal evidence: {finding['reversal_path']}",)
    return emitter.delegate(
        source={"agent": "critic", "kind": "critic", "finding_id": str(finding_id)},
        kind="improvement" if finding["kind"] == "proposal" else "bug",
        title=str(finding.get("title") or finding_id)[:160],
        problem_statement=str(finding.get("problem_statement") or finding.get("summary") or ""),
        evidence=list(finding.get("evidence") or []),
        acceptance_criteria=list(finding.get("acceptance_criteria") or []),
        target=finding.get("target"),
        severity=finding.get("severity", "medium"),
        priority=finding.get("priority", "P2"),
        confidence=finding.get("confidence", 0.0),
        proposed_approach=finding.get("proposed_approach"),
        safety_notes=safety,
        idempotency_key=finding.get("idempotency_key") or f"critic:{finding_id}:v1",
    )
