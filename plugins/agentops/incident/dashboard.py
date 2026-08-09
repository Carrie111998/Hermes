from __future__ import annotations
from .models import Incident
import hashlib, hmac, re
from datetime import datetime, timezone

class ReadOnlyDashboard:
    """Structured read-only proxy; no chat, token, or Target mutation surface."""
    def __init__(self, incidents: list[Incident], *, token_hash: str, issued_at: datetime, expiry: datetime) -> None:
        if not isinstance(token_hash, str) or re.fullmatch(r"[0-9a-f]{64}", token_hash) is None:
            raise ValueError("token_hash must be a SHA-256 hex digest")
        if issued_at.tzinfo is None or expiry.tzinfo is None or expiry <= issued_at:
            raise ValueError("dashboard token must have a bounded timezone-aware lifetime")
        self._incidents = tuple(incidents)
        self._token_hash = token_hash
        self._issued_at = issued_at
        self._expiry = expiry
    def serve(self, *, auth_token: str | None, request: str) -> dict:
        now = datetime.now(timezone.utc)
        if not isinstance(auth_token, str) or not auth_token or self._expiry <= now or self._issued_at > now or not isinstance(request, str) or request not in {"manifest", "incidents"}:
            raise PermissionError("read-only dashboard authentication required")
        try:
            valid = hmac.compare_digest(hashlib.sha256(auth_token.encode("utf-8")).hexdigest(), self._token_hash)
        except (UnicodeError, TypeError):
            valid = False
        if not valid:
            raise PermissionError("read-only dashboard authentication required")
        return self._manifest() if request == "manifest" else {"incidents": self._view()}
    def manifest(self) -> dict: raise PermissionError("use authenticated serve endpoint")
    def _manifest(self) -> dict:
        return {"mode": "read_only", "chat": False, "target_write": False, "long_lived_tokens": False, "fields": ["fingerprint", "state", "severity", "targets", "signal_count"]}
    def incidents(self) -> tuple[dict, ...]:
        raise PermissionError("use authenticated serve endpoint")
    def _view(self):
        return tuple({"fingerprint": i.fingerprint, "state": i.state, "severity": i.severity, "targets": sorted(i.targets), "signal_count": i.signal_count} for i in self._incidents)
