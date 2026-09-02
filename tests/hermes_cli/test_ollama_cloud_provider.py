"""Tests for Ollama Cloud provider integration."""

import pytest
from unittest.mock import patch, MagicMock

from hermes_cli.auth import PROVIDER_REGISTRY, resolve_provider, resolve_api_key_provider_credentials
from hermes_cli.models import _PROVIDER_MODELS, _PROVIDER_LABELS, _PROVIDER_ALIASES, normalize_provider
from hermes_cli.model_normalize import normalize_model_for_provider
from agent.model_metadata import _URL_TO_PROVIDER, _PROVIDER_PREFIXES
from agent.models_dev import PROVIDER_TO_MODELS_DEV, list_agentic_models


# ── Provider Registry ──

class TestOllamaCloudProviderRegistry:
    def test_ollama_cloud_in_registry(self):
        assert "ollama-cloud" in PROVIDER_REGISTRY

    def test_ollama_cloud_config(self):
        pconfig = PROVIDER_REGISTRY["ollama-cloud"]
        assert pconfig.id == "ollama-cloud"
        assert pconfig.name == "Ollama Cloud"
        assert pconfig.auth_type == "api_key"
        assert pconfig.inference_base_url == "https://ollama.com/v1"


# ── Provider Aliases ──

PROVIDER_ENV_VARS = (
    "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY", "GEMINI_API_KEY", "OLLAMA_API_KEY",
    "GLM_API_KEY", "ZAI_API_KEY", "KIMI_API_KEY",
    "MINIMAX_API_KEY", "DEEPSEEK_API_KEY",
)

@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch):
    for var in PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class TestOllamaCloudAliases:

    def test_alias_ollama_underscore(self):
        """ollama_cloud (underscore) is the unambiguous cloud alias."""
        assert resolve_provider("ollama_cloud") == "ollama-cloud"


    def test_models_py_aliases(self):
        assert _PROVIDER_ALIASES.get("ollama_cloud") == "ollama-cloud"
        # bare "ollama" stays local
        assert _PROVIDER_ALIASES.get("ollama") == "custom"


# ── Auto-detection ──

