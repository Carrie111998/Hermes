from __future__ import annotations

from datetime import timedelta

from .fingerprint import incident_fingerprint
from .models import Incident, IncidentSignal


class IncidentCorrelator:
    """Bounded, deterministic incident correlation.

    ``_seen`` is deliberately an idempotency map, rather than a fingerprint
    cache: a repeated signal id must always resolve to the original incident,
    even if a caller supplies a changed payload on retry.
    """

    def __init__(self, *, window_seconds: int = 900, max_incidents: int = 1000, max_history: int = 5000) -> None:
        if window_seconds <= 0 or max_incidents <= 0 or max_history <= 0:
            raise ValueError("invalid correlation budget")
        self.window = timedelta(seconds=window_seconds)
        self.max_incidents = max_incidents
        self.max_history = max_history
        self._incidents: dict[str, Incident] = {}
        self._history: list[Incident] = []
        self._seen: dict[str, Incident] = {}
        self._split_counter = 0

    def _record_history(self, incident: Incident) -> None:
        self._history.append(incident)
        if len(self._history) > self.max_history:
            del self._history[: len(self._history) - self.max_history]

    def _active_for(self, fingerprint: str) -> Incident | None:
        # Multiple windows can have the same stable fingerprint.
        for incident in reversed(tuple(self._incidents.values())):
            if incident.fingerprint == fingerprint:
                return incident
        return None

    def ingest(self, signal: IncidentSignal) -> Incident:
        existing = self._seen.get(signal.signal_id)
        if existing is not None:
            return existing

        fingerprint = incident_fingerprint(signal.signal_type, signal.payload, collector=signal.collector)
        incident = self._active_for(fingerprint)
        if incident is None or signal.observed_at - incident.last_seen > self.window:
            had_active = incident is not None
            if len(self._incidents) >= self.max_incidents:
                raise RuntimeError("incident budget exceeded")
            incident = Incident(fingerprint, signal.observed_at, signal.observed_at, severity=signal.severity)
            key = f"{fingerprint}:{signal.observed_at.isoformat()}" if had_active else fingerprint
            # Avoid replacing a still-retained active object at the same key.
            if key in self._incidents:
                key = f"{fingerprint}:{signal.observed_at.isoformat()}:{len(self._incidents)}"
            self._incidents[key] = incident
            self._record_history(incident)

        if incident.state == "resolved":
            incident.state = "reopened"
            incident.history.append("reopened")
        if signal.severity in {"critical", "error"}:
            incident.severity = signal.severity
        incident.first_seen = min(incident.first_seen, signal.observed_at)
        incident.last_seen = max(incident.last_seen, signal.observed_at)
        incident.targets.add(signal.target_id)
        incident.signal_count += 1
        incident.evidence.append(signal)
        if len(incident.evidence) > self.max_history:
            del incident.evidence[: len(incident.evidence) - self.max_history]
        self._seen[signal.signal_id] = incident
        return incident

    def incidents(self) -> tuple[Incident, ...]:
        """Currently active incidents (one object per correlation window)."""
        return tuple(self._incidents.values())

    def history(self) -> tuple[Incident, ...]:
        return tuple(self._history)

    def all_incidents(self) -> tuple[Incident, ...]:
        """Active plus retained historical incidents, identity-deduplicated."""
        result: list[Incident] = []
        seen: set[int] = set()
        for incident in (*self._history, *self._incidents.values()):
            if id(incident) not in seen:
                seen.add(id(incident))
                result.append(incident)
        return tuple(result)

    def _find(self, fingerprint: str) -> Incident | None:
        return self._active_for(fingerprint) or next(
            (item for item in reversed(self._history) if item.fingerprint == fingerprint), None
        )

    def suppress(self, fingerprint: str) -> Incident:
        incident = self._find(fingerprint)
        if incident is None:
            raise KeyError(fingerprint)
        if incident.state != "suppressed":
            incident.history.append(f"{incident.state}->suppressed")
            incident.state = "suppressed"
        return incident

    def merge(self, target: Incident, source: Incident) -> Incident:
        if target is source:
            return target
        if len(target.evidence) + len(source.evidence) > self.max_history:
            raise RuntimeError("incident evidence budget exceeded")
        target.targets.update(source.targets)
        target.signal_count += source.signal_count
        target.evidence.extend(source.evidence)
        target.evidence.sort(key=lambda item: item.observed_at)
        target.first_seen = min(target.first_seen, source.first_seen)
        target.last_seen = max(target.last_seen, source.last_seen)
        if source.severity in {"critical", "error"}:
            target.severity = source.severity
        target.history.append("merged")
        source.history.append(f"merged_into:{target.fingerprint}")
        for signal_id, incident in list(self._seen.items()):
            if incident is source:
                self._seen[signal_id] = target
        for key, incident in list(self._incidents.items()):
            if incident is source:
                del self._incidents[key]
        return target

    def split(self, incident: Incident, signal_ids: set[str]) -> Incident:
        all_ids = {item.signal_id for item in incident.evidence}
        if not signal_ids or not signal_ids.issubset(all_ids) or signal_ids == all_ids:
            raise ValueError("invalid split selection")
        moved = [item for item in incident.evidence if item.signal_id in signal_ids]
        remaining = [item for item in incident.evidence if item.signal_id not in signal_ids]
        self._split_counter += 1
        child_fp = f"{incident.fingerprint}:split:{self._split_counter}"
        child = Incident(
            child_fp,
            min(item.observed_at for item in moved),
            max(item.observed_at for item in moved),
            targets={item.target_id for item in moved},
            signal_count=len(moved),
            severity=incident.severity,
            evidence=moved,
            history=["split"],
        )
        incident.evidence = remaining
        incident.targets = {item.target_id for item in remaining}
        incident.signal_count = len(remaining)
        incident.first_seen = min((item.observed_at for item in remaining), default=incident.first_seen)
        incident.last_seen = max((item.observed_at for item in remaining), default=incident.last_seen)
        incident.history.append(f"split:{child_fp}")
        self._record_history(child)
        self._incidents[child_fp] = child
        for item in moved:
            self._seen[item.signal_id] = child
        return child
