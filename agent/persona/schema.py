"""Immutable schema and validation for a minimal Persona Kernel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Tuple

REQUIRED_LIST_FIELDS = ("responsibilities", "non_responsibilities", "owner_relation", "output_contract", "growth_boundary")
REQUIRED_TEXT_FIELDS = ("persona_id", "canonical_role", "purpose", "canon_version")

class PersonaValidationError(ValueError):
    """Raised when Persona Canon is incomplete or contradictory."""

def _validated_items(data: Mapping[str, Any], field: str) -> Tuple[str, ...]:
    value = data.get(field)
    if not isinstance(value, list) or not value:
        raise PersonaValidationError(f"{field} must be a non-empty list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise PersonaValidationError(f"{field} entries must be non-empty strings")
    normalized = tuple(item.strip() for item in value)
    if len(normalized) != len(set(normalized)):
        raise PersonaValidationError(f"{field} contains duplicate entries")
    return normalized

@dataclass(frozen=True)
class PersonaKernel:
    persona_id: str
    canonical_role: str
    purpose: str
    responsibilities: Tuple[str, ...]
    non_responsibilities: Tuple[str, ...]
    owner_relation: Tuple[str, ...]
    output_contract: Tuple[str, ...]
    growth_boundary: Tuple[str, ...]
    canon_version: str
    checksum: str
    display_name: str = ""
    identity: str = ""
    boundaries: Tuple[str, ...] = ()
    authority: Tuple[str, ...] = ()
    core_principles: Tuple[str, ...] = ()
    handoff_targets: Tuple[str, ...] = ()
    known_aliases: Tuple[str, ...] = ()
    canon_status: str = "CONFIRMED"
    source_basis: Tuple[str, ...] = ()
    unknown_fields: Tuple[str, ...] = ()
    secondary_responsibilities: Tuple[str, ...] = ()
    boundary_status: str = "CONFIRMED"
    overlap_candidates: Tuple[str, ...] = ()
    owner_decision_required: bool = False
    historical_responsibilities: Tuple[str, ...] = ()
    historical_status: str = ""
    registry_membership: str = "ACTIVE_EXISTING"
    formal_status: str = "CONFIRMED"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PersonaKernel":
        if not isinstance(data, Mapping):
            raise PersonaValidationError("persona manifest must be an object")
        text = {}
        for field in REQUIRED_TEXT_FIELDS:
            value = data.get(field)
            if not isinstance(value, str) or not value.strip():
                raise PersonaValidationError(f"{field} must be a non-empty string")
            text[field] = value.strip()
        checksum = data.get("checksum")
        if not isinstance(checksum, str) or len(checksum) != 64:
            raise PersonaValidationError("checksum must be a SHA-256 hex digest")
        try:
            int(checksum, 16)
        except ValueError as exc:
            raise PersonaValidationError("checksum must be a SHA-256 hex digest") from exc
        lists = {field: _validated_items(data, field) for field in REQUIRED_LIST_FIELDS}
        overlap = set(lists["responsibilities"]) & set(lists["non_responsibilities"])
        if overlap:
            raise PersonaValidationError("responsibility/non-responsibility contradiction: " + ", ".join(sorted(overlap)))
        optional_lists = {}
        for field in ("boundaries", "authority", "core_principles", "handoff_targets", "known_aliases", "source_basis", "unknown_fields", "secondary_responsibilities", "overlap_candidates", "historical_responsibilities"):
            value = data.get(field, [])
            if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                raise PersonaValidationError(f"{field} must be a string list")
            optional_lists[field] = tuple(item.strip() for item in value)
        optional_text = {}
        for field in ("display_name", "identity", "canon_status", "boundary_status", "historical_status", "registry_membership", "formal_status"):
            value = data.get(field, "CONFIRMED" if field == "canon_status" else "")
            if not isinstance(value, str):
                raise PersonaValidationError(f"{field} must be a string")
            optional_text[field] = value.strip()
        if optional_text["canon_status"] not in {"CONFIRMED", "PARTIAL", "UNKNOWN", "CONFLICT"}:
            raise PersonaValidationError("invalid canon_status")
        owner_decision_required = data.get("owner_decision_required", False)
        if not isinstance(owner_decision_required, bool):
            raise PersonaValidationError("owner_decision_required must be boolean")
        return cls(checksum=checksum.lower(), owner_decision_required=owner_decision_required, **text, **lists, **optional_text, **optional_lists)
