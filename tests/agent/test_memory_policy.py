from __future__ import annotations

from agent.memory_policy import (
    evaluate_authority,
    validate_approval,
    validate_lifecycle_transition,
    validate_supersession,
    validate_transition,
)
from agent.memory_records import Approval, Candidate, CanonicalRecord, Lifecycle, LifecycleStatus, MemoryClass, Provenance, ReviewPacket, Scope


def make(memory_class: MemoryClass = MemoryClass.U, *, derived: bool = False, rule: str = "hold", supersedes: str | None = None) -> Candidate:
    return Candidate(
        memory_class=memory_class,
        scope=Scope("profile", "project", "alpha"),
        provenance=Provenance("provider" if derived else "direct_user", "source", "model" if derived else "user", derived),
        lifecycle=Lifecycle(LifecycleStatus.PROPOSED, observed_at="2026-01-01T00:00:00Z", supersedes_record_id=supersedes),
        content={"rule": rule},
        confidence=0.5,
    )


def exact(candidate: Candidate, version: int) -> tuple[ReviewPacket, Approval]:
    packet = ReviewPacket("packet", candidate.scope, (candidate.content_hash(),), version)
    return packet, Approval("packet", "direct-user", True)


def active(candidate: Candidate, record_id: str = "record-1") -> CanonicalRecord:
    return CanonicalRecord(
        record_id,
        1,
        candidate.memory_class,
        candidate.scope,
        candidate.provenance,
        Lifecycle(
            LifecycleStatus.ACTIVE,
            observed_at=candidate.lifecycle.observed_at,
            effective_at="2026-01-02T00:00:00Z",
        ),
        candidate.content,
    )


def test_authority_comes_from_provenance_not_confidence() -> None:
    direct = make()
    history = Candidate(
        MemoryClass.U,
        direct.scope,
        Provenance("session_history", "earlier-turn", "assistant", True),
        Lifecycle(LifecycleStatus.PROPOSED, observed_at="2026-01-01T00:00:00Z"),
        {"rule": "historical"},
        1.0,
    )
    derived = make(derived=True)
    assert evaluate_authority(direct).rank > evaluate_authority(history).rank > evaluate_authority(derived).rank
    assert not evaluate_authority(derived).may_activate


def test_initial_authorization_requires_exact_direct_user_approval() -> None:
    candidate = make(MemoryClass.A)
    packet, approval = exact(candidate, 0)
    assert validate_transition(None, candidate, None, packet) == ("exact_approval_required",)
    assert validate_transition(None, candidate, Approval("packet", "model", True), packet) == ("direct_user_approval_required",)
    assert validate_transition(None, candidate, approval, packet) == ()


def test_f5_initial_derived_authorization_is_rejected_even_with_direct_approval() -> None:
    candidate = make(MemoryClass.A, derived=True)
    packet, approval = exact(candidate, 0)
    assert validate_transition(None, candidate, approval, packet) == ("derived_authorization_forbidden",)


def test_direct_supersession_rejects_derived_authorization_first() -> None:
    previous = active(make(MemoryClass.A, rule="hold"))
    replacement = make(MemoryClass.A, derived=True, rule="deploy", supersedes=previous.record_id)
    packet, approval = exact(replacement, 3)
    assert validate_supersession(previous, replacement, approval, packet) == ("derived_authorization_forbidden",)


def test_supersession_requires_a_matching_packet() -> None:
    previous = active(make(MemoryClass.A, rule="old"))
    replacement = make(MemoryClass.A, rule="new", supersedes=previous.record_id)
    packet, approval = exact(replacement, 2)
    assert validate_approval(packet, approval) == ()
    assert validate_supersession(previous, replacement, approval, packet) == ()
    assert validate_supersession(previous, replacement, Approval("other", "direct-user", True), packet) == ("packet_id_mismatch",)


def test_every_activation_requires_exact_direct_user_approval() -> None:
    candidate = make(MemoryClass.U)
    packet, approval = exact(candidate, 0)
    assert validate_transition(None, candidate, None, packet) == ("exact_approval_required",)
    assert validate_transition(None, candidate, Approval("packet", "model", True), packet) == ("direct_user_approval_required",)
    assert validate_transition(None, candidate, approval, packet) == ()


def test_supersession_requires_exact_target_and_preserves_class() -> None:
    previous = active(make(MemoryClass.U, rule="old"))
    missing = make(MemoryClass.U, rule="new")
    packet, approval = exact(missing, 1)
    assert validate_supersession(previous, missing, approval, packet) == ("supersession_link_required",)

    wrong = make(MemoryClass.U, rule="new", supersedes="other")
    packet, approval = exact(wrong, 1)
    assert validate_supersession(previous, wrong, approval, packet) == ("supersession_target_mismatch",)

    changed_class = make(MemoryClass.P, rule="new", supersedes=previous.record_id)
    packet, approval = exact(changed_class, 1)
    assert validate_supersession(previous, changed_class, approval, packet) == ("memory_class_change_forbidden",)


def test_initial_candidate_cannot_claim_supersession() -> None:
    candidate = make(supersedes="record")
    packet, approval = exact(candidate, 0)
    assert validate_transition(None, candidate, approval, packet) == ("supersession_target_missing",)


def test_lifecycle_transition_matrix_is_explicit_and_terminal_states_stay_terminal() -> None:
    allowed = (
        (LifecycleStatus.PROPOSED, LifecycleStatus.ACTIVE),
        (LifecycleStatus.PROPOSED, LifecycleStatus.REJECTED),
        (LifecycleStatus.PROPOSED, LifecycleStatus.EXPIRED),
        (LifecycleStatus.ACTIVE, LifecycleStatus.DISPUTED),
        (LifecycleStatus.ACTIVE, LifecycleStatus.SUPERSEDED),
        (LifecycleStatus.ACTIVE, LifecycleStatus.EXPIRED),
        (LifecycleStatus.DISPUTED, LifecycleStatus.ACTIVE),
        (LifecycleStatus.DISPUTED, LifecycleStatus.SUPERSEDED),
        (LifecycleStatus.DISPUTED, LifecycleStatus.REJECTED),
        (LifecycleStatus.DISPUTED, LifecycleStatus.EXPIRED),
    )
    for previous, proposed in allowed:
        assert validate_lifecycle_transition(previous, proposed) == ()
    for terminal in (LifecycleStatus.SUPERSEDED, LifecycleStatus.EXPIRED, LifecycleStatus.REJECTED):
        assert validate_lifecycle_transition(terminal, LifecycleStatus.ACTIVE) == ("terminal_lifecycle_state",)
    assert validate_lifecycle_transition(LifecycleStatus.ACTIVE, LifecycleStatus.PROPOSED) == ("invalid_lifecycle_transition",)
    assert validate_lifecycle_transition(LifecycleStatus.ACTIVE, LifecycleStatus.ACTIVE) == ("lifecycle_transition_noop",)
