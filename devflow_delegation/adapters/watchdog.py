"""Watchdog adapter — persistent or state-transition component failures at
high/critical severity only. INFO and single transient misses never delegate.
Recoveries update/close the existing request via dedup rather than opening a
new one (the fingerprint carries the component identity)."""
from devflow_delegation.emitter import DelegationResult

_SEVERITIES = {"high", "critical"}


def delegate_watchdog_alert(emitter, alert: dict) -> DelegationResult:
    severity = str(alert.get("severity") or "").lower()
    if severity not in _SEVERITIES:
        return DelegationResult("declined", reason="below_severity")
    if not (alert.get("persistent") or alert.get("state_transition")):
        return DelegationResult("declined", reason="transient")
    component = str(alert.get("component") or "unknown")
    detail = str(alert.get("detail") or "")
    return emitter.delegate(
        source={"agent": "watchdog", "kind": "watchdog",
                "finding_id": str(alert.get("alert_id") or component)},
        kind="bug",
        title=f"watchdog: {component} failure"[:160],
        problem_statement=detail or f"{component} reported a persistent failure.",
        evidence=[{"kind": "watchdog_alert", "ref": str(alert.get("alert_id") or ""),
                   "summary": (detail or component)[:400]}],
        acceptance_criteria=[
            f"{component} returns to healthy state and stays stable across two consecutive watchdog sweeps."
        ],
        target=alert.get("target") or {"repo": "hermes", "subsystem": component},
        severity=severity,
        priority=alert.get("priority", "P2"),
        confidence=float(alert.get("confidence", 0.85)),
        idempotency_key=alert.get("idempotency_key") or f"watchdog:{component.lower()}:{severity}:v1",
    )
