from __future__ import annotations

from dataclasses import dataclass

from agent.memory_records import Approval, Candidate, CanonicalRecord, LifecycleStatus, MemoryClass, ReviewPacket


@dataclass(frozen=True)
class Authority:
    rank: int
    may_activate: bool


_LIFECYCLE_TRANSITIONS = {
    LifecycleStatus.PROPOSED: frozenset({
        LifecycleStatus.ACTIVE,
        LifecycleStatus.REJECTED,
        LifecycleStatus.EXPIRED,
    }),
    LifecycleStatus.ACTIVE: frozenset({
        LifecycleStatus.DISPUTED,
        LifecycleStatus.SUPERSEDED,
        LifecycleStatus.EXPIRED,
    }),
    LifecycleStatus.DISPUTED: frozenset({
        LifecycleStatus.ACTIVE,
        LifecycleStatus.SUPERSEDED,
        LifecycleStatus.REJECTED,
        LifecycleStatus.EXPIRED,
    }),
}
_TERMINAL_LIFECYCLE_STATES = frozenset({
    LifecycleStatus.SUPERSEDED,
    LifecycleStatus.EXPIRED,
    LifecycleStatus.REJECTED,
})


def evaluate_authority(candidate: Candidate) -> Authority:
    ranks = {"direct_user": 100, "session_history": 60, "provider": 20, "model": 10}
    rank = ranks.get(candidate.provenance.source_type, 0)
    allowed = candidate.provenance.source_type == "direct_user" and not candidate.provenance.derived
    return Authority(rank=rank, may_activate=allowed)


def validate_lifecycle_transition(previous: LifecycleStatus, proposed: LifecycleStatus) -> tuple[str, ...]:
    if type(previous) is not LifecycleStatus or type(proposed) is not LifecycleStatus:
        return ("invalid_lifecycle_state",)
    if previous is proposed:
        return ("lifecycle_transition_noop",)
    if previous in _TERMINAL_LIFECYCLE_STATES:
        return ("terminal_lifecycle_state",)
    if proposed not in _LIFECYCLE_TRANSITIONS.get(previous, frozenset()):
        return ("invalid_lifecycle_transition",)
    return ()


def validate_candidate(candidate: Candidate) -> tuple[str, ...]:
    findings: list[str] = []
    if candidate.lifecycle.status is not LifecycleStatus.PROPOSED:
        findings.append("candidate_must_be_proposed")
    if candidate.memory_class is MemoryClass.T and candidate.lifecycle.expires_at is None:
        findings.append("temporary_requires_expiry")
    return tuple(findings)


def validate_approval(packet: ReviewPacket, approval: Approval | None) -> tuple[str, ...]:
    if approval is None or not approval.exact:
        return ("exact_approval_required",)
    if approval.packet_id != packet.packet_id:
        return ("packet_id_mismatch",)
    if approval.approver != "direct-user":
        return ("direct_user_approval_required",)
    return ()


def _validate_packet(candidate: Candidate, packet: ReviewPacket | None) -> tuple[str, ...]:
    if packet is None:
        return ("exact_approval_required",)
    if packet.scope != candidate.scope or packet.candidate_ids != (candidate.content_hash(),):
        return ("packet_candidate_mismatch",)
    return ()


def validate_supersession(active: CanonicalRecord, proposed: Candidate, approval: Approval | None = None, packet: ReviewPacket | None = None) -> tuple[str, ...]:
    if proposed.memory_class is MemoryClass.A and proposed.provenance.derived:
        return ("derived_authorization_forbidden",)
    if active.scope != proposed.scope:
        return ("scope_change_forbidden",)
    if active.memory_class is not proposed.memory_class:
        return ("memory_class_change_forbidden",)
    target = proposed.lifecycle.supersedes_record_id
    if target is None:
        return ("supersession_link_required",)
    if target != active.record_id:
        return ("supersession_target_mismatch",)
    packet_findings = _validate_packet(proposed, packet)
    if packet_findings:
        return packet_findings
    assert packet is not None
    return validate_approval(packet, approval)


def validate_transition(previous: CanonicalRecord | None, proposed: Candidate, approval: Approval | None = None, packet: ReviewPacket | None = None) -> tuple[str, ...]:
    candidate_findings = validate_candidate(proposed)
    if candidate_findings:
        return candidate_findings
    if proposed.memory_class is MemoryClass.A and proposed.provenance.derived:
        return ("derived_authorization_forbidden",)
    if previous is not None:
        return validate_supersession(previous, proposed, approval, packet)
    if proposed.lifecycle.supersedes_record_id is not None:
        return ("supersession_target_missing",)
    packet_findings = _validate_packet(proposed, packet)
    if packet_findings:
        return packet_findings
    assert packet is not None
    return validate_approval(packet, approval)
