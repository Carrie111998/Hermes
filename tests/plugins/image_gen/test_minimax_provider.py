#!/usr/bin/env python3
"""Tests for MiniMax image generation provider."""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fake_api_key(monkeypatch):
    """Ensure MINIMAX_API_KEY is set for all tests."""
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key-12345")


# ---------------------------------------------------------------------------
# Provider class tests
# ---------------------------------------------------------------------------


class TestMiniMaxImageGenProvider:
    def test_name(self):
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        provider = MiniMaxImageGenProvider()
        assert provider.name == "minimax"

    def test_display_name(self):
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        provider = MiniMaxImageGenProvider()
        assert provider.display_name == "MiniMax"

    def test_is_available_with_key(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "sk-xxx")
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        provider = MiniMaxImageGenProvider()
        assert provider.is_available() is True

    def test_is_available_without_key(self, monkeypatch):
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        provider = MiniMaxImageGenProvider()
        assert provider.is_available() is False

    def test_list_models(self):
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        provider = MiniMaxImageGenProvider()
        models = provider.list_models()
        assert len(models) >= 1
        assert models[0]["id"] == "image-01"

    def test_default_model(self):
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        provider = MiniMaxImageGenProvider()
        assert provider.default_model() == "image-01"

    def test_get_setup_schema(self):
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        provider = MiniMaxImageGenProvider()
        schema = provider.get_setup_schema()
        assert schema["name"] == "MiniMax (image-01)"
        assert schema["badge"] == "paid"
        # MiniMax auth is API-key only — the picker should expose the env var.
        assert schema["env_vars"] == [
            {
                "key": "MINIMAX_API_KEY",
                "prompt": "MiniMax API key",
                "url": "https://platform.minimax.io/user-center/basic-information/interface-key",
            }
        ]


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestConfig:
    def test_default_model(self):
        from plugins.image_gen.minimax import _resolve_model

        model_id, meta = _resolve_model()
        assert model_id == "image-01"
        assert meta["display"] == "MiniMax image-01"

    def test_default_base_url(self, monkeypatch):
        from plugins.image_gen.minimax import _resolve_base_url

        monkeypatch.delenv("MINIMAX_IMAGE_BASE_URL", raising=False)
        assert _resolve_base_url() == "https://api.minimax.io/v1"

    def test_env_override_base_url(self, monkeypatch):
        from plugins.image_gen.minimax import _resolve_base_url

        monkeypatch.setenv("MINIMAX_IMAGE_BASE_URL", "https://api.minimaxi.com/v1")
        assert _resolve_base_url() == "https://api.minimaxi.com/v1"

    def test_prompt_optimizer_default(self):
        from plugins.image_gen.minimax import _resolve_prompt_optimizer

        assert _resolve_prompt_optimizer({}) is False

    def test_prompt_optimizer_kwarg_overrides(self):
        from plugins.image_gen.minimax import _resolve_prompt_optimizer

        assert _resolve_prompt_optimizer({"prompt_optimizer": True}) is True
        assert _resolve_prompt_optimizer({"prompt_optimizer": False}) is False

    def test_seed_resolution(self):
        from plugins.image_gen.minimax import _resolve_seed

        assert _resolve_seed({}) is None
        assert _resolve_seed({"seed": 42}) == 42


