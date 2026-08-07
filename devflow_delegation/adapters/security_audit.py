"""Security-audit adapter — verified affected-version findings with a
remediation path only. HOLD / no-fix records never re-queue. SECURITY NOTE:
this adapter forwards ONLY structured fields (package, CVE, versions,
remediation text) — it never accepts or forwards credential values or nested
credential containers (spec Security invariant 4)."""
from devflow_delegation.emitter import DelegationResult

_HOLD = {"HOLD", "NO_FIX", "WONTFIX"}


def delegate_security_finding(emitter, finding: dict) -> DelegationResult:
    if finding.get("hold") or str(finding.get("remediation_status") or "").upper() in _HOLD:
        return DelegationResult("declined", reason="hold_no_fix")
    package = str(finding.get("package") or "").strip()
    cve = str(finding.get("cve") or "").strip()
    affected = finding.get("affected_versions")
    fixed = finding.get("fixed_version")
    remediation = str(finding.get("remediation") or "").strip()
    if not package or not cve or not affected or not fixed or not remediation:
        return DelegationResult("declined", reason="unverified_finding")
    return emitter.delegate(
        source={"agent": "security-audit", "kind": "security-audit", "finding_id": cve},
        kind="bug",
        title=f"{cve}: upgrade {package} to {fixed}"[:160],
        problem_statement=(
            f"{package} is affected by {cve} in versions {affected}; fixed in {fixed}. "
            f"Remediation: {remediation}"),
        evidence=[{"kind": "advisory", "ref": str(finding.get("advisory_ref") or cve),
                   "summary": f"{package} affected by {cve}; fixed in {fixed}"}],
        acceptance_criteria=[
            f"{package} upgraded to >= {fixed}",
            "Patched version verified against the advisory before closing",
        ],
        target=finding.get("target") or {"repo": "hermes", "subsystem": "security"},
        severity=finding.get("severity", "high"),
        priority=finding.get("priority", "P1"),
        confidence=0.9,
        idempotency_key=f"security:{cve.lower()}:{package.lower()}:v1",
    )
