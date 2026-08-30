from agent.fusion.config import participant_specs_for_request
from agent.fusion.context import FusionContext
from agent.fusion.models import FusionParticipantResult, FusionRequest
from agent.fusion.prompts import build_debate_prompt, build_draft_prompt, build_participant_system_prompt


def _context():
    return FusionContext(repo_root="/tmp/repo", cwd="/tmp/repo", repo_guard_available=True, notes=[])


def test_default_participants_are_equal_peers_not_biased_roles():
    request = FusionRequest(mode="plan", task="x", participants=3)
    specs = participant_specs_for_request(request)
    assert [spec.role for spec in specs] == ["Equal peer participant"] * 3
    assert {spec.slug for spec in specs} == {"glm-max", "deepseek-pro-max", "codex-default"}
    for spec in specs:
        system = build_participant_system_prompt(spec, _context())
        draft = build_draft_prompt(spec, request, _context())
        assert "Every participant has the same status, rights, and responsibility" in system
        assert "No participant is the chair, architect, critic, tester, verifier" in system
        assert "not filling a specialized role" in draft


def test_later_debate_round_includes_peer_questions_and_prior_debate():
    request = FusionRequest(mode="plan", task="x", participants=2)
    context = _context()
    specs = participant_specs_for_request(request, config={"fusion": {"model_pool": [
        {"provider": "zai", "model": "glm-5.2"},
        {"provider": "deepseek", "model": "deepseek-v4-pro"},
    ]}})
    drafts = [FusionParticipantResult(spec=spec, status="completed", phase="draft", output="draft") for spec in specs]
    prior = [FusionParticipantResult(spec=specs[1], status="completed", phase="debate-1", output="question for peer")]
    prompt = build_debate_prompt(specs[0], request, context, drafts, round_index=2, previous_debates=prior)
    assert "equal-peer debate round 2" in prompt
    assert "## Questions To Peers" in prompt
    assert "## Answers To Peer Questions" in prompt
    assert "## Previous debate rounds" in prompt
    assert "question for peer" in prompt
