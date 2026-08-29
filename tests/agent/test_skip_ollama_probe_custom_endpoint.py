"""Ollama /api/show must not run against OpenAI-compat custom gateways.

Context-length resolution used to POST /api/show (and, on loopback, run the
local server-type waterfall) whenever GET /v1/models omitted context_length.
That is correct for Ollama. It is a synchronous stall for a remote or
named-custom OpenAI-compat relay — including a loopback reverse proxy in
front of one — and it can run on the messaging gateway's asyncio thread.
"""

from unittest.mock import MagicMock, patch

import pytest

from agent.model_metadata import (
    DEFAULT_FALLBACK_CONTEXT,
    _looks_like_ollama_base_url,
    _should_probe_ollama_native,
    fetch_endpoint_model_metadata,
    get_model_context_length,
)


@pytest.mark.parametrize(
    "base_url,expected",
    [
        ("http://127.0.0.1:11434", True),
        ("http://127.0.0.1:11434/v1", True),
        ("http://localhost:11434/v1", True),
        ("https://ollama.com", True),
        ("https://api.ollama.com/v1", True),
        ("http://127.0.0.1:9000/v1", False),
        ("https://llm-gateway.example.com/v1", False),
        ("http://host:99999/v1", False),
        ("", False),
    ],
)
def test_looks_like_ollama_base_url(base_url, expected):
    assert _looks_like_ollama_base_url(base_url) is expected


@pytest.mark.parametrize(
    "base_url,provider,expected",
    [
        ("https://llm-gateway.example.com/v1", "custom:internal", False),
        ("https://llm-gateway.example.com/v1", "custom", False),
        ("https://llm-gateway.example.com/v1", "", False),
        ("http://127.0.0.1:9000/v1", "custom:internal", False),
        ("http://127.0.0.1:9000/v1", "custom", True),
        ("http://127.0.0.1:9000/v1", "", True),
        ("http://127.0.0.1:11434/v1", "custom:internal", True),
        ("http://127.0.0.1:11434", "ollama", True),
        ("https://ollama.com", "", True),
        ("https://api.openai.com/v1", "", False),
        ("https://openrouter.ai/api/v1", "custom:internal", False),
    ],
)
def test_should_probe_ollama_native(base_url, provider, expected):
    assert _should_probe_ollama_native(base_url, provider) is expected


