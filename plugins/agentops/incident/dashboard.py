from __future__ import annotations
from .models import Incident
import hashlib, hmac

class ReadOnlyDashboard:
    """Structured read-only proxy; no chat, token, or Target mutation surface."""
    def __init__(self, incidents: list[Incident], *, token_hash: str) -> None: self._incidents = incidents; self._token_hash = token_hash
    def serve(self, *, auth_token: str | None, request: str) -> dict:
        if not auth_token or not hmac.compare_digest(hashlib.sha256(auth_token.encode()).hexdigest(), self._token_hash) or request not in {"manifest", "incidents"}: raise PermissionError("read-only dashboard authentication required")
        return self.manifest() if request == "manifest" else {"incidents": self._view()}
    def manifest(self) -> dict:
        return {"mode": "read_only", "chat": False, "target_write": False, "long_lived_tokens": False, "fields": ["fingerprint", "state", "severity", "targets", "signal_count"]}
    def incidents(self) -> tuple[dict, ...]:
        raise PermissionError("use authenticated serve endpoint")
    def _view(self):
        return tuple({"fingerprint": i.fingerprint, "state": i.state, "severity": i.severity, "targets": sorted(i.targets), "signal_count": i.signal_count} for i in self._incidents)
