"""Derived responsibility matrix and deterministic collision detection."""
from dataclasses import dataclass
from typing import Tuple

from .loader import load_persona_kernel
from .registry import REGISTRY

OWNER_AUTHORITY_IDS = {"owner_decision", "final_owner_decision", "canon_promotion", "role_reassignment", "permission_escalation"}

@dataclass(frozen=True)
class ResponsibilityRow:
    persona_id: str
    canonical_name: str
    role_title: str
    primary_responsibilities: Tuple[str, ...]
    secondary_responsibilities: Tuple[str, ...]
    forbidden_responsibilities: Tuple[str, ...]
    handoff_targets: Tuple[str, ...]
    authority_level: Tuple[str, ...]
    canon_status: str
    unknown_fields: Tuple[str, ...]
    boundary_status: str
    overlap_candidates: Tuple[str, ...]
    owner_decision_required: bool

@dataclass(frozen=True)
class Collision:
    collision_type: str
    persona_ids: Tuple[str, ...]
    responsibility_id: str

def build_responsibility_matrix() -> Tuple[ResponsibilityRow, ...]:
    return tuple(ResponsibilityRow(
        persona_id=k.persona_id, canonical_name=k.display_name or k.persona_id,
        role_title=k.identity or k.canonical_role,
        primary_responsibilities=k.responsibilities,
        secondary_responsibilities=k.secondary_responsibilities,
        forbidden_responsibilities=k.non_responsibilities,
        handoff_targets=k.handoff_targets, authority_level=k.authority,
        canon_status=k.canon_status, unknown_fields=k.unknown_fields,
        boundary_status=k.boundary_status, overlap_candidates=k.overlap_candidates,
        owner_decision_required=k.owner_decision_required,
    ) for k in (load_persona_kernel(pid) for pid in REGISTRY))

def detect_collisions(matrix: Tuple[ResponsibilityRow, ...]) -> Tuple[Collision, ...]:
    found = []
    for index, left in enumerate(matrix):
        for right in matrix[index + 1:]:
            for rid in sorted(set(left.primary_responsibilities) & set(right.primary_responsibilities)):
                found.append(Collision("PRIMARY_PRIMARY_COLLISION", (left.persona_id, right.persona_id), rid))
        for rid in sorted(set(left.primary_responsibilities) & set(left.forbidden_responsibilities)):
            found.append(Collision("FORBIDDEN_PRIMARY_COLLISION", (left.persona_id,), rid))
        for rid in sorted((set(left.primary_responsibilities) | set(left.authority_level)) & OWNER_AUTHORITY_IDS):
            found.append(Collision("AUTHORITY_COLLISION", (left.persona_id,), rid))
        if left.owner_decision_required or left.boundary_status == "UNRESOLVED":
            for rid in left.overlap_candidates or left.unknown_fields:
                found.append(Collision("UNKNOWN_BOUNDARY", (left.persona_id,), rid))
        if any(target.startswith("authority:") for target in left.handoff_targets):
            found.append(Collision("HANDOFF_AUTHORITY_ESCALATION", (left.persona_id,), "authority_transfer"))
    return tuple(found)
