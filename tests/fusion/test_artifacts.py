import json
from pathlib import Path

from agent.fusion.artifacts import write_fusion_artifacts
from agent.fusion.models import (
    FusionCandidate,
    FusionConvergenceVote,
    FusionParticipantResult,
    FusionParticipantSpec,
    FusionRequest,
    FusionResult,
    FusionSpikeRun,
)


def test_artifacts_record_models_phases_candidates_and_votes(tmp_path):
    spec = FusionParticipantSpec(slug="glm-max", role="Equal peer participant", focus="x", provider="zai", model="glm-5.2", reasoning_effort="xhigh")
    draft = FusionParticipantResult(spec=spec, status="completed", phase="draft", output="draft", provider="zai", model="glm-5.2")
    vote = FusionConvergenceVote(participant="glm-max", candidate_id="candidate-r1", approved=True)
    result = FusionResult(
        status="converged",
        request=FusionRequest(mode="plan", task="x"),
        run_dir=str(tmp_path / "run"),
        participants=[draft],
        phases={"draft": [draft]},
        candidates=[FusionCandidate(id="candidate-r1", round_index=1, content="# candidate\n")],
        spikes=[FusionSpikeRun(round_index=1, phase="spike-1", worktree_path="/tmp/spike", available=True, cleanup_ok=True, diff_stat="tracked.txt | 2 +-", diff="diff --git a/tracked.txt b/tracked.txt")],
        votes=[vote],
        model_diversity={"distinct_count": 1, "participants": [{"slug": "glm-max", "provider": "zai", "model": "glm-5.2"}]},
        routing={"task_kind": "design_wide_solution", "locate_required": False},
        brief={"schema": "fusion-reference-brief/v1", "markdown": "# Fusion Evidence Brief\n", "layers": {"covered": ["tests"], "not_covered": []}},
        coverage={"requested": 1, "draft_successful": 1, "total": 1, "degraded": False},
        decision="consensus",
    )
    write_fusion_artifacts(result)
    manifest = json.loads((Path(result.run_dir) / "manifest.json").read_text(encoding="utf-8"))
    status = json.loads((Path(result.run_dir) / "status.json").read_text(encoding="utf-8"))
    votes = json.loads((Path(result.run_dir) / "verification" / "votes.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "fusion-v2-run/v2"
    assert manifest["model_diversity"]["distinct_count"] == 1
    assert manifest["routing"]["task_kind"] == "design_wide_solution"
    assert manifest["coverage"]["requested"] == 1
    assert manifest["spikes"][0]["phase"] == "spike-1"
    assert status["schema"] == "fusion-v2-status/v2"
    assert status["decision"] == "consensus"
    assert status["coverage"]["degraded"] is False
    assert votes[0]["participant"] == "glm-max"
    assert (Path(result.run_dir) / "routing.json").exists()
    assert (Path(result.run_dir) / "brief" / "brief.md").exists()
    assert (Path(result.run_dir) / "participants" / "glm-max" / "draft.md").exists()
    assert (Path(result.run_dir) / "synthesis" / "candidate-r1.md").exists()
    assert (Path(result.run_dir) / "spikes" / "spikes.json").exists()
    assert (Path(result.run_dir) / "spikes" / "spike-1" / "diff.patch").exists()