class TestOllamaCloudAutoDetection:
    def test_auto_detects_ollama_api_key(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_API_KEY", "test-ollama-key")
        assert resolve_provider("auto") == "ollama-cloud"


# ── Credential Resolution ──

class TestOllamaCloudCredentials:
    def test_resolve_with_ollama_api_key(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_API_KEY", "ollama-secret")
        creds = resolve_api_key_provider_credentials("ollama-cloud")
        assert creds["provider"] == "ollama-cloud"
        assert creds["api_key"] == "ollama-secret"
        assert creds["base_url"] == "https://ollama.com/v1"


    def test_runtime_ollama_cloud(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_API_KEY", "ollama-key")
        from hermes_cli.runtime_provider import resolve_runtime_provider
        result = resolve_runtime_provider(requested="ollama-cloud")
        assert result["provider"] == "ollama-cloud"
        assert result["api_mode"] == "chat_completions"
        assert result["api_key"] == "ollama-key"
        assert result["base_url"] == "https://ollama.com/v1"


# ── Model Catalog (dynamic — no static list) ──

class TestOllamaCloudModelCatalog:


    def test_provider_model_ids_returns_dynamic_models(self, tmp_path, monkeypatch):
        """provider_model_ids('ollama-cloud') should call fetch_ollama_cloud_models()."""
        from hermes_cli.models import provider_model_ids

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("OLLAMA_API_KEY", "test-key")

        mock_mdev = {
            "ollama-cloud": {
                "models": {
                    "qwen3.5:397b": {"tool_call": True},
                    "glm-5": {"tool_call": True},
                }
            }
        }
        with patch("hermes_cli.models.fetch_api_models", return_value=["qwen3.5:397b"]), \
             patch("agent.models_dev.fetch_models_dev", return_value=mock_mdev):
            result = provider_model_ids("ollama-cloud", force_refresh=True)

        assert len(result) > 0
        assert "qwen3.5:397b" in result


# ── Model Picker (list_authenticated_providers) ──

class TestOllamaCloudModelPicker:
    def test_ollama_cloud_shows_model_count(self, tmp_path, monkeypatch):
        """Ollama Cloud should show non-zero model count in provider picker."""
        from hermes_cli.model_switch import list_authenticated_providers

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("OLLAMA_API_KEY", "test-key")

        mock_mdev = {
            "ollama-cloud": {
                "models": {
                    "qwen3.5:397b": {"tool_call": True},
                    "glm-5": {"tool_call": True},
                }
            }
        }
        with patch("hermes_cli.models.fetch_api_models", return_value=["qwen3.5:397b"]), \
             patch("agent.models_dev.fetch_models_dev", return_value=mock_mdev):
            providers = list_authenticated_providers(current_provider="ollama-cloud")

        ollama = next((p for p in providers if p["slug"] == "ollama-cloud"), None)
        assert ollama is not None, "ollama-cloud should appear when OLLAMA_API_KEY is set"
        assert ollama["total_models"] > 0, "ollama-cloud should show non-zero model count"

    def test_ollama_cloud_not_shown_without_creds(self, monkeypatch):
        """Ollama Cloud should not appear without credentials."""
        from hermes_cli.model_switch import list_authenticated_providers

        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

        providers = list_authenticated_providers(current_provider="openrouter")
        ollama = next((p for p in providers if p["slug"] == "ollama-cloud"), None)
        assert ollama is None, "ollama-cloud should not appear without OLLAMA_API_KEY"


# ── Merged Model Discovery ──

class TestOllamaCloudMergedDiscovery:
    def test_merges_live_and_models_dev(self, tmp_path, monkeypatch):
        """Live API models appear first, models.dev additions fill gaps."""
        from hermes_cli.models import fetch_ollama_cloud_models

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("OLLAMA_API_KEY", "test-key")

        mock_mdev = {
            "ollama-cloud": {
                "models": {
                    "glm-5": {"tool_call": True},
                    "kimi-k2.5": {"tool_call": True},
                    "nemotron-3-super": {"tool_call": True},
                }
            }
        }
        with patch("hermes_cli.models.fetch_api_models", return_value=["qwen3.5:397b", "glm-5"]), \
             patch("agent.models_dev.fetch_models_dev", return_value=mock_mdev):
            result = fetch_ollama_cloud_models(force_refresh=True)

        # Live models first, then models.dev additions (deduped)
        assert result[0] == "qwen3.5:397b"  # from live API
        assert result[1] == "glm-5"          # from live API (also in models.dev)
        assert "kimi-k2.5" in result         # from models.dev only
        assert "nemotron-3-super" in result  # from models.dev only
        assert result.count("glm-5") == 1    # no duplicates

    def test_falls_back_to_models_dev_without_api_key(self, tmp_path, monkeypatch):
        """Without API key, only models.dev results are returned."""
        from hermes_cli.models import fetch_ollama_cloud_models

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

        mock_mdev = {
            "ollama-cloud": {
                "models": {
                    "glm-5": {"tool_call": True},
                }
            }
        }
        with patch("agent.models_dev.fetch_models_dev", return_value=mock_mdev):
            result = fetch_ollama_cloud_models(force_refresh=True)

        assert result == ["glm-5"]






# ── Model Normalization ──

class TestOllamaCloudModelNormalization:


    def test_passthrough_no_tag(self):
        assert normalize_model_for_provider("glm-5", "ollama-cloud") == "glm-5"


# ── URL-to-Provider Mapping ──


# ── models.dev Integration ──

class TestOllamaCloudModelsDev:
    def test_ollama_cloud_mapped(self):
        assert PROVIDER_TO_MODELS_DEV.get("ollama-cloud") == "ollama-cloud"

    def test_list_agentic_models_with_mock_data(self):
        """list_agentic_models filters correctly from mock models.dev data."""
        mock_data = {
            "ollama-cloud": {
                "models": {
                    "qwen3.5:397b": {"tool_call": True},
                    "glm-5": {"tool_call": True},
                    "nemotron-3-nano:30b": {"tool_call": True},
                    "some-embedding:latest": {"tool_call": False},
                }
            }
        }
        with patch("agent.models_dev.fetch_models_dev", return_value=mock_data):
            result = list_agentic_models("ollama-cloud")
        assert "qwen3.5:397b" in result
        assert "glm-5" in result
        assert "nemotron-3-nano:30b" in result
        assert "some-embedding:latest" not in result  # no tool_call


# ── Agent Init (no SyntaxError) ──

class TestOllamaCloudAgentInit:
    def test_agent_imports_without_error(self):
        """Verify run_agent.py has no SyntaxError."""
        import importlib
        import run_agent
        importlib.reload(run_agent)

    def test_ollama_cloud_agent_uses_chat_completions(self, monkeypatch):
        """Ollama Cloud falls through to chat_completions — no special elif needed."""
        monkeypatch.setenv("OLLAMA_API_KEY", "test-key")
        with patch("run_agent.OpenAI") as mock_openai:
            mock_openai.return_value = MagicMock()
            from run_agent import AIAgent
            agent = AIAgent(
                model="qwen3.5:397b",
                provider="ollama-cloud",
                api_key="test-key",
                base_url="https://ollama.com/v1",
            )
            assert agent.api_mode == "chat_completions"
            assert agent.provider == "ollama-cloud"


# ── providers.py New System ──

class TestOllamaCloudProvidersNew:
    def test_overlay_exists(self):
        from hermes_cli.providers import HERMES_OVERLAYS
        assert "ollama-cloud" in HERMES_OVERLAYS
        overlay = HERMES_OVERLAYS["ollama-cloud"]
        assert overlay.transport == "openai_chat"
        assert overlay.base_url_env_var == "OLLAMA_BASE_URL"

    def test_alias_resolves(self):
        from hermes_cli.providers import normalize_provider as np
        assert np("ollama") == "custom"  # bare "ollama" = local
        assert np("ollama-cloud") == "ollama-cloud"


    def test_get_provider(self):
        from hermes_cli.providers import get_provider
        pdef = get_provider("ollama-cloud")
        assert pdef is not None
        assert pdef.id == "ollama-cloud"
        assert pdef.transport == "openai_chat"


# ── Cloud Suffix Stripping ──

class TestOllamaCloudSuffixStripping:
    """models.dev appends :cloud / -cloud suffixes that the live API omits.

    fetch_ollama_cloud_models() must normalise these before the dedup merge so
    users never see broken IDs like 'kimi-k2.6:cloud' in the model picker.
    """


    def test_no_duplicate_when_live_clean_and_mdev_suffixed(self, tmp_path, monkeypatch):
        """Live API returns clean ID; mdev has :cloud variant — result has exactly one entry."""
        from hermes_cli.models import fetch_ollama_cloud_models

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("OLLAMA_API_KEY", "test-key")

        mock_mdev = {
            "ollama-cloud": {
                "models": {
                    "kimi-k2.6:cloud": {"tool_call": True},
                    "glm-5.1:cloud": {"tool_call": True},
                }
            }
        }
        with patch("hermes_cli.models.fetch_api_models", return_value=["kimi-k2.6", "glm-5.1"]), \
             patch("agent.models_dev.fetch_models_dev", return_value=mock_mdev):
            result = fetch_ollama_cloud_models(force_refresh=True)

        assert result.count("kimi-k2.6") == 1
        assert result.count("glm-5.1") == 1
        assert "kimi-k2.6:cloud" not in result
        assert "glm-5.1:cloud" not in result

    def test_unsuffixed_model_id_unchanged(self, tmp_path, monkeypatch):
        """Model IDs without :cloud / -cloud suffix are passed through unchanged."""
        from hermes_cli.models import fetch_ollama_cloud_models

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

        mock_mdev = {
            "ollama-cloud": {
                "models": {"nemotron-3-nano:30b": {"tool_call": True}}
            }
        }
        with patch("agent.models_dev.fetch_models_dev", return_value=mock_mdev):
            result = fetch_ollama_cloud_models(force_refresh=True)

        assert "nemotron-3-nano:30b" in result

    def test_strip_suffix_helper(self):
        """Unit test for the _strip_ollama_cloud_suffix helper."""
        from hermes_cli.models import _strip_ollama_cloud_suffix

        assert _strip_ollama_cloud_suffix("kimi-k2.6:cloud") == "kimi-k2.6"
        assert _strip_ollama_cloud_suffix("glm-5.1:cloud") == "glm-5.1"
        assert _strip_ollama_cloud_suffix("qwen3-coder:480b-cloud") == "qwen3-coder:480b"
        assert _strip_ollama_cloud_suffix("nemotron-3-nano:30b") == "nemotron-3-nano:30b"
        assert _strip_ollama_cloud_suffix("") == ""


# ── Credential Resolution & Cache Preservation (#98243) ──

class TestOllamaCloudCredentialResolution:
    """provider_model_ids must feed the live probe with the auth-store credential.

    Desktop/service processes are launched from the GUI and inherit no shell
    exports, so an OLLAMA_API_KEY the user logged in with (in ~/.hermes/.env
    or the credential pool) never reached the probe when it only consulted
    os.environ — the cache then regressed to the models.dev subset and the
    picker dropped models the CLI still listed (#98243).
    """

    def test_provider_model_ids_passes_auth_store_key_to_probe(self, tmp_path, monkeypatch):
        import os as _os

        from hermes_cli.models import provider_model_ids

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        # The fake credential lives in the environment, matching how the
        # auth-store seam surfaces a real key to the resolver.
        monkeypatch.setenv("MOCK_AUTHSTORE_OLLAMA_KEY", "authstore-key")

        def _fake_resolve(pid):
            return {
                "api_key": _os.environ["MOCK_AUTHSTORE_OLLAMA_KEY"],
                "base_url": "https://ollama.com/v1",
            }

        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            side_effect=_fake_resolve,
        ) as mock_resolve, \
             patch("hermes_cli.models.fetch_api_models", return_value=["deepseek-v4-flash:0731"]) as mock_fetch, \
             patch("agent.models_dev.fetch_models_dev", return_value={}):
            result = provider_model_ids("ollama-cloud", force_refresh=True)

        mock_resolve.assert_called_once_with("ollama-cloud")
        assert mock_fetch.call_args.args[0] == "authstore-key"
        assert "deepseek-v4-flash:0731" in result

    def test_provider_model_ids_survives_auth_resolution_failure(self, tmp_path, monkeypatch):
        """A failing auth resolution falls back to the env-key path, not an exception."""
        from hermes_cli.models import provider_model_ids

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("OLLAMA_API_KEY", "env-key")

        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            side_effect=Exception("auth store unreadable"),
        ), \
             patch("hermes_cli.models.fetch_api_models", return_value=["glm-5.3"]) as mock_fetch, \
             patch("agent.models_dev.fetch_models_dev", return_value={}):
            result = provider_model_ids("ollama-cloud", force_refresh=True)

        assert mock_fetch.call_args.args[0] == "env-key"
        assert "glm-5.3" in result


class TestOllamaCloudFailedProbeCachePreservation:
    """An empty live probe must not overwrite the cache with the models.dev subset."""

    def test_failed_probe_keeps_cached_models(self, tmp_path, monkeypatch):
        import json
        import time as _time

        from hermes_cli.models import fetch_ollama_cloud_models, _ollama_cloud_cache_path

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

        cache_path = _ollama_cloud_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "models": ["deepseek-v4-flash:0731", "mistral-large-3:675b"],
            "cached_at": _time.time() - 7200,
        }))

        mock_mdev = {"ollama-cloud": {"models": {"glm-5": {"tool_call": True}}}}
        with patch("hermes_cli.models.fetch_api_models", return_value=[]), \
             patch("agent.models_dev.fetch_models_dev", return_value=mock_mdev):
            result = fetch_ollama_cloud_models(force_refresh=True)

        assert "glm-5" in result
        assert "deepseek-v4-flash:0731" in result
        assert "mistral-large-3:675b" in result

        saved = json.loads(cache_path.read_text())
        assert "deepseek-v4-flash:0731" in saved["models"]
        assert "mistral-large-3:675b" in saved["models"]

    def test_successful_probe_still_replaces_cache(self, tmp_path, monkeypatch):
        """A successful live probe remains authoritative: dropped models stay dropped."""
        import json
        import time as _time

        from hermes_cli.models import fetch_ollama_cloud_models, _ollama_cloud_cache_path

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("OLLAMA_API_KEY", "test-key")

        cache_path = _ollama_cloud_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "models": ["retired-model"],
            "cached_at": _time.time() - 7200,
        }))

        with patch("hermes_cli.models.fetch_api_models", return_value=["qwen3.5:397b"]), \
             patch("agent.models_dev.fetch_models_dev", return_value={}):
            result = fetch_ollama_cloud_models(force_refresh=True)

        assert result == ["qwen3.5:397b"]
        assert "retired-model" not in result

        saved = json.loads(cache_path.read_text())
        assert saved["models"] == ["qwen3.5:397b"]
