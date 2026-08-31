import pytest

from agent.worker_contract import (
    ContractValidationError,
    CapabilityRecord,
    ConsensusRecord,
    EvidencePacket,
    ObjectiveStack,
    WorkerMode,
    validate_contract_mapping,
)


def test_evidence_packet_serializes_observations_separately_from_conclusions():
    packet = EvidencePacket(
        observations=("pytest exited 0",),
        sources=("terminal://run-1",),
        hypotheses=("the focused suite is green",),
        conclusions=("the focused suite passed",),
        unknowns=("full-suite status",),
        confidence="high",
        evidence_class="targeted",
        freshness="2026-08-30T12:00:00Z",
        reversible_next_actions=("rerun the focused test",),
        outcome="completed",
    )

    assert packet.to_dict() == {
        "kind": "evidence_packet",
        "observations": ["pytest exited 0"],
        "sources": ["terminal://run-1"],
        "hypotheses": ["the focused suite is green"],
        "conclusions": ["the focused suite passed"],
        "unknowns": ["full-suite status"],
        "confidence": "high",
        "evidence_class": "targeted",
        "artifacts": [],
        "limitations": [],
        "freshness": "2026-08-30T12:00:00Z",
        "reversible_next_actions": ["rerun the focused test"],
        "outcome": "completed",
    }


def test_evidence_packet_rejects_conclusion_without_observation_or_source():
    with pytest.raises(ContractValidationError, match="observations"):
        EvidencePacket(
            observations=(),
            sources=(),
            conclusions=("done",),
        ).validate()


def test_contract_mapping_requires_complete_typed_payloads():
    with pytest.raises(ContractValidationError, match="missing field"):
        validate_contract_mapping({"kind": "evidence_packet"})

    with pytest.raises(ContractValidationError, match="observations"):
        validate_contract_mapping(
            {
                "kind": "evidence_packet",
                "observations": "not-a-list",
                "sources": ["terminal://run-1"],
                "hypotheses": [],
                "conclusions": [],
                "unknowns": [],
                "confidence": "high",
                "evidence_class": "targeted",
                "artifacts": [],
                "limitations": [],
                "freshness": "2026-08-30T12:00:00Z",
                "reversible_next_actions": [],
                "outcome": "completed",
            }
        )


def test_contract_serialization_is_composable_with_mapping_validation():
    packet = EvidencePacket(
        observations=("observed",),
        sources=("source",),
        freshness="2026-08-30T12:00:00Z",
        reversible_next_actions=("inspect again",),
        outcome="blocked",
    )
    assert validate_contract_mapping(packet.to_dict()) == packet.to_dict()


def test_objective_stack_requires_task_owner_and_repository_identity():
    stack = ObjectiveStack(
        profile="worker",
        authority="read_only",
        mission="research",
        task_id="task-1",
        owner_id="operator-1",
        repository="org/repo",
    )
    assert stack.validate() is stack
    with pytest.raises(ContractValidationError, match="task_id"):
        ObjectiveStack(
            profile="worker",
            authority="read_only",
            mission="research",
            owner_id="operator-1",
            repository="org/repo",
        ).validate()


def test_evidence_packet_requires_freshness_reversible_actions_and_explicit_outcome():
    with pytest.raises(ContractValidationError, match="freshness"):
        EvidencePacket(
            observations=("observed",),
            sources=("source",),
            reversible_next_actions=("inspect",),
            outcome="failed",
        ).validate()

    with pytest.raises(ContractValidationError, match="reversible"):
        EvidencePacket(
            observations=("observed",),
            sources=("source",),
            freshness="2026-08-30T12:00:00Z",
            outcome="blocked",
        ).validate()

    with pytest.raises(ContractValidationError, match="outcome"):
        EvidencePacket(
            observations=("observed",),
            sources=("source",),
            freshness="2026-08-30T12:00:00Z",
            reversible_next_actions=("inspect",),
        ).validate()


def test_objective_stack_rejects_hidden_or_conflicting_objectives():
    with pytest.raises(ContractValidationError, match="hidden"):
        ObjectiveStack(
            profile="scientist",
            authority="advisory",
            mission="research",
            task_id="task-1",
            owner_id="operator-1",
            repository="org/repo",
            hidden_objectives=("do not tell the operator",),
        ).validate()

    with pytest.raises(ContractValidationError, match="conflict"):
        ObjectiveStack(
            profile="worker",
            authority="read_only",
            mission="publish",
            task_id="task-1",
            owner_id="operator-1",
            repository="org/repo",
            constraints=("never publish without approval",),
            conflicts=("publish automatically",),
        ).validate()


def test_worker_mode_cannot_reduce_truth_or_safety_requirements():
    with pytest.raises(ContractValidationError, match="citations"):
        WorkerMode(
            name="incident",
            verbosity="concise",
            directness="high",
            requires_citations=False,
            requires_uncertainty=True,
        ).validate()


def test_contract_mapping_rejects_unknown_fields():
    with pytest.raises(ContractValidationError, match="unknown field"):
        validate_contract_mapping(
            {"kind": "evidence_packet", "observations": [], "unexpected": True}
        )


def test_capability_record_requires_test_provenance_before_activation():
    record = CapabilityRecord(
        name="exact_head_verification",
        owner_profile="acceptance-gate-verifier",
        authority="read_only",
        evidence_class="governed",
        status="active",
        tested_at="2026-08-30T12:00:00Z",
        source_sha="abc123",
    )

    assert record.validate() is record
    assert record.to_dict()["source_sha"] == "abc123"

    with pytest.raises(ContractValidationError, match="source_sha"):
        CapabilityRecord(
            name="unsafe_capability",
            owner_profile="worker",
            authority="execute",
            status="active",
            tested_at="2026-08-30T12:00:00Z",
        ).validate()


def test_consensus_record_preserves_dissent_and_requires_worker_identity():
    record = ConsensusRecord(
        worker_reports=(
            {"worker": "a", "conclusion": "pass"},
            {"worker": "b", "conclusion": "blocked"},
        ),
        agreement=("same source was inspected",),
        dissent=("worker b found a missing receipt",),
        status="needs_review",
    )

    assert record.validate() is record
    assert record.to_dict()["dissent"] == ["worker b found a missing receipt"]

    with pytest.raises(ContractValidationError, match="worker"):
        ConsensusRecord(
            worker_reports=({"conclusion": "pass"},),
            status="accepted",
        ).validate()

    with pytest.raises(ContractValidationError, match="independent"):
        ConsensusRecord(
            worker_reports=({"worker": "a", "conclusion": "pass"},),
            status="accepted",
        ).validate()

    with pytest.raises(ContractValidationError, match="duplicate"):
        ConsensusRecord(
            worker_reports=(
                {"worker": "a", "conclusion": "pass"},
                {"worker": "a", "conclusion": "pass"},
            ),
            status="pending",
        ).validate()
