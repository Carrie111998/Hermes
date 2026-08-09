from __future__ import annotations

from datetime import timedelta
from .fingerprint import incident_fingerprint
from .models import Incident, IncidentSignal

class IncidentCorrelator:
    def __init__(self, *, window_seconds: int = 900, max_incidents: int = 1000, max_history: int = 5000) -> None:
        if window_seconds <= 0 or max_incidents <= 0: raise ValueError("invalid correlation budget")
        self.window = timedelta(seconds=window_seconds)
        self.max_incidents = max_incidents
        self.max_history = max_history
        self._incidents: dict[str, Incident] = {}
        self._history: list[Incident] = []
        self._seen: dict[str, Incident] = {}

    def ingest(self, signal: IncidentSignal) -> Incident:
        if signal.signal_id in self._seen:
            fp = incident_fingerprint(signal.signal_type, signal.payload, collector=signal.collector)
            return self._seen[signal.signal_id]
        fp = incident_fingerprint(signal.signal_type, signal.payload, collector=signal.collector)
        incident = self._incidents.get(fp)
        if incident is None:
            if len(self._incidents) >= self.max_incidents: raise RuntimeError("incident budget exceeded")
            incident = Incident(fp, signal.observed_at, signal.observed_at, severity=signal.severity)
            self._incidents[fp] = incident
            self._history.append(incident)
        elif signal.observed_at - incident.last_seen > self.window:
            incident = Incident(fp, signal.observed_at, signal.observed_at, severity=signal.severity)
            self._history.append(incident); self._incidents[fp] = incident
            if len(self._history) > self.max_history: self._history.pop(0)
        if incident.state == "resolved": incident.state = "reopened"; incident.history.append("reopened")
        if signal.severity in {"critical", "error"}: incident.severity = signal.severity
        incident.first_seen = min(incident.first_seen, signal.observed_at)
        incident.last_seen = max(incident.last_seen, signal.observed_at)
        incident.targets.add(signal.target_id); incident.signal_count += 1
        incident.evidence.append(signal)
        self._seen[signal.signal_id] = incident
        if len(incident.evidence) > self.max_history: incident.evidence.pop(0)
        return incident

    def incidents(self) -> tuple[Incident, ...]: return tuple(self._incidents.values())
    def history(self) -> tuple[Incident, ...]: return tuple(self._history)
    def suppress(self, fingerprint: str) -> None:
        if fingerprint in self._incidents: self._incidents[fingerprint].state = "resolved"; self._incidents[fingerprint].history.append("suppressed")
    def merge(self, target: Incident, source: Incident) -> Incident:
        target.targets.update(source.targets); target.signal_count += source.signal_count; target.evidence.extend(source.evidence); target.history.append("merged"); return target