class TestSkipOllamaProbeOnCustomOpenAICompat:
    def test_remote_named_custom_does_not_post_api_show(self):
        with (
            patch("agent.model_metadata.get_cached_context_length", return_value=None),
            patch("agent.model_metadata.fetch_model_metadata", return_value={}),
            patch(
                "agent.model_metadata._resolve_endpoint_context_length",
                return_value=None,
            ),
            patch("agent.model_metadata._query_ollama_api_show") as show,
            patch("agent.model_metadata._query_local_context_length") as local,
            patch("agent.model_metadata.save_context_length"),
        ):
            result = get_model_context_length(
                "totally-unknown-model",
                base_url="https://llm-gateway.example.com/v1",
                provider="custom:internal",
            )
        assert result == DEFAULT_FALLBACK_CONTEXT
        show.assert_not_called()
        local.assert_not_called()

    def test_remote_unlabeled_custom_url_does_not_post_api_show(self):
        with (
            patch("agent.model_metadata.get_cached_context_length", return_value=None),
            patch("agent.model_metadata.fetch_model_metadata", return_value={}),
            patch(
                "agent.model_metadata._resolve_endpoint_context_length",
                return_value=None,
            ),
            patch("agent.model_metadata._query_ollama_api_show") as show,
            patch("agent.model_metadata._query_local_context_length") as local,
            patch("agent.model_metadata.save_context_length"),
        ):
            result = get_model_context_length(
                "totally-unknown-model",
                base_url="https://llm-gateway.example.com/v1",
            )
        assert result == DEFAULT_FALLBACK_CONTEXT
        show.assert_not_called()
        local.assert_not_called()

    def test_named_custom_on_loopback_does_not_fingerprint_as_ollama(self):
        with (
            patch("agent.model_metadata.get_cached_context_length", return_value=None),
            patch("agent.model_metadata.fetch_model_metadata", return_value={}),
            patch(
                "agent.model_metadata._resolve_endpoint_context_length",
                return_value=None,
            ),
            patch("agent.model_metadata._query_ollama_api_show") as show,
            patch("agent.model_metadata._query_local_context_length") as local,
            patch("agent.model_metadata.save_context_length"),
        ):
            result = get_model_context_length(
                "totally-unknown-model",
                base_url="http://127.0.0.1:9000/v1",
                provider="custom:internal",
            )
        assert result == DEFAULT_FALLBACK_CONTEXT
        show.assert_not_called()
        local.assert_not_called()

    def test_local_ollama_port_still_probes_even_with_named_custom(self):
        with (
            patch("agent.model_metadata.get_cached_context_length", return_value=None),
            patch("agent.model_metadata.fetch_model_metadata", return_value={}),
            patch(
                "agent.model_metadata._resolve_endpoint_context_length",
                return_value=None,
            ),
            patch("agent.model_metadata._query_ollama_api_show", return_value=None) as show,
            patch(
                "agent.model_metadata._query_local_context_length",
                return_value=32768,
            ) as local,
            patch("agent.model_metadata.save_context_length"),
            patch("agent.model_metadata._maybe_cache_local_context_length"),
        ):
            result = get_model_context_length(
                "llama3",
                base_url="http://127.0.0.1:11434",
                provider="custom:internal",
            )
        assert result == 32768
        local.assert_called_once()
        show.assert_not_called()

    def test_bare_custom_on_loopback_still_probes_local_runtime(self):
        with (
            patch("agent.model_metadata.get_cached_context_length", return_value=None),
            patch("agent.model_metadata.fetch_model_metadata", return_value={}),
            patch(
                "agent.model_metadata._resolve_endpoint_context_length",
                return_value=None,
            ),
            patch("agent.model_metadata._query_ollama_api_show", return_value=None),
            patch(
                "agent.model_metadata._query_local_context_length",
                return_value=8192,
            ) as local,
            patch("agent.model_metadata.save_context_length"),
            patch("agent.model_metadata._maybe_cache_local_context_length"),
        ):
            result = get_model_context_length(
                "local-model",
                base_url="http://127.0.0.1:9000/v1",
                provider="custom",
            )
        assert result == 8192
        local.assert_called_once()

    def test_explicit_config_context_length_still_short_circuits(self):
        with (
            patch("agent.model_metadata._query_ollama_api_show") as show,
            patch("agent.model_metadata._query_local_context_length") as local,
            patch("agent.model_metadata._resolve_endpoint_context_length") as resolve,
        ):
            result = get_model_context_length(
                "any-model",
                base_url="https://llm-gateway.example.com/v1",
                provider="custom:internal",
                config_context_length=1_000_000,
            )
        assert result == 1_000_000
        show.assert_not_called()
        local.assert_not_called()
        resolve.assert_not_called()


class TestFetchEndpointSkipsLocalWaterfall:
    def setup_method(self):
        import agent.model_metadata as mm

        mm._endpoint_model_metadata_cache.clear()
        mm._endpoint_model_metadata_cache_time.clear()

    def test_named_custom_loopback_skips_server_type_waterfall(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "data": [{"id": "some-model", "object": "model"}]
        }

        with (
            patch("agent.model_metadata.detect_local_server_type") as detect,
            patch("agent.model_metadata.requests.get", return_value=response) as get,
        ):
            result = fetch_endpoint_model_metadata(
                "http://127.0.0.1:9000/v1",
                provider="custom:internal",
                force_refresh=True,
            )

        detect.assert_not_called()
        assert get.called
        assert "some-model" in result
        assert "context_length" not in result["some-model"]
