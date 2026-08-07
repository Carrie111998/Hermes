"""Tracker adapter — reproducible platform defects only; normal JobFlow work
items are NOT delegation material."""
from devflow_delegation.emitter import DelegationResult


def delegate_tracker_defect(emitter, finding: dict) -> DelegationResult:
    if not str(finding.get("repro") or "").strip():
        return DelegationResult("declined", reason="not_reproducible")
    return emitter.delegate(
        source={"agent": "tracker", "kind": "tracker",
                "finding_id": str(finding.get("finding_id") or finding.get("title", "")[:40])},
        kind="bug",
        title=str(finding.get("title") or "tracker defect")[:160],
        problem_statement=str(finding.get("problem_statement") or ""),
        evidence=list(finding.get("evidence") or []),
        acceptance_criteria=list(finding.get("acceptance_criteria") or []),
        target=finding.get("target"),
        severity=finding.get("severity", "medium"),
        priority=finding.get("priority", "P2"),
        confidence=float(finding.get("confidence", 0.7)),
        idempotency_key=finding.get("idempotency_key"),
    )
