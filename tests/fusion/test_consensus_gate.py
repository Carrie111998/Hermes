from agent.fusion.consensus_gate import evaluate_consensus_gate
from agent.fusion.models import FusionParticipantResult, FusionParticipantSpec, FusionRequest, MATERIAL_AXES
from agent.fusion.verification import verify_convergence_votes, verify_participant_outputs


def _output(claims: dict[str, str]) -> str:
    lines = ["## Material Axes"]
    for axis in MATERIAL_AXES:
        lines.append(f"- {axis}: {claims.get(axis, 'same')}")
    return "\n".join(lines)


def _participant(slug: str, claims: dict[str, str] | None = None) -> FusionParticipantResult:
    return FusionParticipantResult(
        spec=FusionParticipantSpec(slug=slug, role=slug, focus="test", provider="p", model=slug),
        status="completed",
        output=_output(claims or {}),
    )


def _vote(slug: str, approved=True, dissent=None, changes=None) -> FusionParticipantResult:
    return FusionParticipantResult(
        spec=FusionParticipantSpec(slug=slug, role=slug, focus="test", provider="p", model=slug),
        status="completed",
        phase="vote-1",
        output="```json\n" + __import__("json").dumps({
            "candidate_id": "candidate-r1",
            "approved": approved,
            "material_dissent": dissent or [],
            "required_changes": changes or [],
            "unsupported_claims": [],
            "confidence": "high",
            "summary": "ok" if approved else "blocked",
        }) + "\n```",
    )


def test_unanimous_legacy_axis_consensus_passes_gate():
    report = verify_participant_outputs([_participant("a"), _participant("b"), _participant("c")])
    gate = evaluate_consensus_gate(report, FusionRequest(mode="plan", task="x", participants=3))
    assert gate.passed is True
    assert gate.status == "converged"


def test_two_of_three_with_material_dissenter_fails_gate():
    dissent = {"architecture": "different architecture"}
    report = verify_participant_outputs([_participant("a"), _participant("b"), _participant("c", dissent)])
    gate = evaluate_consensus_gate(report, FusionRequest(mode="plan", task="x", participants=3))
    assert gate.passed is False
    assert gate.status == "operator_decision"
    assert any(conflict.axis == "architecture" for conflict in gate.conflicts)


def test_structured_votes_allow_different_draft_wording_to_converge():
    report = verify_convergence_votes([_vote("a"), _vote("b"), _vote("c")], candidate_id="candidate-r1", total_participants=3)
    gate = evaluate_consensus_gate(report, FusionRequest(mode="plan", task="x", participants=3))
    assert gate.passed is True
    assert gate.status == "converged"


def test_structured_vote_material_reject_blocks_majority():
    report = verify_convergence_votes(
        [_vote("a"), _vote("b"), _vote("c", approved=False, dissent=["unsafe"] )],
        candidate_id="candidate-r1",
        total_participants=3,
    )
    gate = evaluate_consensus_gate(report, FusionRequest(mode="plan", task="x", participants=3))
    assert gate.passed is False
    assert gate.status == "operator_decision"
    assert any("c" in conflict.participants for conflict in gate.conflicts)


def test_single_successful_participant_does_not_pass_by_default():
    report = verify_participant_outputs([_participant("solo")])
    gate = evaluate_consensus_gate(report, FusionRequest(mode="plan", task="x", participants=1, min_successful_participants=1))
    assert gate.passed is False
    assert gate.status == "degraded_insufficient_participants"
