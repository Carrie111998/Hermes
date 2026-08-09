"""Append-only audit-record validation and deterministic chain hashing."""

from __future__ import annotations

from typing import Any, Mapping

from plugins.agentops.control.events import canonical_hash, contains_secret
from plugins.agentops.control.models import AuditEvent


class AuditValidationError(ValueError):
    """Raised without serializing untrusted audit metadata."""


def validate_audit_event(event: AuditEvent) -> None:
    required = (event.actor_type, event.actor_id, event.action, event.object_type, event.object_id, event.timestamp)
    if any(not isinstance(value, str) or not value.strip() for value in required):
        raise AuditValidationError("audit validation failed")
    if not isinstance(event.metadata, Mapping) or contains_secret(event.metadata):
        raise AuditValidationError("audit validation failed")


def audit_entry_hash(*, sequence: int, payload: Mapping[str, Any]) -> str:
    return canonical_hash({"sequence": sequence, "payload": dict(payload)})
