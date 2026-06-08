"""Tests for the AtlasCloud video generation plugin."""

from __future__ import annotations

import base64

import pytest

from agent import video_gen_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    video_gen_registry._reset_for_tests()
    yield
    video_gen_registry._reset_for_tests()


def test_atlas_provider_registers():
    from plugins.video_gen.atlas import AtlasVideoGenProvider
    from plugins.video_gen.atlas.catalog import DEFAULT_MODEL

    provider = AtlasVideoGenProvider()
    video_gen_registry.register_provider(provider)

    assert video_gen_registry.get_provider("atlas") is provider
    assert provider.display_name == "AtlasCloud"
    assert provider.default_model() == DEFAULT_MODEL


def test_atlas_unavailable_without_key(monkeypatch):
    from plugins.video_gen.atlas import AtlasVideoGenProvider

    monkeypatch.delenv("ATLAS_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    assert AtlasVideoGenProvider().is_available() is False


def test_atlas_generate_requires_key(monkeypatch):
    from plugins.video_gen.atlas import AtlasVideoGenProvider

    monkeypatch.delenv("ATLAS_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    result = AtlasVideoGenProvider().generate("a dog running")
    assert result["success"] is False
    assert result["error_type"] == "auth_required"


def test_payload_uses_top_level_atlas_image_data_uri(tmp_path):
    from plugins.video_gen.atlas.client import build_payload
    from plugins.video_gen.atlas.catalog import ATLAS_FAMILIES

    image = tmp_path / "frame.png"
    image.write_bytes(base64.b64decode("iVBORw0KGgo="))

    payload = build_payload(
        ATLAS_FAMILIES["wan-2.6-flash"],
        atlas_model="alibaba/wan-2.6/image-to-video-flash",
        prompt="slow camera push",
        image_url=str(image),
        duration=5,
        aspect_ratio="16:9",
        resolution="720p",
        audio=None,
        seed=12,
    )

    assert payload["model"] == "alibaba/wan-2.6/image-to-video-flash"
    assert payload["image"].startswith("data:image/png;base64,")
    assert "input" not in payload
    assert "img_url" not in payload
    assert payload["seed"] == 12


def test_text_payload_uses_top_level_model_prompt_and_resolution():
    from plugins.video_gen.atlas.client import build_payload
    from plugins.video_gen.atlas.catalog import ATLAS_FAMILIES

    payload = build_payload(
        ATLAS_FAMILIES["wan-2.6-flash"],
        atlas_model="alibaba/wan-2.6/text-to-video",
        prompt="a product ad",
        image_url=None,
        duration=99,
        aspect_ratio="9:16",
        resolution="1080p",
        audio=None,
        seed=None,
    )

    assert payload["model"] == "alibaba/wan-2.6/text-to-video"
    assert payload["prompt"] == "a product ad"
    assert payload["duration"] == 15
    assert payload["resolution"] == "720P"
    assert payload["aspect_ratio"] == "9:16"
    assert payload["enable_sync_mode"] is False


def test_text_generation_submits_and_polls(monkeypatch):
    import plugins.video_gen.atlas as atlas
    from plugins.video_gen.atlas import client

    captured = {}

    async def fake_submit(http, payload, *, api_key, api_root):
        captured["payload"] = payload
        captured["api_key"] = api_key
        captured["api_root"] = api_root
        return "pred-1"

    async def fake_poll(http, prediction_id, **kwargs):
        captured["prediction_id"] = prediction_id
        return {"status": "completed", "body": {"outputs": ["https://cdn.example/out.mp4"]}}

    monkeypatch.setenv("ATLAS_API_KEY", "test-key")
    monkeypatch.setattr(client, "submit", fake_submit)
    monkeypatch.setattr(client, "poll", fake_poll)

    result = atlas.AtlasVideoGenProvider().generate(
        "a dog running",
        model="wan-2.6-flash",
    )

    assert result["success"] is True
    assert result["provider"] == "atlas"
    assert result["modality"] == "text"
    assert result["video"] == "https://cdn.example/out.mp4"
    assert captured["payload"]["model"] == "alibaba/wan-2.6/text-to-video"
    assert captured["prediction_id"] == "pred-1"


def test_image_generation_routes_to_image_model(monkeypatch, tmp_path):
    import plugins.video_gen.atlas as atlas
    from plugins.video_gen.atlas import client

    image = tmp_path / "frame.jpg"
    image.write_bytes(b"fake")
    captured = {}

    async def fake_submit(http, payload, **kwargs):
        captured["payload"] = payload
        return "pred-2"

    async def fake_poll(http, prediction_id, **kwargs):
        return {
            "status": "completed",
            "body": {"urls": {"video": "https://cdn.example/i2v.mp4"}},
        }

    monkeypatch.setenv("ATLAS_API_KEY", "test-key")
    monkeypatch.setattr(client, "submit", fake_submit)
    monkeypatch.setattr(client, "poll", fake_poll)

    result = atlas.AtlasVideoGenProvider().generate(
        "animate the product",
        model="wan-2.6-flash",
        image_url=str(image),
    )

    assert result["success"] is True
    assert result["modality"] == "image"
    assert result["video"] == "https://cdn.example/i2v.mp4"
    assert captured["payload"]["model"] == "alibaba/wan-2.6/image-to-video-flash"
    assert captured["payload"]["image"].startswith("data:image/jpeg;base64,")


def test_full_model_id_rejects_wrong_modality(monkeypatch):
    from plugins.video_gen.atlas import AtlasVideoGenProvider

    monkeypatch.setenv("ATLAS_API_KEY", "test-key")
    result = AtlasVideoGenProvider().generate(
        "text prompt",
        model="alibaba/wan-2.6/image-to-video-flash",
    )

    assert result["success"] is False
    assert result["error_type"] == "modality_unsupported"


def test_prediction_self_url_is_not_treated_as_output():
    from plugins.video_gen.atlas.client import first_output_url

    body = {
        "status": "completed",
        "url": "https://api.atlascloud.ai/api/v1/model/prediction/pred-1",
    }

    assert first_output_url(body) is None


def test_output_dict_url_is_treated_as_output():
    from plugins.video_gen.atlas.client import first_output_url

    body = {
        "status": "completed",
        "outputs": [{"url": "https://cdn.example/video.mp4"}],
    }

    assert first_output_url(body) == "https://cdn.example/video.mp4"
