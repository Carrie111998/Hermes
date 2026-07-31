"""Tests for the bundled MiniMax text-to-image providers."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import plugins.image_gen.minimax as minimax_plugin


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_CN_API_KEY", raising=False)


def _response(
    image_value: str = "https://images.example/generated.png",
    *,
    status_code: int = 200,
    base_status: int = 0,
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.text = ""
    response.json.return_value = {
        "data": {"image_urls": [image_value]},
        "metadata": {"success_count": 1, "failed_count": 0},
        "base_resp": {"status_code": base_status, "status_msg": "ok"},
    }
    return response


def test_registers_global_and_china_providers():
    ctx = MagicMock()

    minimax_plugin.register(ctx)

    providers = [
        call.args[0] for call in ctx.register_image_gen_provider.call_args_list
    ]
    assert [provider.name for provider in providers] == ["minimax", "minimax-cn"]


@pytest.mark.parametrize("provider_name", ["minimax", "minimax-cn"])
def test_model_catalog_matches_supported_models(provider_name):
    provider = minimax_plugin.MiniMaxImageGenProvider(provider_name)

    assert [model["id"] for model in provider.list_models()] == [
        "image-01",
        "image-01-live",
    ]
    assert provider.default_model() == "image-01"
    assert provider.capabilities() == {
        "modalities": ["text"],
        "max_reference_images": 0,
    }


@pytest.mark.parametrize(
    ("provider_name", "api_key_env", "endpoint"),
    [
        ("minimax", "MINIMAX_API_KEY", "https://api.minimax.io/v1/image_generation"),
        (
            "minimax-cn",
            "MINIMAX_CN_API_KEY",
            "https://api.minimaxi.com/v1/image_generation",
        ),
    ],
)
def test_region_uses_matching_endpoint_and_api_key(
    monkeypatch, tmp_path, provider_name, api_key_env, endpoint
):
    monkeypatch.setenv(api_key_env, "test-region-key")
    mock_post = MagicMock(return_value=_response())
    monkeypatch.setattr(minimax_plugin.requests, "post", mock_post)
    monkeypatch.setattr(
        minimax_plugin,
        "save_url_image",
        lambda url, prefix: tmp_path / f"{prefix}.png",
    )

    result = minimax_plugin.MiniMaxImageGenProvider(provider_name).generate(
        "a mountain lake",
        aspect_ratio="portrait",
    )

    assert result["success"] is True
    assert result["provider"] == provider_name
    assert result["model"] == "image-01"
    assert result["success_count"] == 1
    assert result["failed_count"] == 0
    call = mock_post.call_args
    assert call.args[0] == endpoint
    assert call.kwargs["headers"]["Authorization"] == "Bearer test-region-key"
    assert call.kwargs["json"] == {
        "model": "image-01",
        "prompt": "a mountain lake",
        "aspect_ratio": "9:16",
        "response_format": "url",
        "n": 1,
    }


def test_request_forwards_supported_optional_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    mock_post = MagicMock(return_value=_response())
    monkeypatch.setattr(minimax_plugin.requests, "post", mock_post)
    monkeypatch.setattr(
        minimax_plugin,
        "save_url_image",
        lambda url, prefix: tmp_path / "generated.png",
    )
    subject_reference = [{"type": "character", "image_file": "encoded-image"}]

    result = minimax_plugin.MiniMaxImageGenProvider().generate(
        "a studio portrait",
        model="image-01-live",
        subject_reference=subject_reference,
        width=1024,
        height=1024,
        seed=42,
        n=2,
        prompt_optimizer=True,
    )

    assert result["success"] is True
    payload = mock_post.call_args.kwargs["json"]
    assert payload == {
        "model": "image-01-live",
        "prompt": "a studio portrait",
        "aspect_ratio": "16:9",
        "response_format": "url",
        "n": 2,
        "subject_reference": subject_reference,
        "width": 1024,
        "height": 1024,
        "seed": 42,
        "prompt_optimizer": True,
    }


def test_base64_response_is_saved_from_image_urls(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    encoded = base64.b64encode(b"generated-image").decode()
    monkeypatch.setattr(
        minimax_plugin.requests,
        "post",
        MagicMock(return_value=_response(encoded)),
    )

    result = minimax_plugin.MiniMaxImageGenProvider().generate(
        "an abstract poster",
        response_format="base64",
    )

    assert result["success"] is True
    assert result["response_format"] == "base64"
    assert Path(result["image"]).read_bytes() == b"generated-image"


def test_base64_data_url_preserves_extension(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    encoded = base64.b64encode(b"jpeg-data").decode()
    monkeypatch.setattr(
        minimax_plugin.requests,
        "post",
        MagicMock(return_value=_response(f"data:image/jpeg;base64,{encoded}")),
    )

    result = minimax_plugin.MiniMaxImageGenProvider().generate(
        "a product photo",
        response_format="base64",
    )

    assert result["success"] is True
    assert Path(result["image"]).suffix == ".jpg"
    assert Path(result["image"]).read_bytes() == b"jpeg-data"


def test_url_cache_failure_returns_original_url(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    image_url = "https://images.example/temporary.png"
    monkeypatch.setattr(
        minimax_plugin.requests,
        "post",
        MagicMock(return_value=_response(image_url)),
    )
    monkeypatch.setattr(
        minimax_plugin,
        "save_url_image",
        MagicMock(side_effect=OSError("cache unavailable")),
    )

    result = minimax_plugin.MiniMaxImageGenProvider().generate("a city skyline")

    assert result["success"] is True
    assert result["image"] == image_url


@pytest.mark.parametrize(
    ("provider_name", "expected_key"),
    [
        ("minimax", "MINIMAX_API_KEY"),
        ("minimax-cn", "MINIMAX_CN_API_KEY"),
    ],
)
def test_missing_regional_key_returns_auth_error(provider_name, expected_key):
    result = minimax_plugin.MiniMaxImageGenProvider(provider_name).generate("a cat")

    assert result["success"] is False
    assert result["error_type"] == "auth_required"
    assert expected_key in result["error"]


def test_rejects_image_inputs_for_text_to_image_scope(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    mock_post = MagicMock()
    monkeypatch.setattr(minimax_plugin.requests, "post", mock_post)

    result = minimax_plugin.MiniMaxImageGenProvider().generate(
        "change the background",
        image_url="https://images.example/source.png",
    )

    assert result["success"] is False
    assert result["error_type"] == "modality_unsupported"
    mock_post.assert_not_called()


def test_business_error_uses_base_response_status(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    response = _response(base_status=1008)
    response.json.return_value["base_resp"]["status_msg"] = "insufficient balance"
    monkeypatch.setattr(
        minimax_plugin.requests, "post", MagicMock(return_value=response)
    )

    result = minimax_plugin.MiniMaxImageGenProvider().generate("a cat")

    assert result["success"] is False
    assert result["error_type"] == "provider_error"
    assert "1008" in result["error"]
    assert "insufficient balance" in result["error"]


def test_invalid_response_format_is_rejected(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")

    result = minimax_plugin.MiniMaxImageGenProvider().generate(
        "a cat",
        response_format="binary",
    )

    assert result["success"] is False
    assert result["error_type"] == "invalid_argument"


def test_empty_image_urls_returns_empty_response(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    response = _response()
    response.json.return_value["data"]["image_urls"] = []
    monkeypatch.setattr(
        minimax_plugin.requests, "post", MagicMock(return_value=response)
    )

    result = minimax_plugin.MiniMaxImageGenProvider().generate("a cat")

    assert result["success"] is False
    assert result["error_type"] == "empty_response"
