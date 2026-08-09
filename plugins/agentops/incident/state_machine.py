from __future__ import annotations
from .models import Incident

STATES = ("open", "acknowledged", "resolved", "reopened", "suppressed", "merged")
_ALLOWED = {
    "open": {"acknowledged", "resolved", "suppressed"},
    "acknowledged": {"resolved", "reopened", "suppressed"},
    "resolved": {"reopened", "suppressed"},
    "reopened": {"acknowledged", "resolved", "suppressed"},
    "suppressed": {"reopened"},
    "merged": set(),
}

def transition(incident: Incident, state: str) -> Incident:
    if state not in STATES or state not in _ALLOWED.get(incident.state, set()): raise ValueError("invalid incident transition")
    incident.history.append(f"{incident.state}->{state}")
    incident.state = state
    return incident
