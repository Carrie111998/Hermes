"""Tests for the AtlasCloud image generation plugin."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import httpx
import pytest

from agent import image_gen_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    image_gen_registry._reset_for_tests()
    yield
    image_gen_registry._reset_for_tests()


def test_atlas_provider_registers():
    from plugins.image_gen.atlas import AtlasImageGenProvider
    from plugins.image_gen.atlas.catalog import DEFAULT_MODEL

    provider = AtlasImageGenProvider()
    image_gen_registry.register_provider(provider)

    assert image_gen_registry.get_provider("atlas") is provider
    assert provider.display_name == "AtlasCloud"
    assert provider.default_model() == DEFAULT_MODEL


def test_atlas_unavailable_without_key(monkeypatch):
    from plugins.image_gen.atlas import AtlasImageGenProvider

    monkeypatch.delenv("ATLAS_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    assert AtlasImageGenProvider().is_available() is False


def test_atlas_generate_requires_key(monkeypatch):
    from plugins.image_gen.atlas import AtlasImageGenProvider

    monkeypatch.delenv("ATLAS_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    result = AtlasImageGenProvider().generate("a cat")
    assert result["success"] is False
    assert result["error_type"] == "auth_required"


def test_build_payload_uses_atlas_top_level_fields():
    from plugins.image_gen.atlas.client import build_payload

    payload = build_payload(
        atlas_model="google/nano-banana-2/text-to-image",
        prompt="a cat",
        aspect_ratio="portrait",
    )

    assert payload == {
        "model": "google/nano-banana-2/text-to-image",
        "prompt": "a cat",
        "aspect_ratio": "9:16",
        "output_format": "png",
        "enable_sync_mode": True,
        "num_images": 1,
    }


def test_resolve_model_accepts_full_atlas_model_id():
    from plugins.image_gen.atlas.catalog import resolve_model

    model_id, atlas_model = resolve_model("google/nano-banana-pro/text-to-image")

    assert model_id == "nano-banana-pro"
    assert atlas_model == "google/nano-banana-pro/text-to-image"


def test_generate_success_with_outputs_url(monkeypatch):
    import plugins.image_gen.atlas as atlas
    from plugins.image_gen.atlas import client

    captured = {}

    def fake_generate_image(payload, *, api_key, api_root):
        captured["payload"] = payload
        captured["api_key"] = api_key
        captured["api_root"] = api_root
        return {"data": {"outputs": ["https://cdn.example/cat.png"]}}

    monkeypatch.setenv("ATLAS_API_KEY", "test-key")
    monkeypatch.setattr(client, "generate_image", fake_generate_image)

    result = atlas.AtlasImageGenProvider().generate(
        "a cat",
        aspect_ratio="square",
        model="nano-banana-2",
    )

    assert result["success"] is True
    assert result["provider"] == "atlas"
    assert result["image"] == "https://cdn.example/cat.png"
    assert result["model"] == "nano-banana-2"
    assert result["atlas_model"] == "google/nano-banana-2/text-to-image"
    assert captured["payload"]["aspect_ratio"] == "1:1"
    assert captured["api_key"] == "test-key"


def test_generate_success_with_urls_dict(monkeypatch):
    from plugins.image_gen.atlas import AtlasImageGenProvider
    from plugins.image_gen.atlas import client

    monkeypatch.setenv("ATLAS_API_KEY", "test-key")
    monkeypatch.setattr(
        client,
        "generate_image",
        lambda *args, **kwargs: {"data": {"urls": {"image": "https://cdn.example/out.png"}}},
    )

    result = AtlasImageGenProvider().generate("a cat")

    assert result["success"] is True
    assert result["image"] == "https://cdn.example/out.png"


def test_generate_materializes_data_uri(monkeypatch, tmp_path):
    from plugins.image_gen.atlas import AtlasImageGenProvider
    from plugins.image_gen.atlas import client

    raw = base64.b64encode(b"image-bytes").decode("ascii")
    monkeypatch.setenv("ATLAS_API_KEY", "test-key")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        client,
        "generate_image",
        lambda *args, **kwargs: {"data": {"outputs": [f"data:image/png;base64,{raw}"]}},
    )

    result = AtlasImageGenProvider().generate("a cat")

    assert result["success"] is True
    assert result["image"].startswith(str(tmp_path))


def test_generate_reports_http_error(monkeypatch):
    from plugins.image_gen.atlas import AtlasImageGenProvider
    from plugins.image_gen.atlas import client

    response = MagicMock()
    response.status_code = 401
    response.text = "Unauthorized"
    exc = httpx.HTTPStatusError("unauthorized", request=MagicMock(), response=response)

    monkeypatch.setenv("ATLAS_API_KEY", "test-key")
    monkeypatch.setattr(client, "generate_image", MagicMock(side_effect=exc))

    result = AtlasImageGenProvider().generate("a cat")

    assert result["success"] is False
    assert result["error_type"] == "api_error"
    assert "401" in result["error"]


def test_register_calls_register_image_gen_provider():
    from plugins.image_gen.atlas import AtlasImageGenProvider, register

    ctx = MagicMock()
    register(ctx)
    ctx.register_image_gen_provider.assert_called_once()
    provider = ctx.register_image_gen_provider.call_args[0][0]
    assert isinstance(provider, AtlasImageGenProvider)
