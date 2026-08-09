from __future__ import annotations

from datetime import timedelta
from .fingerprint import incident_fingerprint
from .models import Incident, IncidentSignal

class IncidentCorrelator:
    def __init__(self, *, window_seconds: int = 900, max_incidents: int = 1000) -> None:
        if window_seconds <= 0 or max_incidents <= 0: raise ValueError("invalid correlation budget")
        self.window = timedelta(seconds=window_seconds)
        self.max_incidents = max_incidents
        self._incidents: dict[str, Incident] = {}

    def ingest(self, signal: IncidentSignal) -> Incident:
        fp = incident_fingerprint(signal.signal_type, signal.payload, collector=signal.collector)
        incident = self._incidents.get(fp)
        if incident is None:
            if len(self._incidents) >= self.max_incidents: raise RuntimeError("incident budget exceeded")
            incident = Incident(fp, signal.observed_at, signal.observed_at, severity=signal.severity)
            self._incidents[fp] = incident
        elif signal.observed_at - incident.last_seen > self.window:
            incident = Incident(fp, signal.observed_at, signal.observed_at, severity=signal.severity)
            self._incidents[fp] = incident
        incident.first_seen = min(incident.first_seen, signal.observed_at)
        incident.last_seen = max(incident.last_seen, signal.observed_at)
        incident.targets.add(signal.target_id); incident.signal_count += 1
        incident.evidence.append(signal)
        return incident

    def incidents(self) -> tuple[Incident, ...]: return tuple(self._incidents.values())