# ---------------------------------------------------------------------------
# Generate tests
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_empty_prompt(self):
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        provider = MiniMaxImageGenProvider()
        result = provider.generate(prompt="")
        assert result["success"] is False
        assert result["error_type"] == "invalid_argument"

    def test_oversized_prompt(self):
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        provider = MiniMaxImageGenProvider()
        result = provider.generate(prompt="x" * 2000)
        assert result["success"] is False
        assert result["error_type"] == "invalid_argument"
        assert "1500" in result["error"]

    def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        provider = MiniMaxImageGenProvider()
        result = provider.generate(prompt="test")
        assert result["success"] is False
        assert "MINIMAX_API_KEY" in result["error"]
        assert result["error_type"] == "auth_required"

    def test_successful_base64_generation(self):
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "id": "trace-abc",
            "data": {"image_urls": ["https://example.com/img.png"], "image_base64": "dGVzdC1pbWFnZS1kYXRh"},
            "metadata": {"success_count": "1", "failed_count": "0"},
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }

        with patch("plugins.image_gen.minimax.requests.post", return_value=mock_resp), \
             patch("plugins.image_gen.minimax.save_b64_image", return_value="/tmp/test.png"):
            provider = MiniMaxImageGenProvider()
            result = provider.generate(prompt="A cat playing piano")

        assert result["success"] is True
        # Base64 path wins over URL even when both are returned (defensive).
        assert result["image"] == "/tmp/test.png"
        assert result["provider"] == "minimax"
        assert result["model"] == "image-01"
        # `extra` keys are merged into the top-level payload via setdefault(),
        # so they're accessible as direct keys rather than under an "extra" key.
        assert result["request_id"] == "trace-abc"
        assert result["aspect_ratio_native"] == "16:9"
        assert result["prompt_optimizer"] is False

    def test_successful_url_response_is_cached(self):
        """URL fallback must be cached locally — MiniMax URLs expire in 24h.

        Mirrors the xAI bug fix (#26942): the gateway needs an absolute
        filesystem path so its downstream ``send_photo`` doesn't 404 on
        an already-expired URL.
        """
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": {"image_urls": ["https://example.com/img.png"]},
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }

        with patch("plugins.image_gen.minimax.requests.post", return_value=mock_resp), \
             patch("plugins.image_gen.minimax.save_url_image", return_value="/tmp/minimax_cached.png") as mock_save:
            provider = MiniMaxImageGenProvider()
            result = provider.generate(prompt="A cat playing piano")

        assert result["success"] is True
        assert result["image"] == "/tmp/minimax_cached.png"
        mock_save.assert_called_once()
        # URL must be the first positional arg, prefix should be minimax_-prefixed.
        call_args, _ = mock_save.call_args
        assert call_args[0] == "https://example.com/img.png"
        assert mock_save.call_args.kwargs.get("prefix", "").startswith("minimax_")

    def test_base_resp_failure_surfaces_msg(self):
        """MiniMax wraps errors in base_resp.status_code != 0 — surface precisely."""
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": {},
            "base_resp": {"status_code": 1008, "status_msg": "invalid params"},
        }

        with patch("plugins.image_gen.minimax.requests.post", return_value=mock_resp):
            provider = MiniMaxImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "api_error"
        assert "invalid params" in result["error"]

    def test_http_error(self):
        import requests as req_lib
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        mock_resp.json.return_value = {"base_resp": {"status_msg": "Invalid API key"}}
        mock_resp.raise_for_status.side_effect = req_lib.HTTPError(response=mock_resp)

        with patch("plugins.image_gen.minimax.requests.post", return_value=mock_resp):
            provider = MiniMaxImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "api_error"
        assert "401" in result["error"]
        assert "Invalid API key" in result["error"]

    def test_http_error_with_non_json_body(self):
        import requests as req_lib
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "internal error"
        mock_resp.json.side_effect = ValueError("not json")
        mock_resp.raise_for_status.side_effect = req_lib.HTTPError(response=mock_resp)

        with patch("plugins.image_gen.minimax.requests.post", return_value=mock_resp):
            provider = MiniMaxImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "api_error"
        # Falls back to raw text when JSON parsing fails.
        assert "internal error" in result["error"]

    def test_timeout(self):
        import requests as req_lib
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        with patch("plugins.image_gen.minimax.requests.post", side_effect=req_lib.Timeout()):
            provider = MiniMaxImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "timeout"

    def test_connection_error(self):
        import requests as req_lib
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        with patch("plugins.image_gen.minimax.requests.post", side_effect=req_lib.ConnectionError("nope")):
            provider = MiniMaxImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "connection_error"

    def test_invalid_json_response(self):
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.side_effect = ValueError("not json")
        mock_resp.text = "<html>error</html>"

        with patch("plugins.image_gen.minimax.requests.post", return_value=mock_resp):
            provider = MiniMaxImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "invalid_response"

    def test_empty_data(self):
        """No image_urls AND no image_base64 → empty_response."""
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": {"image_urls": []},
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }

        with patch("plugins.image_gen.minimax.requests.post", return_value=mock_resp):
            provider = MiniMaxImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "empty_response"

    def test_auth_header_and_url(self):
        """Verify bearer auth header and the /image_generation URL path."""
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": {"image_base64": "dGVzdA=="},
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }

        with patch("plugins.image_gen.minimax.requests.post", return_value=mock_resp) as mock_post, \
             patch("plugins.image_gen.minimax.save_b64_image", return_value="/tmp/test.png"):
            provider = MiniMaxImageGenProvider()
            provider.generate(prompt="test")

        # URL must hit the image_generation endpoint on the default base.
        url = mock_post.call_args.args[0]
        assert url == "https://api.minimax.io/v1/image_generation"

        # Bearer auth header must carry the configured key.
        headers = mock_post.call_args.kwargs.get("headers") or {}
        assert headers["Authorization"] == "Bearer test-key-12345"
        assert headers["Content-Type"] == "application/json"

        # Payload must request base64 + n=1.
        payload = mock_post.call_args.kwargs.get("json") or {}
        assert payload["response_format"] == "base64"
        assert payload["n"] == 1
        assert payload["model"] == "image-01"
        assert payload["aspect_ratio"] == "16:9"  # landscape
        assert payload["prompt_optimizer"] is False

    def test_aspect_ratio_mapping(self):
        """All three canonical ratios must map to MiniMax's API values."""
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": {"image_base64": "dGVzdA=="},
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }

        for hermes_ar, expected_native in [
            ("landscape", "16:9"),
            ("square", "1:1"),
            ("portrait", "9:16"),
        ]:
            with patch("plugins.image_gen.minimax.requests.post", return_value=mock_resp) as mock_post, \
                 patch("plugins.image_gen.minimax.save_b64_image", return_value="/tmp/test.png"):
                provider = MiniMaxImageGenProvider()
                provider.generate(prompt="test", aspect_ratio=hermes_ar)
            payload = mock_post.call_args.kwargs.get("json") or {}
            assert payload["aspect_ratio"] == expected_native, (
                f"aspect_ratio={hermes_ar} should map to {expected_native}, got {payload['aspect_ratio']!r}"
            )

    def test_seed_passthrough(self):
        from plugins.image_gen.minimax import MiniMaxImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": {"image_base64": "dGVzdA=="},
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }

        with patch("plugins.image_gen.minimax.requests.post", return_value=mock_resp) as mock_post, \
             patch("plugins.image_gen.minimax.save_b64_image", return_value="/tmp/test.png"):
            provider = MiniMaxImageGenProvider()
            provider.generate(prompt="test", seed=12345)

        payload = mock_post.call_args.kwargs.get("json") or {}
        assert payload["seed"] == 12345


# ---------------------------------------------------------------------------
# Registration test
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register(self):
        from plugins.image_gen.minimax import MiniMaxImageGenProvider, register

        mock_ctx = MagicMock()
        register(mock_ctx)
        mock_ctx.register_image_gen_provider.assert_called_once()
        provider = mock_ctx.register_image_gen_provider.call_args[0][0]
        assert isinstance(provider, MiniMaxImageGenProvider)
        assert provider.name == "minimax"
