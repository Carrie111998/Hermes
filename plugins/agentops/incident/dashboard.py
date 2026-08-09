from __future__ import annotations
from .models import Incident

class ReadOnlyDashboard:
    """Structured read-only proxy; no chat, token, or Target mutation surface."""
    def __init__(self, incidents: list[Incident]) -> None: self._incidents = incidents
    def serve(self, *, auth_token: str | None, request: str) -> dict:
        if not auth_token or request not in {"manifest", "incidents"}: raise PermissionError("read-only dashboard authentication required")
        return self.manifest() if request == "manifest" else {"incidents": self.incidents()}
    def manifest(self) -> dict:
        return {"mode": "read_only", "chat": False, "target_write": False, "long_lived_tokens": False, "fields": ["fingerprint", "state", "severity", "targets", "signal_count"]}
    def incidents(self) -> tuple[dict, ...]:
        return tuple({"fingerprint": i.fingerprint, "state": i.state, "severity": i.severity, "targets": sorted(i.targets), "signal_count": i.signal_count} for i in self._incidents)
