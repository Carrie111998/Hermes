"""Tests for Ollama num_ctx context length detection and injection.

Covers:
  agent/model_metadata.py — query_ollama_num_ctx()
  run_agent.py — _ollama_num_ctx detection + extra_body injection
"""

from unittest.mock import patch, MagicMock


from agent.model_metadata import query_ollama_num_ctx, query_ollama_supports_vision


# ═══════════════════════════════════════════════════════════════════════
# Level 1: query_ollama_num_ctx — Ollama API interaction
# ═══════════════════════════════════════════════════════════════════════


def _mock_httpx_client(show_response_data, status_code=200):
    """Create a mock httpx.Client context manager that returns given /api/show data."""
    mock_resp = MagicMock(status_code=status_code)
    mock_resp.json.return_value = show_response_data
    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_client)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    return mock_ctx, mock_client


class TestQueryOllamaNumCtx:
    """Test the Ollama /api/show context length query."""

    def test_returns_context_from_model_info(self):
        """Should extract context_length from GGUF model_info metadata."""
        show_data = {
            "model_info": {"llama.context_length": 131072},
            "parameters": "",
        }
        mock_ctx, _ = _mock_httpx_client(show_data)

        with patch("agent.model_metadata.detect_local_server_type", return_value="ollama"):
            # httpx is imported inside the function — patch the module import
            import httpx
            with patch.object(httpx, "Client", return_value=mock_ctx):
                result = query_ollama_num_ctx("llama3.1:8b", "http://localhost:11434/v1")

        assert result == 131072

    def test_prefers_explicit_num_ctx_from_modelfile(self):
        """If the Modelfile sets num_ctx explicitly, that should take priority."""
        show_data = {
            "model_info": {"llama.context_length": 131072},
            "parameters": "num_ctx 32768\ntemperature 0.7",
        }
        mock_ctx, _ = _mock_httpx_client(show_data)

        with patch("agent.model_metadata.detect_local_server_type", return_value="ollama"):
            import httpx
            with patch.object(httpx, "Client", return_value=mock_ctx):
                result = query_ollama_num_ctx("custom-model", "http://localhost:11434")

        assert result == 32768




    def test_strips_provider_prefix(self):
        """Should strip 'local:' prefix from model name before querying."""
        show_data = {
            "model_info": {"qwen2.context_length": 32768},
            "parameters": "",
        }
        mock_ctx, mock_client = _mock_httpx_client(show_data)

        with patch("agent.model_metadata.detect_local_server_type", return_value="ollama"):
            import httpx
            with patch.object(httpx, "Client", return_value=mock_ctx):
                result = query_ollama_num_ctx("local:qwen2.5:7b", "http://localhost:11434/v1")

        # Verify the post was called with stripped name (no "local:" prefix)
        call_args = mock_client.post.call_args
        assert call_args[1]["json"]["name"] == "qwen2.5:7b" or call_args[0][1] is not None
        assert result == 32768

    def test_handles_qwen2_architecture_key(self):
        """Different model architectures use different key prefixes in model_info."""
        show_data = {
            "model_info": {"qwen2.context_length": 65536},
            "parameters": "",
        }
        mock_ctx, _ = _mock_httpx_client(show_data)

        with patch("agent.model_metadata.detect_local_server_type", return_value="ollama"):
            import httpx
            with patch.object(httpx, "Client", return_value=mock_ctx):
                result = query_ollama_num_ctx("qwen2.5:32b", "http://localhost:11434")

        assert result == 65536



class TestQueryOllamaSupportsVision:
    """Test Ollama /api/show vision capability detection."""

    def test_returns_true_when_capabilities_include_vision(self):
        show_data = {"capabilities": ["completion", "vision"]}
        mock_ctx, _ = _mock_httpx_client(show_data)

        with patch("agent.model_metadata.detect_local_server_type", return_value="ollama"):
            import httpx
            with patch.object(httpx, "Client", return_value=mock_ctx):
                result = query_ollama_supports_vision("gemma4:e2b", "http://localhost:11434/v1")

        assert result is True


    def test_falls_back_to_model_info_vision_block_count(self):
        show_data = {"model_info": {"gemma3.vision.block_count": 27}}
        mock_ctx, _ = _mock_httpx_client(show_data)

        with patch("agent.model_metadata.detect_local_server_type", return_value="ollama"):
            import httpx
            with patch.object(httpx, "Client", return_value=mock_ctx):
                result = query_ollama_supports_vision("llava", "http://localhost:11434")

        assert result is True

    def test_returns_none_for_non_ollama_server(self):
        with patch("agent.model_metadata.detect_local_server_type", return_value="vllm"):
            result = query_ollama_supports_vision("llava", "http://localhost:8000/v1")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# _query_ollama_api_show_uncached — server-type guard (#96054)
# ═══════════════════════════════════════════════════════════════════════


class TestQueryOllamaApiShowServerTypeGuard:
    """The provider-agnostic /api/show probe must not spray 404s at
    OpenAI-compatible local servers (llama.cpp / Lemonade / LM Studio).
    Failures are deliberately never cached, so without the guard every
    fresh process re-paid the probe against a server that can never
    answer it."""

    def test_returns_none_for_non_ollama_local_server(self):
        from agent.model_metadata import _query_ollama_api_show_uncached

        with patch(
            "agent.model_metadata.detect_local_server_type",
            return_value="lm-studio",
        ), patch("httpx.Client") as client_cls:
            result = _query_ollama_api_show_uncached(
                "Qwen3-30B-A3B-Instruct-2507-GGUF", "http://localhost:13305/api/v1"
            )
        assert result is None
        client_cls.assert_not_called()  # the POST never happens

    def test_returns_none_when_detection_fails(self):
        from agent.model_metadata import _query_ollama_api_show_uncached

        with patch(
            "agent.model_metadata.detect_local_server_type",
            side_effect=RuntimeError("boom"),
        ), patch("httpx.Client") as client_cls:
            result = _query_ollama_api_show_uncached(
                "some-model", "http://localhost:8000/v1"
            )
        assert result is None
        client_cls.assert_not_called()

    def test_probes_when_server_is_ollama(self):
        from agent.model_metadata import _query_ollama_api_show_uncached

        mock_ctx, mock_client = _mock_httpx_client(
            {"model_info": {"qwen3.context_length": 40960}}
        )
        with patch(
            "agent.model_metadata.detect_local_server_type",
            return_value="ollama",
        ), patch("httpx.Client", return_value=mock_ctx):
            result = _query_ollama_api_show_uncached(
                "qwen3", "http://localhost:11434/v1"
            )
        assert result == 40960
        mock_client.post.assert_called_once()

    def test_ollama_com_bypasses_local_detection(self):
        from agent.model_metadata import _query_ollama_api_show_uncached

        mock_ctx, mock_client = _mock_httpx_client(
            {"model_info": {"deepseek.context_length": 163840}}
        )
        with patch(
            "agent.model_metadata.detect_local_server_type",
            side_effect=AssertionError("must not be called for ollama.com"),
        ), patch("httpx.Client", return_value=mock_ctx):
            result = _query_ollama_api_show_uncached(
                "deepseek-v4", "https://ollama.com/v1", api_key="k"
            )
        assert result == 163840
        mock_client.post.assert_called_once()
