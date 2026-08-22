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

    def test_resolution_from_config(self):
        """Resolution is picked up from provider-scoped config when env is unset."""
        import plugins.image_gen.openrouter as mod

        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch.object(mod, "_load_image_gen_config", return_value={
                 "openrouter-image-api": {"resolution": "2k"},
             }), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(prompt="a cat")
        payload = mock_post.call_args.kwargs["json"]
        assert payload["resolution"] == "2K"

    def test_resolution_defaults_to_1k(self):
        """Resolution defaults to 1K when neither env nor config is set."""
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(prompt="a cat")
        payload = mock_post.call_args.kwargs["json"]
        assert payload["resolution"] == "1K"

    def test_quality_medium_in_payload(self):
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(prompt="a cat", quality="medium")
        payload = mock_post.call_args.kwargs["json"]
        assert payload["quality"] == "medium"

    def test_quality_invalid_not_in_payload(self):
        """Unsupported quality (e.g. 'high') is silently ignored — no quality key in payload."""
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(prompt="a cat", quality="high")
        payload = mock_post.call_args.kwargs["json"]
        assert "quality" not in payload

    def test_quality_low_in_payload(self):
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(prompt="a cat", quality="low")
        payload = mock_post.call_args.kwargs["json"]
        assert payload["quality"] == "low"

    def test_unknown_aspect_ratio_falls_back_to_default(self):
        """Unrecognized aspect ratios are normalized to 'landscape' by resolve_aspect_ratio, not silently squared."""
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(prompt="a cat", aspect_ratio="ultrawide")
        payload = mock_post.call_args.kwargs["json"]
        assert payload["aspect_ratio"] == "16:9"

    def test_aspect_ratio_4_3_direct(self):
        """Raw '4:3' bypasses resolve_aspect_ratio and goes directly to the API."""
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(prompt="a cat", aspect_ratio="4:3")
        payload = mock_post.call_args.kwargs["json"]
        assert payload["aspect_ratio"] == "4:3"

    def test_aspect_ratio_error_on_unexpected_resolve(self, monkeypatch):
        """When resolve_aspect_ratio returns a value not in _IMAGE_API_ASPECT_RATIOS, an error is returned."""
        import plugins.image_gen.openrouter as mod

        monkeypatch.setattr(mod, "_IMAGE_API_ASPECT_RATIOS", {"portrait": "9:16"})
        monkeypatch.setattr(mod, "resolve_aspect_ratio", lambda _: "landscape")
        with patch(_RUNTIME, return_value=_runtime_ok()):
            result = _provider().generate(prompt="a cat")
        assert result["success"] is False
        assert result["error_type"] == "invalid_aspect_ratio"

    def test_input_references_from_image_url(self):
        """image_url kwarg produces input_references in the payload."""
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(prompt="a cat", image_url="https://example.com/ref.png")

        payload = mock_post.call_args.kwargs["json"]
        assert "input_references" in payload
        assert payload["input_references"] == [
            {"type": "image_url", "image_url": {"url": "https://example.com/ref.png"}}
        ]

    def test_input_references_from_reference_image_urls(self):
        """reference_image_urls list adds extra input_references."""
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(
                prompt="a cat",
                reference_image_urls=["https://example.com/r1.png", "https://example.com/r2.png"]
            )

        payload = mock_post.call_args.kwargs["json"]
        assert "input_references" in payload
        assert len(payload["input_references"]) == 2

    def test_input_references_clamped_to_three(self):
        """At most 3 input_references are sent, extra refs are discarded."""
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(
                prompt="a cat",
                reference_image_urls=[f"https://example.com/{i}.png" for i in range(5)],
            )

        payload = mock_post.call_args.kwargs["json"]
        assert len(payload["input_references"]) == 3

    def test_input_references_local_file(self, tmp_path):
        """Local file paths are read, base64-encoded, and included as data URIs."""
        img = tmp_path / "ref.png"
        img.write_bytes(b"fake_png_bytes")

        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(b64=_PNG_B64)) as mock_post, \
             patch("plugins.image_gen.openrouter.save_b64_image", return_value=Path("/tmp/x.png")):
            _provider().generate(prompt="a cat", image_url=str(img))

        payload = mock_post.call_args.kwargs["json"]
        assert "input_references" in payload
        ref = payload["input_references"][0]
        assert ref["type"] == "image_url"
        assert ref["image_url"]["url"].startswith("data:")
        assert "base64" in ref["image_url"]["url"]

    def test_input_references_raise_if_read_blocked(self):
        """Blocked local files raise an io_error."""
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("agent.file_safety.raise_if_read_blocked",
                   side_effect=PermissionError("no read access")):
            result = _provider().generate(prompt="a cat", image_url="/etc/shadow")

        assert result["success"] is False
        assert result["error_type"] == "io_error"
        assert "no read access" in result.get("error", "")

    def test_model_resolver_stops_at_scoped_config(self):
        """_resolve_image_api_model does NOT fall through to the top-level image_gen.model."""
        import plugins.image_gen.openrouter as mod

        with patch.object(mod, "_load_image_gen_config", return_value={
            "openrouter-image-api": {"model": "x-ai/grok-v2"},
            "model": "google/gemini-3-pro-image",  # top-level; should NOT be picked up
        }):
            result = _provider()._resolve_image_api_model()

        assert "gemini" not in result
        assert result == "x-ai/grok-v2"

    def test_model_resolver_defaults_when_unconfigured(self):
        """_resolve_image_api_model returns the default model when nothing is set."""
        import plugins.image_gen.openrouter as mod

        with patch.object(mod, "_load_image_gen_config", return_value={}):
            result = _provider()._resolve_image_api_model()

        assert result == mod._IMAGE_API_DEFAULT_MODEL

    def test_save_url_cached_ok(self):
        """URL image is saved via save_url_image and path returned."""
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(url="https://cdn/x.png")), \
             patch(
                 "plugins.image_gen.openrouter.save_url_image",
                 return_value=Path("/tmp/or_cached.png"),
             ) as mock_save_url:
            result = _provider().generate(prompt="a cat")

        assert result["success"] is True
        assert result["image"] == "/tmp/or_cached.png"
        mock_save_url.assert_called_once()

    def test_save_url_failure_returns_error(self):
        """When save_url_image fails, an io_error is returned (not a raw CDN URL)."""
        with patch(_RUNTIME, return_value=_runtime_ok()), \
             patch("requests.post", return_value=_mock_image_response(url="https://cdn/x.png")), \
             patch(
                 "plugins.image_gen.openrouter.save_url_image",
                 side_effect=OSError("disk full"),
             ):
            result = _provider().generate(prompt="a cat")

        assert result["success"] is False
        assert result["error_type"] == "io_error"
        assert "disk full" in result.get("error", "")


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