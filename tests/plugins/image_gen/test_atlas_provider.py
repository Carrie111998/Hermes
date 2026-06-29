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
    monkeypatch.delenv("ATLAS_DEV_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("ATLAS_API_BASE", raising=False)
    monkeypatch.delenv("ATLAS_BASE_URL", raising=False)
    monkeypatch.delenv("ATLAS_INTERNAL_ENV", raising=False)

    assert AtlasImageGenProvider().is_available() is False


def test_atlas_generate_requires_key(monkeypatch):
    from plugins.image_gen.atlas import AtlasImageGenProvider

    monkeypatch.delenv("ATLAS_API_KEY", raising=False)
    monkeypatch.delenv("ATLAS_DEV_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("ATLAS_API_BASE", raising=False)
    monkeypatch.delenv("ATLAS_BASE_URL", raising=False)
    monkeypatch.delenv("ATLAS_INTERNAL_ENV", raising=False)

    result = AtlasImageGenProvider().generate("a cat")
    assert result["success"] is False
    assert result["error_type"] == "auth_required"


def test_resolve_credentials_prefers_dev_key_for_dev_base(monkeypatch):
    from plugins.image_gen.atlas import client

    monkeypatch.setenv("ATLAS_API_BASE", "https://api.dev.atlascloud.ai/v1")
    monkeypatch.setenv("ATLAS_API_KEY", "prod-key")
    monkeypatch.setenv("ATLAS_DEV_API_KEY", "dev-key")

    key, root = client.resolve_credentials()

    assert key == "dev-key"
    assert root == "https://api.dev.atlascloud.ai"


def test_headers_include_extra_api_header(monkeypatch):
    from plugins.image_gen.atlas import client

    monkeypatch.setenv("ATLAS_API_EXTRA_HEADER_NAME", "atlas")
    monkeypatch.setenv("ATLAS_API_EXTRA_HEADER_VALUE", "gateway-secret")

    headers = client.headers("api-key")

    assert headers["Authorization"] == "Bearer api-key"
    assert headers["atlas"] == "gateway-secret"


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


def test_build_payload_uses_images_for_reference_inputs(tmp_path):
    from plugins.image_gen.atlas.client import build_payload

    image = tmp_path / "ref.png"
    image.write_bytes(base64.b64decode("iVBORw0KGgo="))

    payload = build_payload(
        atlas_model="google/nano-banana/edit",
        prompt="turn this into a broadcast still",
        aspect_ratio="landscape",
        reference_image_urls=[str(image), "https://cdn.example/other.png"],
        seed=7,
    )

    assert payload["model"] == "google/nano-banana/edit"
    assert payload["images"][0].startswith("data:image/png;base64,")
    assert payload["images"][1] == "https://cdn.example/other.png"
    assert payload["seed"] == 7


def test_local_reference_image_rejects_non_image_file(tmp_path):
    from plugins.image_gen.atlas.client import build_payload

    secret = tmp_path / "secret.txt"
    secret.write_text("not an image")

    with pytest.raises(ValueError, match="PNG, JPEG, WebP, or GIF"):
        build_payload(
            atlas_model="google/nano-banana/edit",
            prompt="edit this",
            aspect_ratio="square",
            reference_image_urls=[str(secret)],
        )


def test_local_reference_image_rejects_oversized_file(tmp_path, monkeypatch):
    from plugins.image_gen.atlas import client

    image = tmp_path / "ref.png"
    image.write_bytes(base64.b64decode("iVBORw0KGgo="))
    monkeypatch.setattr(client, "MAX_LOCAL_IMAGE_BYTES", 1)

    with pytest.raises(ValueError, match="too large"):
        client.build_payload(
            atlas_model="google/nano-banana/edit",
            prompt="edit this",
            aspect_ratio="square",
            reference_image_urls=[str(image)],
        )


def test_reference_image_rejects_remote_file_uri():
    from plugins.image_gen.atlas.client import build_payload

    with pytest.raises(ValueError, match="file:// reference images"):
        build_payload(
            atlas_model="google/nano-banana/edit",
            prompt="edit this",
            aspect_ratio="square",
            reference_image_urls=["file://remote-host/tmp/ref.png"],
        )


def test_reference_image_rejects_non_image_data_uri():
    from plugins.image_gen.atlas.client import build_payload

    with pytest.raises(ValueError, match="data URIs must be PNG"):
        build_payload(
            atlas_model="google/nano-banana/edit",
            prompt="edit this",
            aspect_ratio="square",
            reference_image_urls=["data:text/plain;base64,aGk="],
        )


def test_reference_image_rejects_invalid_data_uri_base64():
    from plugins.image_gen.atlas.client import build_payload

    with pytest.raises(ValueError, match="valid base64"):
        build_payload(
            atlas_model="google/nano-banana/edit",
            prompt="edit this",
            aspect_ratio="square",
            reference_image_urls=["data:image/png;base64,not valid"],
        )


def test_reference_image_rejects_oversized_data_uri(monkeypatch):
    from plugins.image_gen.atlas import client

    monkeypatch.setattr(client, "MAX_LOCAL_IMAGE_BYTES", 1)
    data_uri = "data:image/png;base64," + base64.b64encode(b"xx").decode("ascii")

    with pytest.raises(ValueError, match="too large"):
        client.build_payload(
            atlas_model="google/nano-banana/edit",
            prompt="edit this",
            aspect_ratio="square",
            reference_image_urls=[data_uri],
        )


def test_resolve_model_accepts_full_atlas_model_id():
    from plugins.image_gen.atlas.catalog import resolve_model

    model_id, atlas_model = resolve_model("google/nano-banana-pro/text-to-image")

    assert model_id == "nano-banana-pro"
    assert atlas_model == "google/nano-banana-pro/text-to-image"


def test_resolve_model_uses_edit_route_for_references():
    from plugins.image_gen.atlas.catalog import resolve_model

    model_id, atlas_model = resolve_model("nano-banana-2", edit=True)

    assert model_id == "nano-banana-edit"
    assert atlas_model == "google/nano-banana/edit"


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


def test_generate_with_references_routes_to_edit_model(monkeypatch):
    import plugins.image_gen.atlas as atlas
    from plugins.image_gen.atlas import client

    captured = {}

    def fake_generate_image(payload, *, api_key, api_root):
        captured["payload"] = payload
        return {"data": {"outputs": ["https://cdn.example/still.png"]}}

    monkeypatch.setenv("ATLAS_API_KEY", "test-key")
    monkeypatch.setattr(client, "generate_image", fake_generate_image)

    result = atlas.AtlasImageGenProvider().generate(
        "make a broadcast still",
        aspect_ratio="landscape",
        model="nano-banana-2",
        reference_image_urls=["https://cdn.example/person.png"],
    )

    assert result["success"] is True
    assert result["model"] == "nano-banana-edit"
    assert result["atlas_model"] == "google/nano-banana/edit"
    assert result["reference_image_count"] == 1
    assert captured["payload"]["images"] == ["https://cdn.example/person.png"]


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
