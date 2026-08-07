"""arch-review adapter — open actionable roadmap rows (SR-*) via the shared
emitter, preserving the legacy 'roadmap:{srid}:v1' idempotency keys so v2
envelopes already queued by roadmap_devflow_intake.py continue to dedup."""
from devflow_delegation.emitter import DelegationResult

_PRIORITY_TO_V3 = {"high": ("P1", "medium"), "medium": ("P2", "low"), "low": ("P3", "low")}


def _roadmap_kwargs(row: dict, roadmap_rel: str) -> dict:
    srid = str(row["id"])
    v3_priority, severity = _PRIORITY_TO_V3.get(row.get("priority", "medium"), ("P2", "low"))
    item = str(row.get("item") or "").strip()
    reversibility = str(row.get("reversibility") or "").strip()
    traces = str(row.get("traces_to") or "").strip()
    return dict(
        source={"agent": "roadmap-intake", "kind": "arch-review", "finding_id": srid},
        kind="task",
        title=f"{srid}: {item}"[:160],
        problem_statement=item + (f"\nRecovery/reversibility: {reversibility}" if reversibility else ""),
        evidence=[{"kind": "roadmap_row", "ref": f"{roadmap_rel}:L{row.get('line', '')}",
                   "summary": (traces or item[:200])}],
        acceptance_criteria=[
            f"Resolve roadmap item {srid} as described in its row text.",
            "Preserve the row's reversibility/recovery constraints: " + (reversibility or "unspecified"),
        ],
        target={"repo": "hermes", "subsystem": "roadmap"},
        severity=severity,
        priority=v3_priority,
        confidence=0.8,
        safety_notes=("Roadmap-sourced; no autonomous merge/deploy.",),
        idempotency_key=f"roadmap:{srid.lower()}:v1",
    )


def delegate_roadmap_row(emitter, row: dict, roadmap_rel: str) -> DelegationResult:
    return emitter.delegate(**_roadmap_kwargs(row, roadmap_rel))
