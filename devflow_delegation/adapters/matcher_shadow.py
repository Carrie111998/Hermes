"""matcher-shadow adapter — same evidence-threshold posture as the curator."""
from devflow_delegation.emitter import DelegationResult


def delegate_matcher_shadow_change(emitter, finding: dict) -> DelegationResult:
    if not finding.get("meets_evidence_threshold"):
        return DelegationResult("declined", reason="below_evidence_threshold")
    return emitter.delegate(
        source={"agent": "matcher-shadow", "kind": "matcher-shadow",
                "finding_id": str(finding.get("finding_id") or finding.get("title", "")[:40])},
        kind="improvement",
        title=str(finding.get("title") or "matcher-shadow change")[:160],
        problem_statement=str(finding.get("problem_statement") or ""),
        evidence=list(finding.get("evidence") or []),
        acceptance_criteria=list(finding.get("acceptance_criteria") or []),
        target=finding.get("target"),
        severity=finding.get("severity", "low"),
        priority=finding.get("priority", "P3"),
        confidence=float(finding.get("confidence", 0.7)),
        idempotency_key=finding.get("idempotency_key"),
    )
