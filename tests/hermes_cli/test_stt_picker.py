"""Tests for the Speech-to-Text category in `hermes tools` (tools_config).

Covers the STT provider picker rows, config writes (stt.provider /
use_gateway), the model picker catalog, config-only checklist exclusion,
and the faster_whisper post-setup readiness hook.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hermes_cli.tools_config import (  # noqa: E402
    _CONFIG_ONLY_TOOLSETS,
    CONFIGURABLE_TOOLSETS,
    STT_MODEL_CATALOG,
    TOOL_CATEGORIES,
    _checklist_toolset_keys,
    _configure_stt_model,
    _is_provider_active,
    _write_provider_config,
    apply_provider_selection,
)
from hermes_cli.models import fetch_openrouter_transcription_models  # noqa: E402


def _stt_cat():
    return TOOL_CATEGORIES["stt"]


def _stt_provider_named(name):
    return next(p for p in _stt_cat()["providers"] if p["name"] == name)


class TestSttCategory:
    def test_stt_category_exists(self):
        cat = _stt_cat()
        assert cat["name"] == "Speech-to-Text"
        assert len(cat["providers"]) >= 5

    def test_openrouter_is_a_first_class_stt_provider(self):
        provider = _stt_provider_named("OpenRouter")
        assert provider["stt_provider"] == "openrouter"
        assert [row["key"] for row in provider["env_vars"]] == ["OPENROUTER_API_KEY"]




    def test_managed_row_shares_tts_coverage_category(self):
        from hermes_cli.nous_subscription import MANAGED_FEATURE_COVERAGE_CATEGORY

        managed = [p for p in _stt_cat()["providers"] if p.get("managed_nous_feature")]
        assert managed, "expected a Nous Subscription row"
        for p in managed:
            assert p["managed_nous_feature"] == "stt"
        assert MANAGED_FEATURE_COVERAGE_CATEGORY["stt"] == "openai-audio"


class TestConfigWrites:
    def test_write_provider_config_sets_stt_provider(self):
        config = {}
        prov = _stt_provider_named("Groq")
        _write_provider_config(prov, config, managed_feature=None)
        assert config["stt"]["provider"] == "groq"
        assert config["stt"]["use_gateway"] is False


    def test_apply_provider_selection_stt(self):
        config = {}
        with patch(
            "hermes_cli.tools_config.get_nous_subscription_features"
        ) as feats:
            feats.return_value = MagicMock(
                nous_auth_present=False, account_info=None
            )
            apply_provider_selection("stt", "OpenAI", config)
        assert config["stt"]["provider"] == "openai"


class TestActiveDetection:
    def test_active_matches_config(self):
        config = {"stt": {"provider": "groq"}}
        assert _is_provider_active(_stt_provider_named("Groq"), config)
        assert not _is_provider_active(_stt_provider_named("OpenAI"), config)

    def test_unset_provider_defaults_to_local(self):
        assert _is_provider_active(_stt_provider_named("Local Whisper"), {})


class TestModelPicker:

    def test_catalog_matches_runtime_model_sets(self):
        from tools.transcription_tools import GROQ_MODELS, OPENAI_MODELS

        assert set(STT_MODEL_CATALOG["openai"]) == OPENAI_MODELS
        assert set(STT_MODEL_CATALOG["groq"]) == GROQ_MODELS

    def test_openrouter_catalog_is_live_and_filtered_to_transcription_models(self):
        response = MagicMock()
        response.read.return_value = b'{"data":[{"id":"openai/gpt-4o-transcribe","name":"GPT-4o Transcribe","architecture":{"output_modalities":["transcription"]}},{"id":"chat/model","name":"Chat","architecture":{"output_modalities":["text"]}}]}'
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with patch("hermes_cli.models._urlopen_model_catalog_request", return_value=response):
            models = fetch_openrouter_transcription_models(force_refresh=True)
        assert models == [("openai/gpt-4o-transcribe", "GPT-4o Transcribe")]

    def test_configure_openrouter_model_uses_live_catalog(self):
        config = {"stt": {"openrouter": {"model": "openai/gpt-4o-transcribe"}}}
        live = [
            ("openai/gpt-4o-mini-transcribe", "GPT-4o Mini Transcribe"),
            ("openai/gpt-4o-transcribe", "GPT-4o Transcribe"),
        ]
        with patch("hermes_cli.models.fetch_openrouter_transcription_models", return_value=live), patch(
            "hermes_cli.tools_config._prompt_choice", return_value=1
        ) as pc:
            _configure_stt_model("openrouter", config)
        assert pc.call_args[0][1] == [model_id for model_id, _ in live]
        assert pc.call_args[0][2] == 1
        assert config["stt"]["openrouter"]["model"] == "openai/gpt-4o-transcribe"




    def test_configure_stt_model_defaults_to_current(self):
        config = {"stt": {"openai": {"model": "gpt-transcribe"}}}
        with patch(
            "hermes_cli.tools_config._prompt_choice", return_value=0
        ) as pc:
            _configure_stt_model("openai", config)
        # default index should point at the currently configured model
        args = pc.call_args[0]
        assert args[2] == STT_MODEL_CATALOG["openai"].index("gpt-transcribe")


class TestConfigOnlyExclusion:
    def test_stt_is_config_only(self):
        assert "stt" in _CONFIG_ONLY_TOOLSETS

    def test_stt_excluded_from_checklist_universe(self):
        assert "stt" not in _checklist_toolset_keys("cli")
        # sanity: tts (a real toolset) stays in
        assert "tts" in _checklist_toolset_keys("cli")


class TestPostSetup:
    def test_faster_whisper_in_post_setup_ready(self):
        from hermes_cli.tools_config import _POST_SETUP_READY

        assert "faster_whisper" in _POST_SETUP_READY
