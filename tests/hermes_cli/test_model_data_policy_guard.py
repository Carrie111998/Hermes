"""Tests for the data-training-tier selection guard."""

from hermes_cli.model_data_policy_guard import (
    DataTrainingWarning,
    data_training_warning,
)


def test_fires_on_meta_contributor():
    w = data_training_warning("muse-spark-1.2-contributor", provider="meta-ai")
    assert isinstance(w, DataTrainingWarning)
    assert w.model == "muse-spark-1.2-contributor"
    assert "train" in w.message.lower()
    assert "muse-spark-1.2" in w.message  # points to the no-training alternative
    # Aligns with Meta's own pricing doc language + figures.
    assert "$0.10" in w.message and "$0.20" in w.message and "$0.002" in w.message
    assert "prompts and completions" in w.message.lower()
    assert "dev.meta.ai/docs/pricing-rate-limits" in w.message


def test_fires_on_meta_contributor_1_3_with_correct_model_name():
    # Regression: the predicate matched 1.3 via suffix but the message named
    # 1.2. The warning must name the triggering model and its standard variant.
    w = data_training_warning("muse-spark-1.3-contributor", provider="meta-ai")
    assert isinstance(w, DataTrainingWarning)
    assert w.model == "muse-spark-1.3-contributor"
    assert "muse-spark-1.3-contributor" in w.message
    assert "muse-spark-1.3." in w.message  # points to the no-training alternative
    assert "muse-spark-1.2" not in w.message  # must not name the older version
    # 1.3 standard pricing verified via OpenRouter — comparison present, but
    # cached figures (verified for 1.2 only) must not leak into the 1.3 message.
    assert "$0.10" in w.message and "$0.20" in w.message
    assert "$1.25" in w.message and "$4.25" in w.message
    assert "$0.002" not in w.message and "$0.15" not in w.message
    assert "train" in w.message.lower()
    assert "dev.meta.ai/docs/pricing-rate-limits" in w.message


def test_silent_on_non_contributor_muse():
    assert data_training_warning("muse-spark-1.2", provider="meta-ai") is None
    assert data_training_warning("muse-spark-1.1", provider="meta-ai") is None


def test_silent_on_unrelated_models():
    for m in ("anthropic/claude-opus-4.8", "gpt-5.6-sol", "deepseek-v4-pro", ""):
        assert data_training_warning(m, provider="anthropic") is None


def test_fires_regardless_of_provider_string():
    # id-keyed: must fire even if selected via custom/gateway (no meta-ai provider)
    assert data_training_warning("muse-spark-1.2-contributor", provider="custom") is not None
    assert data_training_warning("muse-spark-1.2-contributor") is not None


def test_case_insensitive():
    assert data_training_warning("MUSE-SPARK-1.2-CONTRIBUTOR", provider="meta-ai") is not None
