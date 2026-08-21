#!/usr/bin/env python3
"""Tests for the OpenRouter Image API provider (dedicated /api/v1/images endpoint)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_RUNTIME = "hermes_cli.runtime_provider.resolve_runtime_provider"
_PNG_B64 = "iVBORw0KGgo="  # minimal valid PNG base64


def _runtime_ok(**over):
    base = {
        "provider": "openrouter",
        "api_mode": "chat_completions",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-or-test",
        "source": "env",
    }
    base.update(over)
    return base


def _mock_image_response(b64=None, url=None, media_type="image/png"):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    data_entry = {"media_type": media_type}
    if b64:
        data_entry["b64_json"] = b64
    if url:
        data_entry["url"] = url
    resp.json.return_value = {"data": [data_entry], "usage": {"cost": 0.04}}
    return resp


def _provider():
    from plugins.image_gen.openrouter import OpenRouterImageAPIProvider

    return OpenRouterImageAPIProvider()


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class TestProviderClass:
    def test_name_and_display(self):
        p = _provider()
        assert p.name == "openrouter-image-api"
        assert p.display_name == "OpenRouter Image API"

    def test_capabilities(self):
        caps = _provider().capabilities()
        assert "image" in caps["modalities"]
        assert caps["max_reference_images"] == 3

    def test_is_available_with_key(self):
        with patch(_RUNTIME, return_value=_runtime_ok()):
            assert _provider().is_available() is True

    def test_is_available_without_key(self):
        with patch(_RUNTIME, return_value=_runtime_ok(api_key="")):
            assert _provider().is_available() is False

    def test_is_available_on_error(self):
        with patch(_RUNTIME, side_effect=RuntimeError("boom")):
            assert _provider().is_available() is False

    def test_default_model(self):
        from plugins.image_gen.openrouter import _IMAGE_API_DEFAULT_MODEL

        assert _provider().default_model() == _IMAGE_API_DEFAULT_MODEL
        assert "grok-imagine" in _IMAGE_API_DEFAULT_MODEL

    def test_list_models(self):
        models = _provider().list_models()
        assert len(models) >= 1
        assert "x-ai/grok-imagine" in models[0]["id"]

    def test_setup_schema(self):
        schema = _provider().get_setup_schema()
        assert "OPENROUTER_API_KEY" in str(schema)


# ---------------------------------------------------------------------------
# generate()
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_missing_credentials(self):
        with patch(_RUNTIME, return_value=_runtime_ok(api_key="")):
            result = _provider().generate(prompt="a cat")
        assert result["success"] is False
        assert result["error_type"] == "missing_api_key"

    def test_success_b64_json(self):
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)), \
             patch(
                 "plugins.image_gen.openrouter.save_b64_image",
                 return_value=Path("/tmp/or_image_api.png"),
             ) as mock_save:
            result = _provider().generate(prompt="a cat")

        assert result["success"] is True
        assert result["image"] == "/tmp/or_image_api.png"
        assert result["provider"] == "openrouter-image-api"
        assert result["model"] == "x-ai/grok-imagine-image-2.0"
        mock_save.assert_called_once()

    def test_success_url(self):
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(url="https://cdn/x.png")), \
             patch(
                 "plugins.image_gen.openrouter.save_url_image",
                 return_value=Path("/tmp/or_image_api_url.png"),
             ) as mock_save_url:
            result = _provider().generate(prompt="a cat")

        assert result["success"] is True
        assert result["image"] == "/tmp/or_image_api_url.png"
        mock_save_url.assert_called_once()

    def test_payload_shape(self):
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(prompt="a cat", aspect_ratio="square")

        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "x-ai/grok-imagine-image-2.0"
        assert payload["prompt"] == "a cat"
        assert payload["aspect_ratio"] == "1:1"
        assert payload["n"] == 1
        assert "resolution" in payload

    def test_payload_landscape(self):
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(prompt="a cat", aspect_ratio="landscape")

        payload = mock_post.call_args.kwargs["json"]
        assert payload["aspect_ratio"] == "16:9"

    def test_payload_portrait(self):
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(prompt="a cat", aspect_ratio="portrait")

        payload = mock_post.call_args.kwargs["json"]
        assert payload["aspect_ratio"] == "9:16"

    def test_empty_response(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"data": []}

        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=resp):
            result = _provider().generate(prompt="a cat")
        assert result["success"] is False
        assert result["error_type"] == "empty_response"

    def test_post_to_correct_endpoint(self):
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(prompt="a cat")

        url = mock_post.call_args[0][0]
        assert url == "https://openrouter.ai/api/v1/images"

    def test_auth_header(self):
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(prompt="a cat")

        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-or-test"
        assert "HTTP-Referer" in headers
        assert "X-Title" in headers

    def test_api_error(self):
        import requests as req_lib

        resp = MagicMock()
        resp.status_code = 402
        resp.text = "Insufficient credits"
        resp.json.return_value = {"error": {"message": "Insufficient credits"}}
        resp.raise_for_status.side_effect = req_lib.HTTPError(response=resp)

        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=resp):
            result = _provider().generate(prompt="a cat")
        assert result["success"] is False
        assert result["error_type"] == "api_error"

    def test_timeout(self):
        import requests as req_lib

        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", side_effect=req_lib.Timeout()):
            result = _provider().generate(prompt="a cat")
        assert result["success"] is False
        assert result["error_type"] == "timeout"

    def test_connection_error(self):
        import requests as req_lib

        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", side_effect=req_lib.ConnectionError("reset")):
            result = _provider().generate(prompt="a cat")
        assert result["success"] is False
        assert result["error_type"] == "connection_error"

    def test_resolution_env_override(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_IMAGES_API_RESOLUTION", "2k")
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(prompt="a cat")
        payload = mock_post.call_args.kwargs["json"]
        assert payload["resolution"] == "2K"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_included_in_build_providers(self):
        from plugins.image_gen.openrouter import _build_providers

        names = {p.name for p in _build_providers()}
        assert "openrouter-image-api" in names

    def test_image_api_is_not_the_chat_compat_provider(self):
        from plugins.image_gen.openrouter import (
            OpenRouterCompatImageProvider,
            OpenRouterImageAPIProvider,
        )

        assert OpenRouterImageAPIProvider is not OpenRouterCompatImageProvider