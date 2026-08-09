from __future__ import annotations
from datetime import datetime
from .models import Incident

def digest(incidents: list[Incident], *, period: str, generated_at: datetime) -> dict:
    return {"period": period, "generated_at": generated_at.isoformat(), "incident_count": len(incidents), "incidents": [{"fingerprint": i.fingerprint, "state": i.state, "targets": sorted(i.targets), "signal_count": i.signal_count, "severity": i.severity} for i in incidents]}
