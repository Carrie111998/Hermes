import pytest

from agent.fusion.config import ModelDiversityError, normalize_request, participant_specs_for_request, parse_model_spec
from agent.fusion.models import FusionRequest


def test_default_fusion_roster_is_heterogeneous_and_max_reasoning():
    specs = participant_specs_for_request(FusionRequest(mode="plan", task="x", participants=3))
    labels = {spec.runtime_label for spec in specs}
    assert labels == {
        "zai:glm-5.2",
        "deepseek:deepseek-v4-pro",
        "openai-codex:gpt-5.5",
    }
    assert {spec.reasoning_effort for spec in specs} == {"xhigh"}
    assert [spec.slug for spec in specs] == ["glm-max", "deepseek-pro-max", "codex-default"]
    assert {spec.role for spec in specs} == {"Equal peer participant"}
    assert all("full-task analysis" in spec.focus for spec in specs)


def test_model_diversity_error_happens_before_execution_when_pool_is_homogeneous():
    cfg = {
        "fusion": {
            "model_pool": [
                {"provider": "openai-codex", "model": "gpt-5.5"},
                {"provider": "openai-codex", "model": "gpt-5.5"},
            ],
            "min_distinct_models": 2,
            "allow_homogeneous_models": False,
        }
    }
    with pytest.raises(ModelDiversityError):
        participant_specs_for_request(FusionRequest(mode="plan", task="x", participants=3), config=cfg)


def test_homogeneous_override_is_explicit_and_noisy():
    cfg = {
        "fusion": {
            "model_pool": [{"provider": "openai-codex", "model": "gpt-5.5"}],
            "allow_homogeneous_models": True,
        }
    }
    specs = participant_specs_for_request(FusionRequest(mode="plan", task="x", participants=2, allow_homogeneous_models=True), config=cfg)
    assert [spec.runtime_label for spec in specs] == ["openai-codex:gpt-5.5", "openai-codex:gpt-5.5"]


def test_parse_cli_model_spec_keeps_slash_model_and_reasoning():
    parsed = parse_model_spec("openrouter:google/gemini-3-pro@xhigh")
    assert parsed["provider"] == "openrouter"
    assert parsed["model"] == "google/gemini-3-pro"
    assert parsed["reasoning_effort"] == "xhigh"


def test_default_fusion_round_limit_is_five_with_early_exit_capacity():
    request = normalize_request(FusionRequest(mode="plan", task="x"))
    assert request.debate_rounds == 5
    assert request.convergence_rounds == 5
    assert request.spike_worktrees is True
