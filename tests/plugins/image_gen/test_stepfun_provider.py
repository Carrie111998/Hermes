#!/usr/bin/env python3
"""Tests for StepFun image generation provider."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fake_api_key(monkeypatch):
    """Ensure STEPFUN_API_KEY is set for all tests."""
    monkeypatch.setenv("STEPFUN_API_KEY", "test-key-12345")


# ---------------------------------------------------------------------------
# Provider class tests
# ---------------------------------------------------------------------------


class TestStepFunImageGenProvider:
    def test_name(self):
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        provider = StepFunImageGenProvider()
        assert provider.name == "stepfun"

    def test_display_name(self):
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        provider = StepFunImageGenProvider()
        assert provider.display_name == "StepFun"

    def test_is_available_with_key(self, monkeypatch):
        monkeypatch.setenv("STEPFUN_API_KEY", "sk-xxx")
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        provider = StepFunImageGenProvider()
        assert provider.is_available() is True

    def test_is_available_without_key(self, monkeypatch):
        monkeypatch.delenv("STEPFUN_API_KEY", raising=False)
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        provider = StepFunImageGenProvider()
        assert provider.is_available() is False

    def test_list_models(self):
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        provider = StepFunImageGenProvider()
        models = provider.list_models()
        assert len(models) >= 1
        assert models[0]["id"] == "step-image-edit-2"

    def test_default_model(self):
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        provider = StepFunImageGenProvider()
        assert provider.default_model() == "step-image-edit-2"

    def test_get_setup_schema(self):
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        provider = StepFunImageGenProvider()
        schema = provider.get_setup_schema()
        assert schema["name"] == "StepFun (step-image-edit-2)"
        assert schema["badge"] == "paid"
        assert schema["env_vars"] == [
            {
                "key": "STEPFUN_API_KEY",
                "prompt": "StepFun API key",
                "url": "https://platform.stepfun.ai/",
            }
        ]


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestConfig:
    def test_default_model(self):
        from plugins.image_gen.stepfun import _resolve_model

        model_id, meta = _resolve_model()
        assert model_id == "step-image-edit-2"

    def test_default_base_url(self, monkeypatch):
        from plugins.image_gen.stepfun import _resolve_base_url

        monkeypatch.delenv("STEPFUN_IMAGE_BASE_URL", raising=False)
        assert _resolve_base_url() == "https://api.stepfun.ai/v1"

    def test_env_override_base_url(self, monkeypatch):
        from plugins.image_gen.stepfun import _resolve_base_url

        monkeypatch.setenv("STEPFUN_IMAGE_BASE_URL", "https://api.stepfun.com/v1")
        assert _resolve_base_url() == "https://api.stepfun.com/v1"

    def test_int_kwarg_clamps_and_defaults(self):
        from plugins.image_gen.stepfun import _resolve_int_kwarg

        # No value → default.
        assert _resolve_int_kwarg("steps", {}, minimum=1, maximum=50, default=8) == 8
        # In-range value passes through.
        assert _resolve_int_kwarg("steps", {"steps": 25}, minimum=1, maximum=50, default=8) == 25
        # Out-of-range gets clamped.
        assert _resolve_int_kwarg("steps", {"steps": 999}, minimum=1, maximum=50, default=8) == 50
        assert _resolve_int_kwarg("steps", {"steps": 0}, minimum=1, maximum=50, default=8) == 1
        # Wrong-type value (string) falls back to default.
        assert _resolve_int_kwarg("steps", {"steps": "twenty"}, minimum=1, maximum=50, default=8) == 8

    def test_float_kwarg_clamps_and_defaults(self):
        from plugins.image_gen.stepfun import _resolve_float_kwarg

        assert _resolve_float_kwarg("cfg_scale", {}, minimum=1.0, maximum=10.0, default=1.0) == 1.0
        assert _resolve_float_kwarg("cfg_scale", {"cfg_scale": 5.5}, minimum=1.0, maximum=10.0, default=1.0) == 5.5
        assert _resolve_float_kwarg("cfg_scale", {"cfg_scale": 99.0}, minimum=1.0, maximum=10.0, default=1.0) == 10.0
        assert _resolve_float_kwarg("cfg_scale", {"cfg_scale": 0.5}, minimum=1.0, maximum=10.0, default=1.0) == 1.0

    def test_str_kwarg_truncates(self):
        from plugins.image_gen.stepfun import _resolve_str_kwarg

        assert _resolve_str_kwarg("negative_prompt", {}, max_length=512) is None
        long = "x" * 1000
        out = _resolve_str_kwarg("negative_prompt", {"negative_prompt": long}, max_length=512)
        assert out is not None and len(out) == 512

    def test_bool_kwarg(self):
        from plugins.image_gen.stepfun import _resolve_bool_kwarg

        assert _resolve_bool_kwarg("text_mode", {}, default=False) is False
        assert _resolve_bool_kwarg("text_mode", {"text_mode": True}, default=False) is True
        # Wrong-type value (string) → default.
        assert _resolve_bool_kwarg("text_mode", {"text_mode": "yes"}, default=False) is False


# ---------------------------------------------------------------------------
# Generate tests
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_empty_prompt(self):
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        provider = StepFunImageGenProvider()
        result = provider.generate(prompt="")
        assert result["success"] is False
        assert result["error_type"] == "invalid_argument"

    def test_oversized_prompt(self):
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        provider = StepFunImageGenProvider()
        result = provider.generate(prompt="x" * 600)
        assert result["success"] is False
        assert result["error_type"] == "invalid_argument"
        assert "512" in result["error"]

    def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("STEPFUN_API_KEY", raising=False)
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        provider = StepFunImageGenProvider()
        result = provider.generate(prompt="test")
        assert result["success"] is False
        assert "STEPFUN_API_KEY" in result["error"]
        assert result["error_type"] == "auth_required"

    def test_successful_b64_generation(self):
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "created": 1700000000,
            "data": [
                {
                    "b64_json": "dGVzdC1pbWFnZS1kYXRh",
                    "finish_reason": "success",
                    "seed": 12345,
                }
            ],
        }

        with patch("plugins.image_gen.stepfun.requests.post", return_value=mock_resp), \
             patch("plugins.image_gen.stepfun.save_b64_image", return_value="/tmp/test.png"):
            provider = StepFunImageGenProvider()
            result = provider.generate(prompt="A serene alpine lake")

        assert result["success"] is True
        assert result["image"] == "/tmp/test.png"
        assert result["provider"] == "stepfun"
        assert result["model"] == "step-image-edit-2"
        # `extra` keys are merged into the top-level payload via setdefault(),
        # so they're accessible as direct keys rather than under an "extra" key.
        assert result["seed"] == 12345
        assert result["finish_reason"] == "success"
        assert result["size"] == "1360x768"  # landscape

    def test_content_filtered_response(self):
        """finish_reason=content_filtered with no bytes → clean error."""
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [{"finish_reason": "content_filtered", "seed": 99}],
        }

        with patch("plugins.image_gen.stepfun.requests.post", return_value=mock_resp):
            provider = StepFunImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "content_filtered"
        assert "content_filtered" in result["error"]

    def test_successful_url_response_is_cached(self):
        """URL fallback must be cached — StepFun URLs expire in 2h.

        Mirrors the xAI/MiniMax URL-caching contract so the gateway
        doesn't try to fetch an expired URL at ``send_photo`` time.
        """
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [{"url": "https://stepfun.example.com/tmp-img.jpeg", "finish_reason": "success"}],
        }

        with patch("plugins.image_gen.stepfun.requests.post", return_value=mock_resp), \
             patch("plugins.image_gen.stepfun.save_url_image",
                   return_value=Path("/tmp/stepfun_cached.jpeg")) as mock_save:
            provider = StepFunImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is True
        assert result["image"] == "/tmp/stepfun_cached.jpeg"
        mock_save.assert_called_once()
        call_args, _ = mock_save.call_args
        assert call_args[0] == "https://stepfun.example.com/tmp-img.jpeg"
        assert mock_save.call_args.kwargs.get("prefix", "").startswith("stepfun_")

    def test_url_response_falls_back_when_cache_fails(self):
        """If save_url_image raises, fall back to bare URL (not a hard error)."""
        import requests as req_lib
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [{"url": "https://stepfun.example.com/already-404.jpeg"}],
        }

        with patch("plugins.image_gen.stepfun.requests.post", return_value=mock_resp), \
             patch("plugins.image_gen.stepfun.save_url_image", side_effect=req_lib.HTTPError("404")):
            provider = StepFunImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is True
        assert result["image"] == "https://stepfun.example.com/already-404.jpeg"

    def test_http_error(self):
        import requests as req_lib
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        mock_resp.json.return_value = {"error": {"message": "Invalid API key"}}
        mock_resp.raise_for_status.side_effect = req_lib.HTTPError(response=mock_resp)

        with patch("plugins.image_gen.stepfun.requests.post", return_value=mock_resp):
            provider = StepFunImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "api_error"
        assert "401" in result["error"]
        assert "Invalid API key" in result["error"]

    def test_http_error_non_json(self):
        import requests as req_lib
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "internal error"
        mock_resp.json.side_effect = ValueError("not json")
        mock_resp.raise_for_status.side_effect = req_lib.HTTPError(response=mock_resp)

        with patch("plugins.image_gen.stepfun.requests.post", return_value=mock_resp):
            provider = StepFunImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "api_error"
        assert "internal error" in result["error"]

    def test_timeout(self):
        import requests as req_lib
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        with patch("plugins.image_gen.stepfun.requests.post", side_effect=req_lib.Timeout()):
            provider = StepFunImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "timeout"

    def test_connection_error(self):
        import requests as req_lib
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        with patch("plugins.image_gen.stepfun.requests.post", side_effect=req_lib.ConnectionError("nope")):
            provider = StepFunImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "connection_error"

    def test_invalid_json(self):
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.side_effect = ValueError("not json")
        mock_resp.text = "<html>error</html>"

        with patch("plugins.image_gen.stepfun.requests.post", return_value=mock_resp):
            provider = StepFunImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "invalid_response"

    def test_empty_data(self):
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"data": []}

        with patch("plugins.image_gen.stepfun.requests.post", return_value=mock_resp):
            provider = StepFunImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "empty_response"

    def test_response_with_neither_b64_nor_url(self):
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"data": [{"finish_reason": "success", "seed": 1}]}

        with patch("plugins.image_gen.stepfun.requests.post", return_value=mock_resp):
            provider = StepFunImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "empty_response"
        assert "neither b64_json nor URL" in result["error"]

    def test_auth_header_and_url(self):
        """Verify bearer auth + /v1/images/generations URL path."""
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [{"b64_json": "dGVzdA==", "finish_reason": "success"}],
        }

        with patch("plugins.image_gen.stepfun.requests.post", return_value=mock_resp) as mock_post, \
             patch("plugins.image_gen.stepfun.save_b64_image", return_value="/tmp/test.png"):
            provider = StepFunImageGenProvider()
            provider.generate(prompt="test")

        url = mock_post.call_args.args[0]
        assert url == "https://api.stepfun.ai/v1/images/generations"

        headers = mock_post.call_args.kwargs.get("headers") or {}
        assert headers["Authorization"] == "Bearer test-key-12345"
        assert headers["Content-Type"] == "application/json"

        payload = mock_post.call_args.kwargs.get("json") or {}
        assert payload["response_format"] == "b64_json"
        assert payload["n"] == 1
        assert payload["model"] == "step-image-edit-2"
        # Defaults (steps=8, cfg=1.0) should NOT be on the wire.
        assert "steps" not in payload
        assert "cfg_scale" not in payload
        assert "negative_prompt" not in payload
        assert "text_mode" not in payload

    def test_aspect_ratio_size_mapping(self):
        """StepFun uses ``{height}x{width}`` ordering — verify each ratio."""
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [{"b64_json": "dGVzdA==", "finish_reason": "success"}],
        }

        cases = [
            ("landscape", "1360x768"),
            ("square", "1024x1024"),
            ("portrait", "768x1360"),
        ]
        for hermes_ar, expected_size in cases:
            with patch("plugins.image_gen.stepfun.requests.post", return_value=mock_resp) as mock_post, \
                 patch("plugins.image_gen.stepfun.save_b64_image", return_value="/tmp/test.png"):
                provider = StepFunImageGenProvider()
                provider.generate(prompt="test", aspect_ratio=hermes_ar)
            payload = mock_post.call_args.kwargs.get("json") or {}
            assert payload["size"] == expected_size, (
                f"aspect_ratio={hermes_ar} should map to size={expected_size}, "
                f"got {payload['size']!r}"
            )

    def test_tunable_kwargs_pass_through(self):
        """steps / cfg_scale / negative_prompt / text_mode are wired through."""
        from plugins.image_gen.stepfun import StepFunImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [{"b64_json": "dGVzdA==", "finish_reason": "success"}],
        }

        with patch("plugins.image_gen.stepfun.requests.post", return_value=mock_resp) as mock_post, \
             patch("plugins.image_gen.stepfun.save_b64_image", return_value="/tmp/test.png"):
            provider = StepFunImageGenProvider()
            provider.generate(
                prompt="test",
                steps=25,
                cfg_scale=7.5,
                negative_prompt="ugly, blurry",
                text_mode=True,
                seed=42,
            )

        payload = mock_post.call_args.kwargs.get("json") or {}
        assert payload["steps"] == 25
        assert payload["cfg_scale"] == 7.5
        assert payload["negative_prompt"] == "ugly, blurry"
        assert payload["text_mode"] is True
        assert payload["seed"] == 42


# ---------------------------------------------------------------------------
# Registration test
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register(self):
        from plugins.image_gen.stepfun import StepFunImageGenProvider, register

        mock_ctx = MagicMock()
        register(mock_ctx)
        mock_ctx.register_image_gen_provider.assert_called_once()
        provider = mock_ctx.register_image_gen_provider.call_args[0][0]
        assert isinstance(provider, StepFunImageGenProvider)
        assert provider.name == "stepfun"
