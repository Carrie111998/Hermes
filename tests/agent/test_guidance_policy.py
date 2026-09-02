"""Regression coverage for model guidance policy."""

import pytest

from agent.system_prompt import _guidance_config_enabled


@pytest.mark.parametrize(
    "model",
    [
        "muse-spark-1.2-contributor",
        "anthropic/claude-sonnet-4.6",
        "openai/gpt-5.6",
        "google/gemini-3-pro",
        "qwen/qwen3-coder",
        "totally-new-model-family-1",
    ],
)
def test_auto_guidance_applies_to_every_model(model):
    assert _guidance_config_enabled("auto", model, ("gpt", "qwen")) is True


def test_explicit_false_still_disables_guidance():
    assert _guidance_config_enabled(False, "muse-spark-1.2", ("muse",)) is False
    assert _guidance_config_enabled("off", "muse-spark-1.2", ("muse",)) is False


def test_custom_model_list_still_targets_by_substring():
    setting = ["spark", "qwen"]
    assert _guidance_config_enabled(setting, "muse-spark-1.2", ()) is True
    assert _guidance_config_enabled(setting, "claude-sonnet-4.6", ()) is False


def test_unrecognised_value_keeps_curated_fallback():
    assert _guidance_config_enabled("unexpected", "qwen3-coder", ("qwen",)) is True
    assert _guidance_config_enabled("unexpected", "claude-sonnet-4.6", ("qwen",)) is False
