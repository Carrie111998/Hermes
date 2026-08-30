from agent.fusion.config import participant_specs_for_request
from agent.fusion.context import FusionContext
from agent.fusion.models import FusionCandidate, FusionParticipantResult, FusionRequest
from agent.fusion.prompts import (
    build_cross_verify_prompt,
    build_locate_prompt,
    build_premortem_prompt,
    build_probe_prompt,
    build_spike_prompt,
    build_wrong_layer_prompt,
)
from agent.fusion.verification import verify_convergence_votes


def _context():
    return FusionContext(repo_root="/tmp/repo", cwd="/tmp/repo", repo_guard_available=True, notes=[])


def _specs():
    return participant_specs_for_request(FusionRequest(mode="plan", task="x", participants=2), config={"fusion": {"model_pool": [
        {"provider": "zai", "model": "glm-5.2"},
        {"provider": "deepseek", "model": "deepseek-v4-pro"},
    ]}})


def _brief():
    return {
        "markdown": "# Fusion Evidence Brief\n\n## Layers covered / Layers NOT covered\n- covered: orchestration\n",
        "routing": {"task_kind": "bug_unknown_root", "locate_required": True},
    }


def test_locate_prompt_enforces_evidence_before_plan():
    spec = _specs()[0]
    prompt = build_locate_prompt(spec, FusionRequest(mode="plan", task="why broken"), _context(), _brief())
    assert "LOCATE before planning" in prompt
    assert "Do NOT propose the final plan yet" in prompt
    assert "Missing Probes" in prompt


def test_cross_verify_prompt_targets_peer_and_requires_verdict():
    specs = _specs()
    target = FusionParticipantResult(spec=specs[1], status="completed", phase="draft", output="## Summary\npeer draft")
    prompt = build_cross_verify_prompt(specs[0], FusionRequest(mode="plan", task="x"), _context(), target, _brief())
    assert "Target draft author" in prompt
    assert specs[1].slug in prompt
    assert "VERDICT: verified|issues-found|blocked" in prompt
    assert "Nobody grades themselves" in prompt


def test_wrong_layer_prompt_challenges_solution_boundary():
    specs = _specs()
    draft = FusionParticipantResult(spec=specs[0], status="completed", phase="draft", output="root is cli")
    prompt = build_wrong_layer_prompt(specs[1], FusionRequest(mode="plan", task="x"), _context(), [draft], [], _brief())
    assert "wrong-layer" in prompt
    assert "outside the shown or over-discussed layer" in prompt
    assert "Required Probes" in prompt


def test_spike_prompt_allows_only_isolated_worktree_writes():
    spec = _specs()[0]
    vote_result = FusionParticipantResult(
        spec=spec,
        status="completed",
        phase="vote-1",
        output='```json\n{"candidate_id":"candidate-r1","approved":false,"material_dissent":["unsafe"],"required_changes":["add guard"],"unsupported_claims":[],"confidence":"high","summary":"blocked"}\n```',
    )
    report = verify_convergence_votes([vote_result], candidate_id="candidate-r1", total_participants=1)
    candidate = FusionCandidate(id="candidate-r1", round_index=1, content="# candidate\n")
    prompt = build_spike_prompt(
        spec,
        FusionRequest(mode="plan", task="x"),
        _context(),
        report,
        candidate,
        worktree_root="/tmp/fusion-spike",
        brief=_brief(),
    )
    assert "isolated spike worktree" in prompt
    assert "write_file/patch" in prompt
    assert "ONLY inside" in prompt
    assert "Do not run shell commands" in prompt
    assert "/tmp/fusion-spike" in prompt


def test_probe_and_premortem_prompts_feed_later_consensus():
    spec = _specs()[0]
    vote_result = FusionParticipantResult(
        spec=spec,
        status="completed",
        phase="vote-1",
        output='```json\n{"candidate_id":"candidate-r1","approved":false,"material_dissent":["unsafe"],"required_changes":["add guard"],"unsupported_claims":[],"confidence":"high","summary":"blocked"}\n```',
    )
    report = verify_convergence_votes([vote_result], candidate_id="candidate-r1", total_participants=1)
    candidate = FusionCandidate(id="candidate-r1", round_index=1, content="# candidate\n")
    probe = build_probe_prompt(spec, FusionRequest(mode="plan", task="x"), _context(), report, candidate, _brief())
    premortem = build_premortem_prompt(spec, FusionRequest(mode="plan", task="x"), _context(), candidate, [], _brief())
    assert "read-only probe" in probe
    assert "unsafe" in probe
    assert "pre-mortem" in premortem
    assert "Blocks Convergence" in premortem
