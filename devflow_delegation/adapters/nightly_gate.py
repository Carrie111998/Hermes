"""Nightly-gate adapter — named deterministic failing gates after the gate's
own retry/flake policy. A generic RED headline is insufficient (spec): the
finding must name a culprit and the failed command; output is bounded."""
from devflow_delegation.emitter import DelegationResult

MAX_OUTPUT_CHARS = 4000


def delegate_gate_failure(emitter, report: dict) -> DelegationResult:
    culprit = str(report.get("culprit") or "").strip()
    command = str(report.get("failed_command") or "").strip()
    if not culprit or not command:
        return DelegationResult("declined", reason="generic_red_headline")
    output = str(report.get("output") or "")[:MAX_OUTPUT_CHARS]
    return emitter.delegate(
        source={"agent": "nightly-gate", "kind": "nightly-gate", "finding_id": culprit},
        kind="bug",
        title=f"nightly-gate failure: {culprit}"[:160],
        problem_statement=f"Gate `{culprit}` failed.\nCommand: {command}\nBounded output:\n{output}",
        evidence=[{"kind": "test_failure", "ref": command, "summary": output[:400] or culprit}],
        acceptance_criteria=[f"Nightly gate `{culprit}` passes deterministically (two consecutive green runs)."],
        target=report.get("target") or {"repo": "hermes", "subsystem": str(report.get("subsystem") or "nightly-gate")},
        severity="high",
        priority=report.get("priority", "P1"),
        confidence=0.9,
        idempotency_key=report.get("idempotency_key") or f"nightly-gate:{culprit.lower()}:v1",
    )
