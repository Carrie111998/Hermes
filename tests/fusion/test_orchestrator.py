import json
import subprocess
from pathlib import Path

from agent.fusion.models import FusionParticipantResult, FusionRequest
from agent.fusion.orchestrator import run_fusion


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )


def _init_repo(repo: Path) -> Path:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "fusion@example.test")
    _git(repo, "config", "user.name", "Fusion Test")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "init")
    return repo


def _approval_json(candidate_id: str, approved=True, dissent=None, changes=None) -> str:
    return "```json\n" + json.dumps({
        "candidate_id": candidate_id,
        "approved": approved,
        "material_dissent": dissent or [],
        "required_changes": changes or [],
        "unsupported_claims": [],
        "confidence": "high",
        "summary": "ok" if approved else "blocked",
    }) + "\n```"


def _runner_factory(*, reject_slug=None, leak_phase=None, first_round_changes=False, spike_write=False):
    calls = []

    def runner(spec, request, context, **kwargs):
        phase = kwargs.get("phase", "draft")
        calls.append((phase, spec.slug))
        if leak_phase == phase:
            (Path(context.repo_root) / "tracked.txt").write_text(f"leak from {phase}\n", encoding="utf-8")
        if phase == "draft":
            output = f"## Summary\n{spec.slug} draft\n\n## Material Axes\n" + "\n".join(
                f"- {axis}: {spec.slug} wording" for axis in [
                    "architecture", "approach", "key_assumptions", "repo_facts", "api_flag_config_claims", "risks_blockers", "implementation_sequence", "test_strategy", "migration_backcompat_claims"
                ]
            )
        elif phase.startswith("debate"):
            output = "## Debate Summary\n- agree\n\n## Material Dissent\n- none"
        elif phase.startswith("spike"):
            if spike_write:
                (Path(context.repo_root) / f"{spec.slug}-spike.txt").write_text("spike experiment\n", encoding="utf-8")
            output = "## Spike Findings\n- experiment complete\n\n## Candidate Revision Guidance\n- tighten safety wording"
        elif phase.startswith("vote"):
            candidate_id = "candidate-r" + phase.rsplit("-", 1)[1]
            if first_round_changes and phase == "vote-1" and spec.slug == "deepseek-deepseek-v4-pro":
                output = _approval_json(candidate_id, approved=False, changes=["tighten safety wording"])
            elif reject_slug == spec.slug:
                output = _approval_json(candidate_id, approved=False, dissent=["material blocker"])
            else:
                output = _approval_json(candidate_id, approved=True)
        else:
            output = "unknown phase"
        return FusionParticipantResult(spec=spec, status="completed", phase=phase, output=output)

    runner.calls = calls
    return runner


def _cfg():
    return {"fusion": {"model_pool": [
        {"provider": "zai", "model": "glm-5.2", "reasoning_effort": "xhigh"},
        {"provider": "deepseek", "model": "deepseek-v4-pro", "reasoning_effort": "xhigh"},
        {"provider": "openai-codex", "model": "gpt-5.5", "reasoning_effort": "xhigh"},
    ]}}


def test_orchestrator_runs_draft_debate_vote_and_writes_final_plan(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    runner = _runner_factory()
    result = run_fusion(
        FusionRequest(mode="plan", task="test consensus", repo_path=str(repo), output_root=str(tmp_path / "runs")),
        participant_runner=runner,
        config=_cfg(),
    )
    assert result.status == "converged"
    assert set(result.phases) == {"draft", "cross-verify-1", "wrong-layer", "debate-1", "premortem-1", "vote-1"}
    assert "debate-2" not in result.phases
    assert result.routing["task_kind"] == "design_wide_solution"
    assert result.brief["schema"] == "fusion-reference-brief/v1"
    assert (Path(result.run_dir) / "synthesis" / "final_plan.md").exists()
    assert (Path(result.run_dir) / "verification" / "votes.json").exists()
    assert result.model_diversity["distinct_count"] == 3


def test_orchestrator_operator_decision_has_no_final_plan_on_vote_dissent(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    result = run_fusion(
        FusionRequest(mode="plan", task="test conflict", repo_path=str(repo), output_root=str(tmp_path / "runs")),
        participant_runner=_runner_factory(reject_slug="openai-codex-gpt-5.5"),
        config=_cfg(),
    )
    assert result.status == "operator_decision"
    assert "vote-5" in result.phases
    assert len(result.candidates) == 5
    assert (Path(result.run_dir) / "synthesis" / "operator_decision.md").exists()
    assert not (Path(result.run_dir) / "synthesis" / "final_plan.md").exists()


def test_orchestrator_revises_candidate_when_vote_has_required_changes(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    result = run_fusion(
        FusionRequest(mode="plan", task="test revision", repo_path=str(repo), output_root=str(tmp_path / "runs"), convergence_rounds=2),
        participant_runner=_runner_factory(first_round_changes=True, spike_write=True),
        config=_cfg(),
    )
    assert result.status == "converged"
    assert [candidate.id for candidate in result.candidates] == ["candidate-r1", "candidate-r2"]
    assert "vote-2" in result.phases
    assert "spike-1" in result.phases
    assert "probe-1" not in result.phases
    assert "premortem-2" in result.phases
    assert "debate-2" in result.phases
    assert "debate-3" not in result.phases
    assert result.spikes
    assert all(spike.cleanup_ok for spike in result.spikes if spike.available)
    assert "spike" in result.candidates[-1].content.lower()
    assert not any(repo.glob("*-spike.txt"))


def test_orchestrator_stops_on_write_leak_in_any_phase(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    result = run_fusion(
        FusionRequest(mode="plan", task="test leak", repo_path=str(repo), output_root=str(tmp_path / "runs")),
        participant_runner=_runner_factory(leak_phase="debate-1"),
        config=_cfg(),
    )
    assert result.status == "write_leak"
    assert result.write_leak is True
    assert (Path(result.run_dir) / "synthesis" / "write_leak_report.md").exists()
    assert not (Path(result.run_dir) / "synthesis" / "final_plan.md").exists()


def test_model_diversity_error_stops_before_participant_execution(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    runner = _runner_factory()
    result = run_fusion(
        FusionRequest(mode="plan", task="bad diversity", repo_path=str(repo), output_root=str(tmp_path / "runs")),
        participant_runner=runner,
        config={"fusion": {"model_pool": [{"provider": "openai-codex", "model": "gpt-5.5"}], "allow_homogeneous_models": False}},
    )
    assert result.status == "model_diversity_error"
    assert runner.calls == []


def test_bug_shaped_task_runs_locate_before_draft(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    runner = _runner_factory()
    result = run_fusion(
        FusionRequest(mode="plan", task="why is auth broken with traceback", repo_path=str(repo), output_root=str(tmp_path / "runs")),
        participant_runner=runner,
        config=_cfg(),
    )
    assert result.routing["task_kind"] == "bug_unknown_root"
    assert result.routing["locate_required"] is True
    assert "locate" in result.phases
    assert runner.calls[0][0] == "locate"
    assert (Path(result.run_dir) / "brief" / "brief.md").exists()
