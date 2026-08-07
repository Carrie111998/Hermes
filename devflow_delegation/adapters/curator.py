"""Curator adapter — actionable system changes only after the curator's own
evidence threshold is met upstream."""
from devflow_delegation.emitter import DelegationResult


def delegate_curator_change(emitter, finding: dict) -> DelegationResult:
    if not finding.get("meets_evidence_threshold"):
        return DelegationResult("declined", reason="below_evidence_threshold")
    return emitter.delegate(
        source={"agent": "curator", "kind": "curator",
                "finding_id": str(finding.get("finding_id") or finding.get("title", "")[:40])},
        kind="improvement",
        title=str(finding.get("title") or "curator change")[:160],
        problem_statement=str(finding.get("problem_statement") or ""),
        evidence=list(finding.get("evidence") or []),
        acceptance_criteria=list(finding.get("acceptance_criteria") or []),
        target=finding.get("target"),
        severity=finding.get("severity", "low"),
        priority=finding.get("priority", "P3"),
        confidence=float(finding.get("confidence", 0.7)),
        idempotency_key=finding.get("idempotency_key"),
    )
