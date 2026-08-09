from __future__ import annotations
from .models import Incident, ReviewResult

class RuleReviewer:
    """Deterministic reviewer; no LLM/network call is made."""
    def review(self, incident: Incident) -> ReviewResult:
        decision = "escalate" if incident.severity in {"critical", "error"} else "observe"
        risk = incident.severity if incident.severity in {"critical", "high", "medium", "low"} else "medium"
        return ReviewResult(incident.fingerprint, decision, "deterministic rule review", model_used=False, degraded=True, hypotheses=("repeated signal pattern",), risk=risk, actions=(), verification=("collect next observation",), rollback=(), confidence=0.2, evidence_ids=tuple(s.signal_id for s in incident.evidence))
